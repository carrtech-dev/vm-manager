"""Persistent backup plans and offline, independently restorable VM archives."""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from . import storage
from .hypervisor import HypervisorError, name, run
from .power import PowerInterrupted


class Plan(BaseModel):
    block_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    enabled: bool = False
    frequency: Literal["daily", "weekly"] = "daily"
    time: str = Field(default="02:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(default="Europe/London", max_length=100)
    weekday: int = Field(default=6, ge=0, le=6)
    keep: int = Field(default=7, ge=1, le=365)
    shutdown: bool = False

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
        return [dict(r) for r in db.execute("SELECT * FROM vm_backups ORDER BY created DESC")]


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


class Manager:
    def __init__(self, store, data, incus, libvirt, power_requests, power_action):
        self.store, self.data = store, data
        self.incus, self.libvirt = incus, libvirt
        self.power_requests, self.power_action = power_requests, power_action
        self.lock = threading.RLock()

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

    def create(self, backup_id, shutdown, cancelled):
        row = self.record(backup_id)
        vm_id = row['vm_id']
        provider, vm_name = vm_id.split(':', 1)
        driver = self.incus if provider == 'incus' else self.libvirt
        restart, complete, error = False, False, None
        self.update(backup_id, 'running')
        try:
            # Validate before disrupting the guest; hold storage lock throughout to
            # prevent block deletion/default changes while exporting.
            with storage.lock:
                path = archive_dir(self.store, self.data, row, create=True)
                status = self.status(provider, vm_name)
                if status not in ('stopped', 'shut off'):
                    if status != 'running' or not shutdown:
                        raise HypervisorError('VM must be stopped, or enable graceful shutdown and restart for backups')
                    restart = True
                    self.power_action(vm_id, 'shutdown', cancelled)
                self.power_requests.check(cancelled)
                manifest = {'version': 1, 'provider': provider, 'vm_id': vm_id, 'created': row['created']}
                if provider == 'incus':
                    self.export_incus(vm_name, path, manifest)
                else:
                    manifest['libvirt'] = storage.broker('libvirt_export', block_id=row['block_id'], backup_id=backup_id, vm_name=vm_name)
                if self.status(provider, vm_name) not in ('stopped', 'shut off'):
                    raise HypervisorError('VM power changed during backup; archive cannot be trusted')
                self.power_requests.check(cancelled)
                manifest['files'] = {str(p.relative_to(path)): {'size': p.stat().st_size, 'sha256': digest(p)}
                                     for p in path.rglob('*') if p.is_file()}
                (path / 'manifest.json').write_text(json.dumps(manifest, indent=2))
                # Flush data before making a restore point visible.
                for file in path.rglob('*'):
                    if file.is_file():
                        file.chmod(0o600)
                        with file.open('rb') as stream:
                            os.fsync(stream.fileno())
                size = sum(f['size'] for f in manifest['files'].values())
                self.update(backup_id, 'completed', 'Verified SHA-256; full offline backup', size)
                complete = True
        except Exception as exc:
            error = exc
            self.update(backup_id, 'interrupted' if isinstance(exc, PowerInterrupted) else 'failed', str(exc))
        finally:
            if restart:
                try:
                    # Restart the same hardware; pending edits remain pending.
                    # An emergency Force stop cancels this automatic restart.
                    with self.power_requests.transition(vm_id, cancelled):
                        if self.status(provider, vm_name) in ('stopped', 'shut off'):
                            driver.action(vm_name, 'start')
                except Exception as exc:
                    if complete:
                        self.update(backup_id, 'completed', 'Backup complete; automatic restart failed: ' + str(exc), size)
                    if not isinstance(error, PowerInterrupted):
                        error = exc if isinstance(exc, PowerInterrupted) else HypervisorError((str(error) + '; ' if error else '') + 'Automatic restart failed: ' + str(exc))
        if error:
            raise error
        return 'Backup completed: ' + backup_id

    def export_incus(self, vm_name, path, manifest):
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
            if not pool and device.get('readonly') == 'true' and source and source.lower().endswith('.iso'):
                media.append(key)
                continue
            if not pool or not source or '/' in source:
                raise HypervisorError('Host-path disks require manual backup: ' + key)
            volume = self.incus.get(f'/1.0/storage-pools/{pool}/volumes/custom/{source}')
            if volume.get('content_type') == 'iso':
                media.append(key)
                continue
            if len(volume.get('used_by', [])) > 1:
                raise HypervisorError('Shared volumes require manual backup: ' + key)
            filename = 'volume-' + str(len(volumes)) + '.tar.gz'
            volumes.append(dict(device=key, pool=pool, source=source, file=filename))
        # Portable exports include root disk/firmware. Snapshots and ISO media
        # are excluded; each attached writable volume is exported separately.
        self.cli('export', vm_name, str(path / 'instance.tar.gz'), '--instance-only', '--compression=gzip')
        for volume in volumes:
            self.cli('storage', 'volume', 'export', volume['pool'], volume['source'], str(path / volume['file']), '--volume-only', '--compression=gzip')
        manifest.update(config=vm, volumes=volumes, media=media)

    def delete(self, backup_id):
        with self.lock, storage.lock:
            row = self.record(backup_id)
            if row['status'] in ('queued', 'running'):
                raise HypervisorError('Wait for the backup to finish')
            path = archive_dir(self.store, self.data, row)
            if path.exists():
                shutil.rmtree(path)
            with self.store.db() as db:
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
        with self.lock, storage.lock:
            row = self.record(backup_id)
            if row['status'] != 'completed':
                raise HypervisorError('Only completed backups can be restored')
            path = archive_dir(self.store, self.data, row)
            manifest = self.verify(path)
            if manifest['provider'] == 'incus':
                self.restore_incus(path, manifest, spec)
            else:
                self.restore_libvirt(path, manifest, spec)
        return 'Restored as ' + spec.name + ' (stopped). Check guest identity and network settings before starting.'

    def restore_incus(self, path, manifest, spec):
        if not spec.pool:
            raise HypervisorError('Choose a VM storage pool')
        storage.check_vm_pool(spec.pool)
        if any(v['name'] == spec.name for v in self.incus.vms()):
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
            new_name = 'vmm-' + spec.name[:35] + '-restore-' + uuid.uuid4().hex[:12] + '-' + str(i)
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
            vm['config'].update({'boot.autostart': 'false', 'user.vm-manager': 'true'})
        self.incus.update(spec.name, transform)

    def restore_libvirt(self, path, manifest, spec):
        from .backup_libvirt import restore
        restore(self.data, self.libvirt, path / 'libvirt', manifest['libvirt'], spec.name)
