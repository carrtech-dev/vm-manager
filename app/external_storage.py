"""External source registry and guest-agent operations owned by the storage broker.

No source secrets are returned to the app. Every path and command is derived from
validated records; guest payloads and credentials are never command arguments.
"""
import base64
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .hypervisor import HypervisorError, Incus, name

REGISTRY = Path('/var/lib/vm-manager-storage/external.json')
CONFIG = Path('/var/lib/vm-manager-storage/external-config')
STAGING = Path('/var/lib/vm-manager/external-staging')
HOST_MOUNTS = Path('/var/lib/vm-manager-storage/mounts')
lock = threading.RLock()
host_mount_locks = {}


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=63)
    kind: Literal['smb', 's3']
    server: str = Field(default='', max_length=253)
    share: str = Field(default='', max_length=128)
    username: str = Field(default='', max_length=256)
    password: str = Field(default='', max_length=1024, repr=False)
    domain: str = Field(default='', max_length=256)
    port: int = Field(default=445, ge=1, le=65535)
    endpoint: str = Field(default='', max_length=1024)
    bucket: str = Field(default='', max_length=63)
    access_key: str = Field(default='', max_length=256, repr=False)
    secret_key: str = Field(default='', max_length=1024, repr=False)
    region: str = Field(default='us-east-1', max_length=128)
    prefix: str = Field(default='', max_length=512)
    readonly: bool = False

    @field_validator('*')
    @classmethod
    def no_control_characters(cls, value):
        if isinstance(value, str) and any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Control characters are not allowed')
        return value

    @model_validator(mode='after')
    def connection(self):
        if not self.name.strip():
            raise ValueError('Enter a source name')
        if self.prefix and (self.prefix.startswith('/') or '\\' in self.prefix or ',' in self.prefix or any(p in ('', '.', '..') for p in self.prefix.split('/'))):
            raise ValueError('Use a relative folder or prefix without empty, dot or parent components')
        if self.kind == 'smb':
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', self.server):
                raise ValueError('Enter a hostname or IPv4 address for the SMB server')
            if not re.fullmatch(r'[A-Za-z0-9_$][A-Za-z0-9_ .$-]*', self.share) or not self.username:
                raise ValueError('Enter a share name and username')
        else:
            parsed = urlsplit(self.endpoint)
            if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError('Enter an HTTP or HTTPS S3 endpoint without credentials, a path or query')
            if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket):
                raise ValueError('Enter a valid S3 bucket name')
        return self


class Mount(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    path: str = Field(min_length=6, max_length=240)
    readonly: bool = False

    @field_validator('path')
    @classmethod
    def mount_path(cls, value):
        if not re.fullmatch(r'/(mnt|media)/[A-Za-z0-9_-][A-Za-z0-9_./-]*', value) or any(p in ('', '.', '..') for p in value[1:].split('/')):
            raise ValueError('Choose a folder below /mnt or /media without dot or empty components')
        return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise HypervisorError('Invalid external storage reference')
    return value


def read():
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {'sources': [], 'mounts': []}


def save(state):
    REGISTRY.parent.mkdir(parents=True, mode=0o711, exist_ok=True)
    temporary = REGISTRY.with_suffix('.tmp')
    with open(temporary, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, REGISTRY)
    REGISTRY.chmod(0o600)


def source_record(source_id, state=None):
    identifier(source_id)
    source = next((s for s in (state or read())['sources'] if s['id'] == source_id), None)
    if source is None:
        raise HypervisorError('External storage source not found')
    return source


def public(source):
    return {k: v for k, v in source.items() if k not in ('password', 'access_key', 'secret_key')} | {
        'has_password': bool(source['password']), 'has_access_key': bool(source['access_key']),
        'has_secret_key': bool(source['secret_key'])}


def listing():
    state = read()
    return {'sources': [public(s) for s in state['sources']], 'mounts': state['mounts']}


def initialize():
    with lock:
        state = read()
        if any(m['status'] == 'configuring' for m in state['mounts']):
            for mount in state['mounts']:
                if mount['status'] == 'configuring':
                    mount['status'] = 'incomplete'
            save(state)


def create(body, source_id=None):
    state = read()
    previous = source_record(source_id, state) if source_id else None
    if previous:
        if any(m['source_id'] == source_id for m in state['mounts']):
            raise HypervisorError('Detach this source from its VMs before editing its connection')
        if body.get('kind') != previous['kind']:
            raise HypervisorError('A source type cannot be changed')
        body = dict(body)
        for key in ('password', 'access_key', 'secret_key'):
            if not body.get(key):
                body[key] = previous[key]
    spec = Source.model_validate(body)
    if spec.kind == 's3' and (not spec.access_key or not spec.secret_key):
        raise HypervisorError('Enter the S3 access key and secret key')
    if any(s['name'].casefold() == spec.name.casefold() and s['id'] != source_id for s in state['sources']):
        raise HypervisorError('An external source with this name already exists')
    source = spec.model_dump() | {'id': source_id or uuid.uuid4().hex, 'status': 'untested', 'error': '', 'tested': None}
    if previous:
        # Never discard a mounted host share/cache underneath an operation.
        host = HOST_MOUNTS / ('external-' + source_id)
        if os.path.ismount(host):
            safe_run('umount', str(host))
        state['sources'].remove(previous)
    state['sources'].append(source)
    save(state)
    return public(source)


def safe_run(*args, timeout=180, stdin=None):
    """Sanitize utility failures: never leak credentials or endpoints to task logs."""
    try:
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HypervisorError('External storage command failed or timed out: ' + args[0]) from exc
    if result.returncode:
        raise HypervisorError('External storage connection failed: ' + args[0] + '. Check credentials, endpoint and network access.')
    return result.stdout.strip()


def config(source):
    directory = CONFIG / source['id']
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    if source['kind'] == 'smb':
        path = directory / 'credentials'
        text = 'username=' + source['username'] + '\npassword=' + source['password'] + '\ndomain=' + source['domain'] + '\n'
    else:
        path = directory / 'rclone.conf'
        text = '[remote]\ntype = s3\nprovider = Other\naccess_key_id = ' + source['access_key'] + '\nsecret_access_key = ' + source['secret_key'] + '\nendpoint = ' + source['endpoint'] + '\nregion = ' + source['region'] + '\nforce_path_style = true\n'
    temporary = directory / ('.config-' + uuid.uuid4().hex)
    with open(temporary, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)
    return path


def rclone(source, *args):
    settings = str(config(source)) if source['kind'] == 's3' else '/dev/null'
    return safe_run('rclone', *args, '--config', settings, '--contimeout', '15s', '--timeout', '60s',
                    '--retries', '2', '--low-level-retries', '2', timeout=90 if args[0] == 'lsf' else 86400)


def remote(source):
    return 'remote:' + source['bucket'] + ('/' + source['prefix'] if source['prefix'] else '')


def test(source_id):
    with lock:
        source = dict(source_record(source_id))
    try:
        if source['kind'] == 'smb':
            # smbclient uses the same credential-file format as mount.cifs.
            safe_run('smbclient', '//' + source['server'] + '/' + source['share'],
                '-A', str(config(source)), '-p', str(source['port']), '-m', 'SMB3', '-D', source['prefix'] or '.', '-c', 'ls', timeout=30)
        else:
            rclone(source, 'lsf', remote(source), '--max-depth', '1')
        source.update(status='ready', error='', tested=time.time())
    except HypervisorError as exc:
        source.update(status='unavailable', error=str(exc), tested=time.time())
        with lock:
            state = read()
            source_record(source_id, state).update(status=source['status'], error=source['error'], tested=source['tested'])
            save(state)
        raise
    with lock:
        state = read()
        source_record(source_id, state).update(status=source['status'], error='', tested=source['tested'])
        save(state)
    return 'Connection verified: ' + source['name']


def delete(source_id, confirm_name):
    state = read()
    source = source_record(source_id, state)
    if confirm_name != source['name']:
        raise HypervisorError('Type the source name to confirm removal')
    if any(m['source_id'] == source_id for m in state['mounts']):
        raise HypervisorError('Detach this source from its VMs before removing it')
    staged = STAGING / source_id
    if staged.exists() and any(staged.rglob('manifest.json')):
        raise HypervisorError('Staged backup data remains; remove its backup records first')
    host = HOST_MOUNTS / ('external-' + source_id)
    if os.path.ismount(host):
        safe_run('umount', str(host))
    state['sources'].remove(source)
    save(state)
    # Remove only generated credential files. Remote data is never emptied.
    shutil.rmtree(CONFIG / source_id, ignore_errors=True)
    return 'External source removed; remote files retained'


def guest(vm_id, body):
    provider, sep, vm_name = vm_id.partition(':')
    if not sep or provider not in ('incus', 'libvirt'):
        raise HypervisorError('Invalid VM reference')
    name(vm_name)
    script = Path(__file__).with_name('guest_storage.py').read_text()
    if provider == 'incus':
        vm = Incus().vm(vm_name)
        if vm.get('type') != 'virtual-machine' or vm['status'].lower() != 'running':
            raise HypervisorError('Start the Linux VM before configuring its mounted storage')
        try:
            ready = time.monotonic() + 30
            while True:
                probe = subprocess.run(['incus', '--force-local', '--project', 'default', 'exec', vm_name,
                    '--mode=non-interactive', '--', '/usr/bin/true'], capture_output=True, timeout=10)
                if probe.returncode == 0:
                    break
                if time.monotonic() >= ready:
                    raise HypervisorError('Automatic mounting requires a running Linux VM with its Incus agent, Python 3 and systemd')
                time.sleep(1)
            result = subprocess.run(['incus', '--force-local', '--project', 'default', 'exec', vm_name,
                '--mode=non-interactive', '--', '/usr/bin/python3', '-c', script],
                input=json.dumps(body), capture_output=True, text=True, timeout=1100)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HypervisorError('Guest mount operation timed out; inspect the mount before retrying') from exc
        if result.returncode:
            # Only our controlled guest messages can be displayed, never CLI stderr.
            message = result.stdout.strip()
            if not message or len(message) > 1500:
                message = 'Automatic mounting requires a running Linux VM with its Incus agent, Python 3 and systemd'
            raise HypervisorError(message)
        return result.stdout.strip()
    # Use the distribution binding; secret stdin must never be a virsh argument.
    try:
        sys.path.append('/usr/lib/python3/dist-packages')
        import libvirt
        import libvirt_qemu
        connection = libvirt.open('qemu:///system')
        try:
            domain = connection.lookupByName(vm_name)
            if not domain.isActive():
                raise HypervisorError('Start the Linux VM before configuring its mounted storage')
            def agent(command):
                return json.loads(libvirt_qemu.qemuAgentCommand(domain, json.dumps(command), 15, 0))['return']
            ready = time.monotonic() + 30
            while True:
                try:
                    agent({'execute': 'guest-ping'})
                    break
                except Exception:
                    if time.monotonic() >= ready:
                        raise
                    time.sleep(1)
            process = agent({'execute': 'guest-exec', 'arguments': {'path': '/usr/bin/python3', 'arg': ['-c', script],
                'input-data': base64.b64encode(json.dumps(body).encode()).decode(), 'capture-output': True}})
            deadline = time.monotonic() + 1100
            while time.monotonic() < deadline:
                status = agent({'execute': 'guest-exec-status', 'arguments': {'pid': process['pid']}})
                if status.get('exited'):
                    output = base64.b64decode(status.get('out-data', '')).decode(errors='replace').strip()
                    if status.get('exitcode', 1):
                        raise HypervisorError(output[:1500] or 'Guest storage setup failed; Python 3 and systemd are required')
                    return output
                time.sleep(.5)
            raise HypervisorError('Guest operation timed out; inspect the mount before retrying')
        finally:
            connection.close()
    except HypervisorError:
        raise
    except Exception as exc:
        raise HypervisorError('Automatic mounting requires a running Linux VM with an enabled QEMU guest agent, Python 3 and systemd') from exc


def attach(vm_id, body):
    with lock:
        state = read()
        spec = Mount.model_validate(body)
        source = source_record(spec.source_id, state)
        # Prevent nested mount points and accidental covering of another attachment.
        existing = [m for m in state['mounts'] if m['vm_id'] == vm_id]
        mount = next((m for m in existing if m['path'] == spec.path), None)
        if mount and (mount['source_id'] != spec.source_id or mount['readonly'] != (spec.readonly or source['readonly'])):
            raise HypervisorError('Detach the existing mount at this path first')
        if any(m['path'] != spec.path and (m['path'].startswith(spec.path + '/') or spec.path.startswith(m['path'] + '/')) for m in existing):
            raise HypervisorError('Mounted storage paths cannot overlap')
        if not mount:
            mount = spec.model_dump() | {'id': uuid.uuid4().hex, 'vm_id': vm_id, 'readonly': spec.readonly or source['readonly'], 'status': 'configuring', 'checked': None}
            state['mounts'].append(mount)
        mount['status'] = 'configuring'
        save(state)  # Retain intent and ID if guest setup is interrupted.
    try:
        message = guest(vm_id, mount | {'operation': 'attach', 'source': source,
            'runner': Path(__file__).with_name('mount_runner.py').read_text()})
        mount.update(status='mounted', checked=time.time())
    except Exception:
        mount['status'] = 'incomplete'
        update_mount(mount)
        raise
    update_mount(mount)
    return message


def update_mount(mount, remove=False):
    with lock:
        state = read()
        state['mounts'] = [m for m in state['mounts'] if m['id'] != mount['id']]
        if not remove:
            state['mounts'].append(mount)
        save(state)


def mount_operation(vm_id, mount_id, operation):
    state = read()
    identifier(mount_id)
    mount = next((m for m in state['mounts'] if m['id'] == mount_id and m['vm_id'] == vm_id), None)
    if not mount:
        raise HypervisorError('VM storage attachment not found')
    try:
        result = guest(vm_id, mount | {'operation': operation})
    except Exception:
        mount['status'] = 'unavailable'
        update_mount(mount)
        raise
    if operation != 'detach':
        mount.update(status='mounted', checked=time.time())
    update_mount(mount, remove=operation == 'detach')
    return result


def backup_directory(source_id):
    source = source_record(source_id)
    if source['readonly']:
        raise HypervisorError('Read-only sources cannot store backups')
    # Validate connectivity before consuming local staging space; no silent
    # substitution of a missing remote by an empty local mountpoint.
    test(source_id)
    directory = STAGING / source_id
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.resolve() != directory.absolute():
        raise HypervisorError('Unexpected external backup staging path')
    account = pwd.getpwnam('vm-manager')
    os.chown(directory, account.pw_uid, account.pw_gid)
    return str(directory)


def backup_remote(source):
    if source['kind'] == 's3':
        return remote(source) + '/vm-manager-backups'
    with lock:
        mount_lock = host_mount_locks.setdefault(source['id'], threading.Lock())
    with mount_lock:
        return smb_backup_remote(source)


def smb_backup_remote(source):
    path = HOST_MOUNTS / ('external-' + source['id'])
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not os.path.ismount(path):
        options = ['credentials=' + str(config(source)), 'vers=3.0', 'port=' + str(source['port']), 'nosuid', 'nodev', 'noexec']
        if source['prefix']:
            options.append('prefixpath=' + source['prefix'])
        safe_run('mount.cifs', '//' + source['server'] + '/' + source['share'], str(path), '-o', ','.join(options), timeout=45)
    if not os.path.ismount(path):
        raise HypervisorError('External backup share is not mounted')
    return str(path / 'vm-manager-backups')


def backup_transfer(source_id, backup_id, operation):
    source = source_record(source_id)
    identifier(backup_id)
    path = STAGING / source_id / 'vm-backups' / backup_id
    if path.resolve() != path.absolute():
        raise HypervisorError('Unexpected backup staging path')
    remote_path = backup_remote(source) + '/' + backup_id
    if operation == 'push':
        if not (path / 'manifest.json').is_file() or any(p.is_symlink() for p in path.rglob('*')):
            raise HypervisorError('Backup staging is incomplete or contains symbolic links')
        rclone(source, 'copy', str(path), remote_path, '--exclude', '/manifest.json')
        # Download checks verify the actual remote bytes, including multipart S3
        # objects whose ETag cannot be used as a content checksum.
        rclone(source, 'check', str(path), remote_path, '--download', '--one-way', '--exclude', '/manifest.json')
        rclone(source, 'copyto', str(path / 'manifest.json'), remote_path + '/manifest.json')
        rclone(source, 'check', str(path), remote_path, '--download', '--one-way')
    elif operation == 'pull':
        # Always fetch a fresh remote copy; a stale local cache is not proof that
        # a remote backup still exists or passes its checksums.
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, mode=0o700)
        rclone(source, 'copy', remote_path, str(path))
        account = pwd.getpwnam('vm-manager')
        for item in [path.parent, path, *path.rglob('*')]:
            if item.is_symlink():
                raise HypervisorError('Unexpected symbolic link in remote backup')
            os.chown(item, account.pw_uid, account.pw_gid)
            item.chmod(0o700 if item.is_dir() else 0o600)
        if not (path / 'manifest.json').is_file():
            raise HypervisorError('Remote backup is missing its completion manifest')
    elif operation == 'delete':
        if source['readonly']:
            raise HypervisorError('Read-only sources cannot delete backups')
        rclone(source, 'purge', remote_path)
        if path.exists():
            shutil.rmtree(path)
    return 'Remote backup ' + operation + ' completed'


def dispatch(operation, request):
    if operation == 'external_test':
        return test(identifier(request['source_id']))
    if operation == 'external_attach':
        return attach(request['vm_id'], request['body'])
    if operation in ('external_detach', 'external_status'):
        return mount_operation(request['vm_id'], request['mount_id'], operation.removeprefix('external_'))
    if operation == 'external_backup_directory':
        return backup_directory(identifier(request['source_id']))
    with lock:
        if operation == 'external_list':
            return listing()
        if operation == 'external_create':
            return create(request['body'])
        if operation == 'external_update':
            return create(request['body'], identifier(request['source_id']))
        if operation == 'external_delete':
            return delete(identifier(request['source_id']), request['confirm_name'])
    # Transfers may run concurrently across VMs. Host-wide edits are excluded by
    # the app task queue; do not hold the registry lock during a long upload.
    if operation in ('external_backup_push', 'external_backup_pull', 'external_backup_delete'):
        return backup_transfer(identifier(request['source_id']), identifier(request['backup_id']), operation.rsplit('_', 1)[1])
    raise HypervisorError('Unsupported external storage operation')
