"""Self-contained Linux guest mount installer, sent over an authenticated guest agent.

Connection data travels on stdin, never in a shell command or process arguments.
Only generated service IDs and validated /mnt or /media paths are accepted by the
broker. Keep this module independent of application dependencies.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path('/etc/vm-manager/mounts')
OS_RELEASE = Path('/etc/os-release')


def command(*args, timeout=300):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Utilities can print connection strings. Never forward their output.
        raise RuntimeError('Guest command failed: ' + args[0] + '. Check guest networking and the storage connection.')
    return result.stdout.strip()


def rclone_config(source):
    hostname = urlsplit(source['endpoint']).hostname or ''
    r2 = hostname.endswith('.r2.cloudflarestorage.com')
    text = ('[remote]\ntype = s3\nprovider = ' + ('Cloudflare' if r2 else 'Other') +
            '\naccess_key_id = ' + source['access_key'] + '\nsecret_access_key = ' + source['secret_key'] +
            '\nendpoint = ' + source['endpoint'] + '\nregion = ' + source['region'] +
            '\nforce_path_style = true\nno_check_bucket = true\n')
    if r2:
        # Bucket-scoped keys cannot create buckets. R2 also rejects the versioned
        # HEAD that rclone performs after PUT; retain PUT checksum validation.
        text += 'no_head = true\n'
    return text


def install_tools(kind):
    needed = ['cifs-utils'] if kind == 'smb' else ['rclone', 'fuse3', 'ca-certificates']
    if shutil.which('mount.cifs' if kind == 'smb' else 'rclone') and (kind == 'smb' or shutil.which('fusermount3')):
        return
    try:
        if shutil.which('apt-get'):
            command('env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'update', timeout=300)
            command('env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-install-recommends', *needed, timeout=600)
        elif shutil.which('dnf'):
            repositories = []
            if kind == 's3' and not shutil.which('rclone'):
                release = dict(line.split('=', 1) for line in OS_RELEASE.read_text().splitlines() if '=' in line)
                distro = release.get('ID', '').strip('\"\'')
                major = release.get('VERSION_ID', '').strip('\"\'').split('.')[0]
                if distro in ('rocky', 'almalinux') and major.isdigit() and int(major) >= 8:
                    # rclone is in EPEL, not the base Rocky/Alma repositories.
                    # Enable build dependencies only for this installation.
                    command('dnf', 'install', '-y', 'epel-release', timeout=300)
                    repositories = ['--enablerepo=' + ('powertools' if major == '8' else 'crb')]
            command('dnf', *repositories, 'install', '-y', *needed, timeout=600)
        else:
            raise RuntimeError('No supported package manager')
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Could not install guest storage tools (' + ', '.join(needed) +
                           '). Check guest package repositories and outbound network access, or install these packages before retrying.') from exc


def pending(config):
    if config['source']['kind'] != 's3':
        return
    auth = base64.b64encode(('vmm:' + config['rc_password']).encode()).decode()
    req = urllib.request.Request('http://127.0.0.1:' + str(config['rc_port']) + '/vfs/stats',
        data=b'{}', headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            stats = json.load(response)
    except Exception as exc:
        cache_path = ROOT / config.get('id', '') / 'cache'
        if config.get('path') and not os.path.ismount(config['path']) and not any(p.is_file() for p in cache_path.rglob('*')):
            # A first mount that failed authentication cannot have dirty data
            # when neither a mount nor any persistent cache files exist.
            return
        raise RuntimeError('Cannot verify pending S3 uploads. Keep the mount and cache, restore its service, then retry.') from exc
    cache = stats.get('diskCache') or {}
    if cache.get('uploadsInProgress', 0) or cache.get('uploadsQueued', 0) or cache.get('erroredFiles', 0):
        raise RuntimeError('S3 files are open or uploads are pending. Close files and wait for uploads before detaching.')


def busy(target):
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            references = [process / 'cwd', *list((process / 'fd').iterdir())]
        except OSError:
            continue
        for reference in references:
            try:
                value = os.readlink(reference)
            except OSError:
                continue
            if value == str(target) or value.startswith(str(target) + '/'):
                return True
    return False


def execute(body):
    mount_id = body['id']
    directory = ROOT / mount_id
    target = Path(body['path'])
    unit = 'vm-manager-share-' + mount_id + '.service'
    unit_path = Path('/etc/systemd/system') / unit
    config_path = directory / 'mount.json'
    if body['operation'] == 'status':
        if not config_path.is_file():
            raise RuntimeError('Mount configuration is missing inside the guest')
        if not os.path.ismount(target):
            raise RuntimeError('Storage is not mounted. Check the source and guest service ' + unit)
        # Force a bounded read so a disconnected share cannot appear healthy.
        command('timeout', '15', 'ls', '-A', str(target), timeout=20)
        return 'Mounted at ' + str(target)
    if body['operation'] == 'detach':
        if not config_path.exists() and os.path.ismount(target):
            raise RuntimeError('Mount configuration is missing; inspect the guest before detaching')
        if config_path.exists():
            config = json.loads(config_path.read_text())
            cache_files = any(p.is_file() for p in (directory / 'cache').rglob('*'))
            failed_empty_s3 = (config['source']['kind'] == 's3' and
                              body.get('status') in ('incomplete', 'unavailable') and not cache_files)
            if failed_empty_s3:
                # An unreachable bucket can leave our readiness read blocked
                # in FUSE. With a full VFS cache and no cache files, there are
                # no dirty writes to lose. Stop that failed mount to release
                # blocked readers; never apply this to a cache containing data.
                command('systemctl', 'stop', unit, timeout=330)
                if any(p.is_file() for p in (directory / 'cache').rglob('*')):
                    raise RuntimeError('S3 cache files appeared during detach. Configuration and cache retained; restore the service and verify uploads before retrying.')
            else:
                if busy(target):
                    raise RuntimeError('Files are open on this mount. Close files before detaching.')
                if config['source']['kind'] == 's3':
                    pending(config)
            if os.path.ismount(target):
                command('umount', str(target), timeout=60)
            command('systemctl', 'stop', unit)
            if os.path.ismount(target):
                command('umount', str(target), timeout=60)
            if os.path.ismount(target):
                raise RuntimeError('Storage is still mounted; close files before detaching')
            command('systemctl', 'disable', unit)
            unit_path.unlink(missing_ok=True)
            shutil.rmtree(directory)
            command('systemctl', 'daemon-reload')
        return 'Detached; remote files retained'
    if not shutil.which('systemctl') or not Path('/run/systemd/system').is_dir():
        raise RuntimeError('Automatic mounts require a Linux guest with systemd and Python 3')
    if not config_path.exists() and (target.is_symlink() or os.path.ismount(target) or (target.exists() and any(target.iterdir()))):
        raise RuntimeError('Choose an empty, unmounted directory inside the guest')
    # Refuse symlink parents, including /mnt itself; never cover existing data.
    if target.resolve() != target.absolute():
        raise RuntimeError('Guest mount paths cannot contain symbolic links')
    source = body['source']
    install_tools(source['kind'])
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    ROOT.chmod(0o700)
    directory.mkdir(mode=0o700, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        # A retry uses the same ID and cache; never overwrite a running mount.
        if os.path.ismount(target):
            command('timeout', '15', 'ls', '-A', str(target), timeout=20)
            return 'Mounted at ' + str(target)
    previous = json.loads(config_path.read_text()) if config_path.exists() else {}
    config = dict(body)
    if source['kind'] == 'smb':
        (directory / 'credentials').write_text('username=' + source['username'] + '\npassword=' + source['password'] + '\ndomain=' + source['domain'] + '\n')
        (directory / 'credentials').chmod(0o600)
    else:
        import socket
        import secrets
        if previous:
            config['rc_port'], config['rc_password'] = previous['rc_port'], previous['rc_password']
        else:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                config['rc_port'] = sock.getsockname()[1]
            config['rc_password'] = secrets.token_urlsafe(32)
        (directory / 'rclone.conf').write_text(rclone_config(source))
        (directory / 'rclone.conf').chmod(0o600)
    # Secrets are kept in guest root-only files, never in service arguments.
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    (directory / 'runner.py').write_text(body['runner'])
    (directory / 'runner.py').chmod(0o700)
    unit_path.write_text('[Unit]\nDescription=VM Manager external storage\nWants=network-online.target\nAfter=network-online.target\n\n[Service]\nType=simple\nExecStart=/usr/bin/python3 ' + str(directory / 'runner.py') + ' ' + str(config_path) + '\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=300\nUMask=0077\n\n[Install]\nWantedBy=multi-user.target\n')
    command('systemctl', 'daemon-reload')
    command('systemctl', 'enable', '--now', unit)
    if previous:
        command('systemctl', 'restart', unit)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if os.path.ismount(target):
            command('timeout', '15', 'ls', '-A', str(target), timeout=20)
            return 'Mounted at ' + str(target) + '; automatic mounting enabled'
        time.sleep(.5)
    raise RuntimeError('Mount did not become ready. Configuration retained for retry; check the connection and guest service ' + unit)


if __name__ == '__main__':
    try:
        print(execute(json.load(sys.stdin)))
    except Exception as error:
        # Our RuntimeErrors contain safe product messages. Other exceptions can
        # include a supplied secret, so return only their type.
        print(str(error) if isinstance(error, RuntimeError) else 'Guest mount operation failed: ' + type(error).__name__)
        sys.exit(1)
