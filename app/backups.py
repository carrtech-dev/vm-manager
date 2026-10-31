"""Persistent backup plans and independently restorable VM archives."""

import hashlib
import gzip
import io
import json
import os
import re
import shutil
import subprocess

import tarfile
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from pydantic import BaseModel, Field, field_validator

from .task_output import emit
from . import storage
from .hypervisor import HypervisorError, HypervisorNotFound, name, run
from .power import PowerInterrupted
from .protection import ProtectionPower
from .snapshots import NO_EXPIRY, Snapshots


class Plan(BaseModel):
    block_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    enabled: bool = False
    frequency: Literal["daily", "weekly"] = "daily"
    time: str = Field(default="02:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(default="Europe/London", max_length=100)
    weekday: int = Field(default=6, ge=0, le=6)
    keep: int = Field(default=7, ge=1, le=365)
    shutdown: bool = False
    mode: Literal["live", "shutdown", "stopped"] | None = None

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose a valid IANA timezone, for example Europe/London") from exc
        return value


class BackupRequest(BaseModel):
    vm_ids: list[str] = Field(min_length=1, max_length=200)
    block_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    shutdown: bool = False
    mode: Literal["live", "shutdown", "stopped"] | None = None


class PlanRequest(Plan):
    vm_ids: list[str] = Field(min_length=1, max_length=200)


class Restore(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    pool: str = Field(default="", max_length=63)

    @field_validator("name", "pool")
    @classmethod
    def valid_name(cls, value):
        if value:
            try:
                return name(value)
            except HypervisorError as exc:
                raise ValueError(str(exc)) from exc
        return value


def initialize(store):
    with store.db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS backup_plans(vm_id TEXT PRIMARY KEY,
                settings TEXT NOT NULL, next_run REAL, last_task TEXT);
            CREATE TABLE IF NOT EXISTS vm_backups(id TEXT PRIMARY KEY, vm_id TEXT NOT NULL,
                block_id TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                finished REAL, size INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                task_id TEXT, trigger TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS backup_one_active ON vm_backups(vm_id)
                WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS vm_templates(id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE, description TEXT NOT NULL DEFAULT '');
        """)
        db.execute("UPDATE vm_backups SET status='interrupted', finished=?, message='Service restarted. Partial files are retained; delete this entry before retrying if space is needed.' WHERE status IN ('queued','running')", (time.time(),))


def next_run(plan, after):
    """One run per local calendar day, including clock changes; no DST double run."""
    zone = ZoneInfo(plan.timezone)
    local = datetime.fromtimestamp(after, zone)
    hour, minute = map(int, plan.time.split(":"))
    for offset in range(9):
        date = local.date() + timedelta(days=offset)
        if plan.frequency == "weekly" and date.weekday() != plan.weekday:
            continue
        candidate = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone).timestamp()
        # Nonexistent spring times normalize forward to the first valid instant.
        if candidate > after:
            return candidate
    raise ValueError("Unable to calculate next backup")


def plans(store):
    with store.db() as db:
        return [dict(json.loads(r['settings']), vm_id=r['vm_id'], next_run=r['next_run'], last_task=r['last_task'])
                for r in db.execute("SELECT * FROM backup_plans ORDER BY vm_id")]


def catalog(store):
    with store.db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM vm_backups WHERE trigger!='template' ORDER BY created DESC")]


def destination(store, data, block_id):
    # Full guest backups always require an explicitly selected backup block.
    if not re.fullmatch(r"[0-9a-f]{32}", block_id):
        raise HypervisorError("Choose a backup storage block")
    return storage.directory(store, data, "backup", block_id)


def archive_dir(store, data, row, create=False):
    base = destination(store, data, row['block_id']) / 'vm-backups'
    if create:
        base.mkdir(mode=0o700, exist_ok=True)
    path = base / row['id']
    if path.resolve() != path.absolute() or base.is_symlink():
        raise HypervisorError("Unexpected backup path; manual inspection required")
    if create:
        path.mkdir(mode=0o700)
    return path


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b''):
            result.update(chunk)
    return result.hexdigest()


def external_destination(path):
    # External destinations always stage under this generated application path.
    # Avoid probing the broker for ordinary local backups or test destinations.
    return path.parent.parent.parent.name == 'external-staging'


class ArchiveLoader(yaml.SafeLoader):
    """Keep RFC3339 timestamp scalars intact for the Incus Go decoder."""


ArchiveLoader.add_constructor('tag:yaml.org,2002:timestamp', lambda loader, node: loader.construct_scalar(node))


def portable_instance(source, target):
    """Stream the root archive with a standalone import configuration.

    The original full configuration lives in our manifest. Incus validates
    attached custom volumes during import, before we can replace them, so both
    copies of its import metadata must initially reference only the root disk.
    """
    patched = set()
    with tarfile.open(fileobj=source, mode='r|*') as incoming, gzip.GzipFile(fileobj=target, mode='wb', compresslevel=1) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as outgoing:
            for member in incoming:
                content = incoming.extractfile(member) if member.isfile() else None
                if member.name in ('backup/index.yaml', 'backup/virtual-machine/backup.yaml'):
                    if member.size > 4 * 1024**2:
                        raise HypervisorError('Incus backup metadata is too large')
                    document = yaml.load(content.read(), Loader=ArchiveLoader)
                    config = document['config'] if member.name == 'backup/index.yaml' else document
                    vm = config.get('container', config.get('instance'))
                    if not isinstance(vm, dict):
                        raise HypervisorError('Unsupported Incus backup metadata')
                    devices = vm.get('expanded_devices', vm.get('devices', {}))
                    root = {k: d for k, d in devices.items() if d.get('type') == 'disk' and d.get('path') == '/'}
                    if len(root) != 1:
                        raise HypervisorError('Incus backup does not have one root disk')
                    vm.update(devices=root, expanded_devices=root, profiles=[])
                    # Import must never autostart a partially restored guest.
                    conf = vm.get('expanded_config', vm.get('config', {}))
                    conf = {k: v for k, v in conf.items() if not k.startswith('volatile.')}
                    conf['boot.autostart'] = 'false'
                    vm.update(config=conf, expanded_config=conf)
                    payload = yaml.safe_dump(document).encode()
                    member.size = len(payload)
                    content = io.BytesIO(payload)
                    patched.add(member.name)
                # extractfile expands holes; remove sparse headers when writing
                # a portable regular entry, keeping the reconstructed disk bytes.
                if member.sparse is not None:
                    member.sparse = None
                    member.type = tarfile.REGTYPE
                    member.pax_headers = {k: v for k, v in member.pax_headers.items() if not k.startswith('GNU.sparse.')}
                outgoing.addfile(member, content)
    if patched != {'backup/index.yaml', 'backup/virtual-machine/backup.yaml'}:
        raise HypervisorError('Unsupported Incus archive: import metadata is missing')


class Manager:
    def __init__(self, store, data, incus, libvirt, power_requests, power_action):
        self.store, self.data = store, data
        self.incus, self.libvirt = incus, libvirt
        self.power_requests, self.power_action = power_requests, power_action
        self.lock = threading.RLock()
        self.power = ProtectionPower(incus, libvirt, power_requests, power_action)

    def record(self, backup_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM vm_backups WHERE id=?", (backup_id,)).fetchone()
        if not row:
            raise HypervisorError("Backup not found")
        return dict(row)

    def update(self, backup_id, status, message='', size=0):
        with self.store.db() as db:
            db.execute("UPDATE vm_backups SET status=?,message=?,size=?,finished=? WHERE id=?",
                       (status, message[:2000], size, time.time() if status not in ('queued','running') else None, backup_id))

    def status(self, provider, vm_name):
        return (self.incus.vm(vm_name)['status'].lower() if provider == 'incus'
                else self.libvirt.cli('domstate', vm_name).strip())

    @staticmethod
    def cli(*args):
        return run('incus', '--force-local', '--quiet', '--project', 'default', *args, timeout=86400)

    def create(self, backup_id, shutdown, cancelled, mode=None):
        row = self.record(backup_id)
        vm_id = row['vm_id']
        provider, vm_name = vm_id.split(':', 1)
        # Legacy schedules retain their existing stopped/shutdown policy.
        mode = mode or ('shutdown' if shutdown else 'stopped')
        self.power.power_action = self.power_action
        self.power.status = self.status
        self.update(backup_id, 'running')
        complete, size = False, 0
        try:
            with storage.lock:
                path = archive_dir(self.store, self.data, row, create=True)
            with self.power.operation(vm_id, mode, cancelled) as live:
                manifest = {'version': 1, 'provider': provider, 'vm_id': vm_id,
                            'created': row['created'], 'mode': mode,
                            'consistency': 'crash-consistent' if live else 'offline'}
                if provider == 'incus':
                    if live:
                        self.export_incus_live(vm_name, path, manifest, cancelled)
                    else:
                        self.export_incus(vm_name, path, manifest)
                else:
                    manifest['libvirt'] = storage.broker('libvirt_export', block_id=row['block_id'],
                        backup_id=backup_id, vm_name=vm_name, live=live)
                if self.status(provider, vm_name) not in (('running',) if live else ('stopped', 'shut off')):
                    raise HypervisorError('VM power changed during backup; archive cannot be trusted')
                self.power_requests.check(cancelled)
                manifest['files'] = {str(p.relative_to(path)): {'size': p.stat().st_size, 'sha256': digest(p)}
                                     for p in path.rglob('*') if p.is_file() and p.name != 'capture.json'}
                (path / 'manifest.json').write_text(json.dumps(manifest, indent=2))
                for file in path.rglob('*'):
                    if file.is_file():
                        file.chmod(0o600)
                        with file.open('rb') as stream:
                            os.fsync(stream.fileno())
                size = sum(f['size'] for f in manifest['files'].values())
                if external_destination(path):
                    storage.broker('external_backup_push', source_id=row['block_id'], backup_id=backup_id)
                message = 'Verified SHA-256; ' + ('live crash-consistent backup' if live else 'full offline backup')
                self.update(backup_id, 'completed', message, size)
                complete = True
                if external_destination(path):
                    shutil.rmtree(path)
        except Exception as exc:
            if complete:
                self.update(backup_id, 'completed', 'Backup complete; automatic restart failed: ' + str(exc), size)
            else:
                self.update(backup_id, 'interrupted' if isinstance(exc, PowerInterrupted) else 'failed', str(exc))
            raise
        return 'Backup completed: ' + backup_id

    def export_root(self, vm_name, target):
        emit("Incus: exporting VM archive to backup storage\n")
        # Stream directly to selected storage. Rewriting import metadata requires
        # no second full archive or temporary guest data on the application disk.
        with tempfile.TemporaryFile() as errors, target.open('xb') as output:
            process = subprocess.Popen(['incus', '--force-local', '--quiet', '--project', 'default', 'export', vm_name, '-', '--instance-only', '--compression=gzip'], stdout=subprocess.PIPE, stderr=errors)
            timer = threading.Timer(86400, process.kill)
            timer.daemon = True
            timer.start()
            try:
                portable_instance(process.stdout, output)
                while process.stdout.read(65536):
                    pass
                if process.wait():
                    errors.seek(0)
                    raise HypervisorError(errors.read(2000).decode(errors='replace') or 'Incus export failed')
            except Exception as exc:
                process.kill()
                process.wait()
                errors.seek(0)
                detail = errors.read(2000).decode(errors='replace').strip()
                if detail:
                    raise HypervisorError(detail) from exc
                raise
            finally:
                timer.cancel()
                process.stdout.close()

        emit("Incus: VM archive export completed\n")

    def inspect_incus(self, vm_name):
        vm = self.incus.vm(vm_name)
        if vm.get('ephemeral') or vm.get('stateful'):
            raise HypervisorError('Ephemeral or stateful VMs require manual backup')
        devices = vm.get('expanded_devices', vm.get('devices', {}))
        volumes, media = [], []
        for key, device in devices.items():
            if device.get('type') in ('unix-block', 'unix-char', 'gpu', 'pci', 'usb'):
                raise HypervisorError('Passthrough devices require manual backup: ' + key)
            if device.get('type') != 'disk' or device.get('path') == '/':
                continue
            pool, source = device.get('pool'), device.get('source')
            if source == 'agent:config':
                continue  # Incus regenerates this integration CD on restore/start.
            if not pool and device.get('readonly') == 'true' and source and source.lower().endswith('.iso'):
                media.append(key)
                continue
            if not pool or not source or '/' in source:
                raise HypervisorError('Host-path disks require manual backup: ' + key)
            volume = self.incus.get(f'/1.0/storage-pools/{pool}/volumes/custom/{source}')
            if volume.get('content_type') == 'iso':
                media.append(key)
                continue
            own = '/1.0/instances/' + vm_name
            if any(ref != own and not ref.startswith(own + '/snapshots/') for ref in volume.get('used_by', [])):
                raise HypervisorError('Shared volumes require manual backup: ' + key)
            filename = 'volume-' + str(len(volumes)) + '.tar.gz'
            volumes.append(dict(device=key, pool=pool, source=source, file=filename))
        return vm, volumes, media

    def export_incus(self, vm_name, path, manifest):
        vm, volumes, media = self.inspect_incus(vm_name)
        # Portable exports include root disk/firmware. Snapshots and ISO media
        # are excluded; each attached writable volume is exported separately.
        self.export_root(vm_name, path / 'instance.tar.gz')
        for volume in volumes:
            self.cli('storage', 'volume', 'export', volume['pool'], volume['source'], str(path / volume['file']), '--volume-only', '--compression=gzip')
        manifest.update(config=vm, volumes=volumes, media=media)

    def export_incus_live(self, vm_name, path, manifest, cancelled):
        vm, volumes, media = self.inspect_incus(vm_name)
        Snapshots(self.store, self.incus, self.libvirt).plan('incus', vm_name)
        suffix = path.name
        temporary = 'vmm-backup-' + suffix
        instance_url = '/1.0/instances/' + vm_name
        snapshot_url = instance_url + '/snapshots/' + temporary
        devices = vm.get('expanded_devices', vm.get('devices', {}))
        root = {key: device for key, device in devices.items()
                if device.get('type') == 'disk' and device.get('path') == '/'}
        if len(root) != 1:
            raise HypervisorError('The VM must have one managed root disk')
        # Deduplicate volumes attached through multiple device names.
        copies = {}
        for volume in volumes:
            key = (volume['pool'], volume['source'])
            copies.setdefault(key, temporary + '-' + str(len(copies)))
        volume_urls = [f'/1.0/storage-pools/{pool}/volumes/custom/{source}/snapshots/{temporary}'
                       for pool, source in copies]
        copy_urls = [f'/1.0/storage-pools/{pool}/volumes/custom/{copy}' for (pool, _), copy in copies.items()]
        clone_url = '/1.0/instances/' + temporary
        cleanup = [clone_url, *copy_urls, *volume_urls, snapshot_url]
        # Persist cleanup references before side effects, including service loss.
        recovery = path / 'capture.json'
        recovery.write_text(json.dumps({'source': vm_name, 'resources': cleanup}, indent=2))
        with recovery.open('rb') as stream:
            os.fsync(stream.fileno())
        try:
            with self.power.pause_incus(vm_name, cancelled):
                self.incus.request('POST', instance_url + '/snapshots',
                                   {'name': temporary, 'stateful': False, 'expires_at': NO_EXPIRY})
                for pool, source in copies:
                    self.incus.request('POST', f'/1.0/storage-pools/{pool}/volumes/custom/{source}/snapshots',
                                       {'name': temporary, 'expires_at': NO_EXPIRY})
            self.power_requests.check(cancelled)
            # Export immutable copies while the original VM continues running.
            overrides = {key: {'type': 'none'} for key in devices if key not in root}
            self.incus.request('POST', '/1.0/instances', {
                'name': temporary, 'type': 'virtual-machine', 'profiles': [],
                'config': {'boot.autostart': 'false'}, 'devices': overrides | root,
                'source': {'type': 'copy', 'source': vm_name + '/' + temporary}})
            self.export_root(temporary, path / 'instance.tar.gz')
            for (pool, source), copy in copies.items():
                self.power_requests.check(cancelled)
                self.cli('storage', 'volume', 'copy', pool + '/' + source + '/' + temporary, pool + '/' + copy)
            for volume in volumes:
                self.power_requests.check(cancelled)
                copy = copies[(volume['pool'], volume['source'])]
                self.cli('storage', 'volume', 'export', volume['pool'], copy,
                         str(path / volume['file']), '--volume-only', '--compression=gzip')
            manifest.update(config=vm, volumes=volumes, media=media)
        finally:
            errors = []
            for resource in cleanup:
                try:
                    self.incus.request('DELETE', resource)
                except HypervisorNotFound:
                    pass
                except Exception as exc:
                    errors.append(resource + ': ' + str(exc))
            if errors:
                raise HypervisorError('Temporary backup cleanup failed; inspect capture.json: ' + '; '.join(errors))
            recovery.unlink()

    def delete(self, backup_id):
        with self.lock, storage.lock:
            row = self.record(backup_id)
            if row['status'] in ('queued', 'running'):
                raise HypervisorError('Wait for the backup to finish')
            path = archive_dir(self.store, self.data, row)
            if external_destination(path):
                storage.broker('external_backup_delete', source_id=row['block_id'], backup_id=backup_id)
            if path.exists():
                shutil.rmtree(path)
            with self.store.db() as db:
                db.execute('DELETE FROM vm_templates WHERE id=?', (backup_id,))
                db.execute('DELETE FROM vm_backups WHERE id=?', (backup_id,))
            # Leave the mount empty so that storage deletion remains possible.
            if path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        return 'Backup deleted'

    def retain(self, vm_id, block_id, keep):
        with self.store.db() as db:
            rows = db.execute("SELECT id FROM vm_backups WHERE vm_id=? AND block_id=? AND status='completed' AND trigger='scheduled' ORDER BY created DESC", (vm_id, block_id)).fetchall()
        for row in rows[keep:]:
            self.delete(row['id'])
            self.store.audit('scheduler', 'backup.retention', row['id'], vm_id)

    def verify(self, path):
        manifest = json.loads((path / 'manifest.json').read_text())
        if manifest.get('version') != 1 or not manifest.get('files'):
            raise HypervisorError('Unsupported or incomplete archive')
        for filename, expected in manifest['files'].items():
            file = path / filename
            if file.resolve().parent not in (path.resolve(), (path / 'libvirt').resolve()) or file.is_symlink() or not file.is_file():
                raise HypervisorError('Backup file is missing or invalid: ' + filename)
            if file.stat().st_size != expected['size'] or digest(file) != expected['sha256']:
                raise HypervisorError('Backup checksum mismatch: ' + filename)
        return manifest

    def restore(self, backup_id, spec):
        # Restore and deletion share the source VM queue key. The
        # catalog keeps the block referenced while restore reads its archive.
        with storage.lock:
            row = self.record(backup_id)
            if row['status'] != 'completed':
                raise HypervisorError('Only completed backups can be restored')
            path = archive_dir(self.store, self.data, row)
            if external_destination(path):
                storage.broker('external_backup_pull', source_id=row['block_id'], backup_id=backup_id)
        manifest = self.verify(path)
        if manifest['provider'] == 'incus':
            self.restore_incus(path, manifest, spec)
        else:
            self.restore_libvirt(path, manifest, spec)
        if external_destination(path):
            shutil.rmtree(path)
        return 'Restored as ' + spec.name + ' (stopped). Check guest identity and network settings before starting.'

    def restore_incus(self, path, manifest, spec):
        if not spec.pool:
            raise HypervisorError('Choose a VM storage pool')
        storage.check_vm_pool(spec.pool)
        try:
            self.incus.vm(spec.name)
        except HypervisorNotFound:
            pass
        else:
            raise HypervisorError('A VM with this name already exists')
        # Never auto-delete on failed import: the operator can inspect partial
        # resources using the unique destination name and task message.
        self.cli('import', str(path / 'instance.tar.gz'), spec.name, '--storage', spec.pool)
        # Remove references to original custom volumes before importing any new
        # disks; a partial restore must never expose the source VM's data disks.
        def isolate(vm):
            vm['profiles'] = []
            vm['devices'] = {k: dict(d, pool=spec.pool) for k, d in manifest['config'].get('expanded_devices', manifest['config']['devices']).items() if d.get('type') == 'disk' and d.get('path') == '/'}
            vm['config']['boot.autostart'] = 'false'
        try:
            self.incus.update(spec.name, isolate)
        except Exception:
            self.incus.request('DELETE', '/1.0/instances/' + spec.name)
            raise
        devices = json.loads(json.dumps(manifest['config'].get('expanded_devices', manifest['config']['devices'])))
        for key in manifest['media']:
            devices.pop(key, None)
        for i, volume in enumerate(manifest['volumes']):
            new_name = 'vmm-' + spec.name + '-restore-' + uuid.uuid4().hex[:12] + '-' + str(i)
            self.cli('storage', 'volume', 'import', spec.pool, str(path / volume['file']), new_name)
            devices[volume['device']].update(pool=spec.pool, source=new_name)
        for device in devices.values():
            if device.get('type') == 'disk' and device.get('path') == '/':
                device['pool'] = spec.pool
            if device.get('type') == 'nic':
                device.pop('hwaddr', None)
                device.pop('host_name', None)
        def transform(vm):
            vm['profiles'] = []
            vm['devices'] = devices
            vm['config'] = {k: v for k, v in manifest['config'].get('expanded_config', manifest['config']['config']).items()
                            if not k.startswith('volatile.')}
            identity = str(uuid.uuid4())
            vm['config'].update({'boot.autostart': 'false', 'user.vm-manager': 'true',
                                 'volatile.uuid': identity, 'volatile.uuid.generation': identity})
        self.incus.update(spec.name, transform)

    def restore_libvirt(self, path, manifest, spec):
        from .backup_libvirt import restore
        restore(self.data, self.libvirt, path / 'libvirt', manifest['libvirt'], spec.name)
