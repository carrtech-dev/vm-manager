"""Self-contained Linux patch RPC and persistent systemd worker. No app imports."""
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT = Path('/var/lib/vm-manager-patching')


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def command(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=25, **kwargs)


def probe():
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise RuntimeError('Only Linux guests with root guest-agent execution are supported')
    release = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep:
            release[key] = value.strip('\"\'')
    distro, version = release.get('ID', ''), release.get('VERSION_ID', '')
    major = int(version.split('.')[0]) if version.split('.')[0].isdigit() else 0
    supported = ((distro == 'ubuntu' and major >= 22) or (distro == 'debian' and major >= 12)
                 or (distro in ('rocky', 'almalinux', 'rhel') and major >= 8))
    if not supported:
        raise RuntimeError('Unsupported Linux distribution: ' + release.get('PRETTY_NAME', distro))
    manager = 'apt-get' if distro in ('ubuntu', 'debian') else 'dnf'
    if not shutil.which(manager) or not Path('/run/systemd/system').is_dir():
        raise RuntimeError('Native package manager and a running systemd are required')
    if shutil.disk_usage('/').free < 512 * 1024**2:
        raise RuntimeError('At least 512 MiB free on the root filesystem is required')
    if os.path.ismount('/boot') and shutil.disk_usage('/boot').free < 100 * 1024**2:
        raise RuntimeError('At least 100 MiB free on /boot is required')
    return {'ready': True, 'os': release.get('PRETTY_NAME', distro), 'manager': manager,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def packages(manager):
    args = ['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n'] if manager == 'apt-get' else [
        'rpm', '-qa', '--qf', '%{NAME}.%{ARCH}\t%{VERSION}-%{RELEASE}\n']
    result = command(args)
    if result.returncode:
        raise RuntimeError('Could not read installed package versions')
    versions = {}
    for line in result.stdout.splitlines():
        if '\t' in line:
            package, version = line.split('\t', 1)
            versions.setdefault(package, set()).add(version)
    return {package: sorted(values) for package, values in versions.items()}


def reboot_required(manager, changed):
    if manager == 'apt-get':
        if Path('/var/run/reboot-required').exists() or any(p.startswith(('linux-image', 'linux-base')) for p in changed):
            return True
        return False if Path('/usr/share/update-notifier/notify-reboot-required').exists() else None
    result = command(['dnf', 'needs-restarting', '-r'])
    unavailable = any(text in (result.stdout + result.stderr).lower() for text in ('no such command', 'unknown command', 'invalid choice'))
    if result.returncode in (0, 1) and not unavailable:
        return result.returncode == 1
    return True if any(p.startswith(('kernel', 'glibc', 'systemd')) for p in changed) else None


def worker(run_id):
    directory = ROOT / run_id
    state = json.loads((directory / 'state.json').read_text())
    log = directory / 'output.log'
    with (ROOT / 'transaction.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            state.update(status='failed', message='Another VM Manager patch transaction is active')
            write(directory / 'state.json', state)
            return
        try:
            info = probe()
            before = packages(info['manager'])
            state.update(status='running', message='Refreshing package repositories', **info)
            write(directory / 'state.json', state)
            env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='a', LC_ALL='C')
            if info['manager'] == 'apt-get':
                options = ['-o', 'DPkg::Lock::Timeout=120', '-o', 'Acquire::Retries=2',
                           '-o', 'Acquire::http::Timeout=30', '-o', 'Acquire::https::Timeout=30']
                commands = [['apt-get', *options, '-o', 'APT::Update::Error-Mode=any', 'update'],
                    ['apt-get', *options, '-y', '--with-new-pkgs', '--no-remove',
                     '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold', 'upgrade']]
            else:
                commands = [['dnf', '-y', '--refresh', '--setopt=timeout=30', '--setopt=retries=2', 'upgrade']]
            with log.open('a') as output:
                for args in commands:
                    state['message'] = 'Installing package updates' if args[-1] == 'upgrade' else 'Refreshing package repositories'
                    write(directory / 'state.json', state)
                    # Do not kill a package manager on a controller timeout. This
                    # independent unit owns the transaction until it exits.
                    result = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, env=env)
                    if result.returncode:
                        raise RuntimeError('Package transaction failed (exit %s); inspect the package output' % result.returncode)
            if info['manager'] == 'dnf':
                # RPM can report failed post-install scripts while DNF exits 0.
                # These are partial failures, not a successful maintenance run.
                with log.open(errors='replace') as output:
                    for line in output:
                        text = line.lower()
                        if 'scriptlet failed' in text or 'error in ' in text and 'scriptlet' in text or 'failed to exec scriptlet' in text:
                            raise RuntimeError('RPM scriptlet failed; inspect package output and repair the affected package')
            after = packages(info['manager'])
            changed = sorted(k for k, v in after.items() if before.get(k) != v)
            state.update(status='completed', changed_count=len(changed), packages=[p[:128] for p in changed[:200]],
                         reboot_required=reboot_required(info['manager'], changed),
                         message='Updated %s packages' % len(changed))
        except Exception as exc:
            state.update(status='failed', message=str(exc)[:1000])
        state['finished'] = time.time()
        write(directory / 'state.json', state)


def execute(body):
    operation = body['action']
    if operation == 'probe':
        return probe()
    run_id = body.get('run_id', '')
    if not re.fullmatch('[0-9a-f]{32}', run_id):
        raise RuntimeError('Invalid patch reference')
    info = probe() if operation == 'launch' else None
    directory = ROOT / run_id
    state_file = directory / 'state.json'
    if operation == 'launch':
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Serialize launch retries, including a lost response after systemd-run.
        with (ROOT / 'launch.lock').open('a') as launch_lock:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)
            if not state_file.exists():
                directory.mkdir(mode=0o700, exist_ok=True)
                (directory / 'worker.py').write_text(body['source'])
                write(state_file, dict(info, status='starting', started=time.time(), message='Starting patch worker'))
                result = command(['systemd-run', '--unit=vmm-patch-' + run_id,
                    '--property=Type=exec', '--property=UMask=0077', '--property=Restart=no',
                    '/usr/bin/python3', str(directory / 'worker.py'), '--worker', run_id])
                if result.returncode:
                    write(state_file, dict(info, status='failed', message='Could not start the systemd patch worker'))
    if not state_file.exists():
        return {'status': 'missing', 'message': 'No guest transaction record'}
    state = json.loads(state_file.read_text())
    state['boot_id_now'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if state['status'] in ('starting', 'running'):
        active = command(['systemctl', 'is-active', 'vmm-patch-' + run_id]).stdout.strip()
        if active not in ('active', 'activating') and time.time() - state['started'] > 30:
            # The worker may have committed its final result between our first
            # read and systemctl. Never overwrite that result with interrupted.
            state = json.loads(state_file.read_text())
            if state['status'] in ('starting', 'running'):
                state.update(status='interrupted', message='Guest patch worker stopped; inspect package state before retrying')
                write(state_file, state)
            state['boot_id_now'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if operation == 'reboot' and state['status'] == 'completed' and not state.get('reboot_requested'):
        # Persist intent first. Never repeat a reboot after a lost agent response.
        state['reboot_requested'] = True
        state['reboot_requested_at'] = time.time()
        write(state_file, state)
        result = command(['systemd-run', '--unit=vmm-patch-reboot-' + run_id,
                          '--on-active=5s', '/usr/bin/systemctl', 'reboot'])
        if result.returncode:
            state['reboot_error'] = 'Unable to schedule reboot; reboot manually'
            write(state_file, state)
    log = directory / 'output.log'
    if log.exists():
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 12000))
            text = stream.read(12000).decode(errors='replace')
        state['output'] = re.sub(r'(https?://)[^\s/@]+(?::[^\s/@]*)?@', r'\1[redacted]@', text)
    return state


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        worker(sys.argv[2])
    else:
        try:
            print(json.dumps(execute(json.load(sys.stdin))))
        except Exception as exc:
            print(json.dumps({'ready': False, 'status': 'unavailable', 'message': str(exc)[:1000]}))
