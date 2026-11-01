#!/usr/bin/python3
"""Package lifecycle. Invoked by dpkg/rpm as root; never downloads dependencies."""
import datetime
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.request

SOURCE = Path('/usr/lib/vm-manager')
STATE = Path('/var/lib/vm-manager')
RUNTIME = Path('/var/lib/vm-manager-runtime')
CONFIG = Path('/etc/vm-manager')


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def access():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import vm_manager_access
    return vm_manager_access


def systemd():
    return Path('/run/systemd/system').is_dir()


def pre_upgrade():
    # This installer is deliberately distinct from the established source deployment.
    if Path('/opt/carrtech/vm-manager/current').exists():
        raise SystemExit('Source deployment detected. Migrate explicitly before installing packages.')
    if systemd():
        run('systemctl', 'stop', 'vm-manager.service', 'vm-manager-storage.service')
    database = STATE / 'manager.sqlite3'
    if database.exists():
        backups = RUNTIME / 'backups'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        backups.chmod(0o700)
        dest = backups / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.tar.gz')
        with sqlite3.connect(database) as source, sqlite3.connect(backups / 'database.sqlite3') as target:
            source.backup(target)
        (backups / 'database.sqlite3').chmod(0o600)
        with tarfile.open(dest, 'w:gz') as archive:
            archive.add(backups / 'database.sqlite3', arcname='manager.sqlite3')
            for path in (CONFIG, Path('/var/lib/vm-manager-storage'), Path('/var/lib/vm-manager-edge')):
                if path.exists():
                    # Do not archive mounted VM/backup data, only broker registries/secrets.
                    if path.name == 'vm-manager-storage':
                        for name in ('blocks.json', 'external.json'):
                            if (path / name).exists():
                                archive.add(path / name, arcname='var/lib/vm-manager-storage/' + name)
                    else:
                        archive.add(path, arcname=str(path).lstrip('/'))
        dest.chmod(0o600)
        (backups / 'database.sqlite3').unlink()
        print('VM Manager backup:', dest)


def configure():
    if os.geteuid() != 0:
        raise SystemExit('Run as root')
    os.umask(0o077)
    network = access()
    values = network.settings()
    if Path('/opt/carrtech/vm-manager/current').exists():
        raise SystemExit('Refusing to replace a source installation')
    try:
        account = pwd.getpwnam('vm-manager')
    except KeyError:
        run('useradd', '--system', '--user-group', '--home-dir', str(STATE),
            '--shell', '/usr/sbin/nologin', 'vm-manager')
        account = pwd.getpwnam('vm-manager')
    for group in ('incus-admin', 'libvirt'):
        try:
            grp.getgrnam(group)
        except KeyError:
            continue
        run('usermod', '-a', '-G', group, 'vm-manager')
    # Keep ISO traversal available to QEMU; credentials and DB themselves are 0600.
    for path in (STATE, STATE / 'isos', STATE / 'disks', STATE / 'tmp', STATE / 'external-staging'):
        path.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.chmod(0o755)
        os.chown(path, account.pw_uid, account.pw_gid)
    storage = Path('/var/lib/vm-manager-storage')
    storage.mkdir(mode=0o711, exist_ok=True)
    storage.chmod(0o711)
    (storage / 'mounts').mkdir(mode=0o755, exist_ok=True)
    (storage / 'mounts').chmod(0o755)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    temporary = RUNTIME / 'venv.next'
    if temporary.exists():
        shutil.rmtree(temporary)
    os.umask(0o022)
    run('/usr/bin/python3', '-m', 'venv', '--system-site-packages', str(temporary))
    run(str(temporary / 'bin/python'), '-m', 'pip', 'install', '--no-index', '--no-compile',
        '--find-links=' + str(SOURCE / 'wheels'), '-r', str(SOURCE / 'requirements.lock'))
    os.umask(0o077)
    # Verify imports before replacing a working runtime. Entry points use python,
    # not pip/uvicorn scripts whose shebangs point at the temporary venv path.
    run(str(temporary / 'bin/python'), '-c', 'import fastapi,uvicorn,psutil,libvirt')
    old = RUNTIME / 'venv.previous'
    if old.exists():
        shutil.rmtree(old)
    venv = RUNTIME / 'venv'
    if venv.exists():
        venv.rename(old)
    temporary.rename(venv)
    # The service needs traversal/read, but cannot modify its runtime.
    RUNTIME.chmod(0o755)
    CONFIG.mkdir(mode=0o750, exist_ok=True)
    CONFIG.chmod(0o750)
    os.chown(CONFIG, 0, account.pw_gid)
    tls = CONFIG / 'tls'
    tls.mkdir(mode=0o750, exist_ok=True)
    tls.chmod(0o750)
    os.chown(tls, 0, account.pw_gid)
    if not (tls / 'server.key').exists() and not (tls / 'server.crt').exists():
        run('openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '825',
            '-keyout', str(tls / 'server.key'), '-out', str(tls / 'server.crt'),
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=' + network.certificate_names(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not (tls / 'server.key').exists() or not (tls / 'server.crt').exists():
        raise SystemExit('Incomplete TLS key/certificate pair; repair /etc/vm-manager/tls')
    for path in tls.iterdir():
        os.chown(path, 0, account.pw_gid)
        path.chmod(0o640)
    run('runuser', '-u', 'vm-manager', '--', str(venv / 'bin/python'), '-c',
        "import sys;sys.path.insert(0,'/usr/lib/vm-manager');from pathlib import Path;"
        "from app.security import Store;p=Path('/var/lib/vm-manager');Store(p).bootstrap(p)")
    if systemd():
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', 'vm-manager-storage.service', 'vm-manager.service')
        print(network.configure_firewall(values))
        run('systemctl', 'restart', 'vm-manager-storage.service', 'vm-manager.service')
        context = ssl._create_unverified_context()  # Local generated certificate only.
        for attempt in range(30):
            try:
                # The configured bind address may be a specific host IP.
                host = '127.0.0.1'
                for line in (CONFIG / 'vm-manager.conf').read_text().splitlines():
                    if line.startswith('VM_MANAGER_HOST='):
                        host = line.split('=', 1)[1].strip().strip('\"\'')
                if host in ('0.0.0.0', '::'):
                    host = '127.0.0.1' if host == '0.0.0.0' else '::1'
                if ':' in host:
                    host = '[' + host + ']'
                with urllib.request.urlopen('https://' + host + ':8443/health', context=context, timeout=2) as response:
                    health = json.load(response)
                if health.get('status') == 'ok' and health.get('revision') == (SOURCE / 'REVISION').read_text().strip():
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise SystemExit('VM Manager health check failed. Inspect journalctl -u vm-manager; backups and previous venv retained.')
    network.show_status(values, verify=systemd())


if __name__ == '__main__':
    if sys.argv[1:] == ['pre-upgrade']:
        pre_upgrade()
    elif sys.argv[1:] == ['configure']:
        configure()
    elif sys.argv[1:] == ['remove']:
        access().remove_firewall()
    elif sys.argv[1:] in ([], ['status']):
        network = access()
        raise SystemExit(0 if network.show_status(network.settings()) else 1)
    else:
        raise SystemExit('Usage: vm-manager status (package lifecycle: pre-upgrade|configure|remove)')
