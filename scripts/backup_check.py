"""Opt-in deployed VM backup/restore acceptance. Run as root on the host.

Creates only uniquely named temporary resources. Existing VMs, defaults, accounts
and backup plans are retained. Uses real disk markers and restores both providers.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import httpx

from app.hypervisor import Incus, Libvirt, run
from app.security import hash_password, token_hash


def main():
    prefix = 'vmm-backup-check-' + uuid.uuid4().hex[:8]
    source, restored = prefix + '-src', prefix + '-restored'
    lv_source, lv_restored = prefix + '-lv', prefix + '-lv-copy'
    incus, domain = Incus(), Libvirt()
    db = sqlite3.connect('/var/lib/vm-manager/manager.sqlite3', timeout=30)
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db:
        uid = db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)', (prefix, hash_password(secrets.token_urlsafe(32)), 'operator', time.time())).lastrowid
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(raw), uid, csrf, time.time() + 3600))
    client = httpx.Client(base_url='https://192.168.8.235:8443', verify=False, timeout=200, cookies={'vmm_session': raw}, headers={'X-CSRF-Token': csrf})
    block = None
    lv_dir = Path(tempfile.mkdtemp(prefix=prefix + '-', dir='/var/lib/vm-manager/disks'))
    lv_restored_dirs = []
    own_ids = []

    def request(method, path, body=None):
        r = client.request(method, path, json=body)
        assert not r.is_error, (method, path, r.status_code, r.text)
        return r.json()

    def wait(task_id, expected='completed'):
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            row = next(t for t in request('GET', '/api/tasks') if t['id'] == task_id)
            if row['status'] in ('completed', 'failed', 'interrupted'):
                assert row['status'] == expected, row
                return row
            time.sleep(.5)
        raise AssertionError('Task timed out: ' + task_id)

    def task(method, path, body, expected='completed'):
        return wait(request(method, path, body)['task_id'], expected)

    def backup(vm_id):
        response = request('POST', '/api/backups', dict(vm_ids=[vm_id], block_id=block['id']))
        own_ids.append(response['tasks'][0]['backup_id'])
        wait(response['tasks'][0]['task_id'])
        return next(b for b in request('GET', '/api/backups')['backups'] if b['id'] == own_ids[-1])

    def volume_path(pool, kind, vm_name):
        p = next(p for p in incus.pools() if p['name'] == pool)
        source_dataset = p['config'].get('zfs.pool_name') or p['config']['source']
        path = Path('/dev/zvol') / source_dataset / kind / (vm_name + '.block' if kind == 'virtual-machines' else 'default_' + vm_name)
        if not path.exists():
            # Incus can leave a newly created stopped VM's zvol hidden. Only
            # these uniquely named test volumes are exposed for marker checks.
            run('zfs', 'set', 'volmode=dev', str(path.relative_to('/dev/zvol')))
            run('udevadm', 'settle')
        assert path.exists(), str(path)
        return path

    try:
        inv = request('GET', '/api/inventory')
        pool = next(p['name'] for p in inv['pools'] if p['driver'] == 'zfs' and not p['name'].startswith('vmm-block-'))
        network = next(n['name'] for n in inv['networks'] if n['provider'] == 'incus')
        task('POST', '/api/storage/blocks', dict(name=prefix, parent=pool, purpose='backup', size_gib=2))
        block = next(b for b in request('GET', '/api/inventory')['storage_blocks'] if b['name'] == prefix)
        task('POST', '/api/vms', dict(name=source, cpu=1, memory_gib=1, disk_gib=4, pool=pool, network=network, extra_disks=[1]))
        root_path = volume_path(pool, 'virtual-machines', source)
        extra_path = volume_path(pool, 'custom', 'vmm-' + source + '-disk1')
        marker = b'CarrTech backup acceptance ' + prefix.encode()
        for path in (root_path, extra_path):
            with path.open('r+b', buffering=0) as stream:
                stream.seek(4 * 1024**2)
                stream.write(marker)
                os.fsync(stream.fileno())
        point = backup('incus:' + source)
        task('POST', '/api/backups/' + point['id'] + '/restore', dict(name=restored, pool=pool))
        vm = incus.vm(restored)
        assert vm['status'].lower() == 'stopped'
        assert vm['config']['boot.autostart'] == 'false'
        assert vm['devices']['disk1']['source'] != 'vmm-' + source + '-disk1'
        new_extra = volume_path(pool, 'custom', vm['devices']['disk1']['source'])
        for path in (volume_path(pool, 'virtual-machines', restored), new_extra):
            with path.open('rb') as stream:
                stream.seek(4 * 1024**2)
                assert stream.read(len(marker)) == marker, str(path)
        # A restored archive must also produce a bootable hypervisor definition.
        task('POST', '/api/vms/incus:' + restored + '/action', dict(action='start'))
        started = incus.vm(restored)
        original = incus.vm(source)
        assert started['status'].lower() == 'running'
        for key in ('volatile.uuid', 'volatile.eth0.hwaddr'):
            assert started['config'].get(key) and started['config'][key] != original['config'].get(key), key
        task('POST', '/api/vms/incus:' + restored + '/action', dict(action='stop', confirm_name=restored))
        # Existing destination is protected; corruption is rejected before import.
        task('POST', '/api/backups/' + point['id'] + '/restore', dict(name=restored, pool=pool), 'failed')
        archive = Path(block['path']) / 'vm-backups' / point['id'] / 'instance.tar.gz'
        with archive.open('r+b') as stream:
            original = stream.read(1)
            stream.seek(0)
            stream.write(bytes([original[0] ^ 0xff]))
        row = task('POST', '/api/backups/' + point['id'] + '/restore', dict(name=prefix + '-bad', pool=pool), 'failed')
        assert 'checksum' in row['message'], row
        with archive.open('r+b') as stream:
            stream.write(original)
        print('PASS: Incus root + extra disk marker restoration, independent volumes, stopped/autostart-off restore, successful VM start with new identities, collision and checksum guards.', flush=True)

        # A sparse qcow2 with a backing chain verifies that libvirt export flattens
        # both layers, rather than retaining dependencies on original host files.
        lv_dir.chmod(0o755)
        base, disk = lv_dir / 'base.qcow2', lv_dir / 'disk.qcow2'
        run('qemu-img', 'create', '-f', 'qcow2', str(base), '64M')
        run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x5a 1048576 4096', str(base))
        run('qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(base), str(disk))
        run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x6b 2097152 4096', str(disk))
        xml = f'''<domain type="kvm"><name>{lv_source}</name><memory unit="MiB">256</memory><vcpu>1</vcpu><os><type arch="x86_64">hvm</type></os><devices><disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file="{disk}"/><target dev="vda" bus="virtio"/></disk><interface type="network"><source network="default"/><model type="virtio"/></interface></devices></domain>'''
        domain.define(ET.fromstring(xml))
        point = backup('libvirt:' + lv_source)
        task('POST', '/api/backups/' + point['id'] + '/restore', dict(name=lv_restored))
        root = domain.xml(lv_restored)
        new_disk = Path(root.find('./devices/disk/source').get('file'))
        lv_restored_dirs.append(new_disk.parent)
        domain.stopped(lv_restored)
        run('qemu-img', 'compare', '-f', 'qcow2', '-F', 'qcow2', str(disk), str(new_disk))
        assert 'backing-filename' not in json.loads(run('qemu-img', 'info', '--output=json', str(new_disk)))
        assert root.findtext('uuid') != domain.xml(lv_source).findtext('uuid')
        assert root.find('./devices/interface/mac').get('address') != domain.xml(lv_source).find('./devices/interface/mac').get('address')
        print('PASS: libvirt flattened backing-chain disk restore, new UUID/MAC and stopped VM.', flush=True)

        # Exercise the actual service scheduler, not a test-process scheduler.
        request('PUT', '/api/backups/plans', dict(vm_ids=['incus:' + source, 'libvirt:' + lv_source], block_id=block['id'], enabled=True, keep=1, timezone='UTC'))
        for iteration in range(2):
            with db:
                db.execute('UPDATE backup_plans SET next_run=0 WHERE vm_id IN (?,?)', ('incus:' + source, 'libvirt:' + lv_source))
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                plans = [p for p in request('GET', '/api/backups')['plans'] if p['vm_id'] in ('incus:' + source, 'libvirt:' + lv_source)]
                if len(plans) == 2 and all(p['next_run'] > time.time() and p['last_task'] for p in plans):
                    for p in plans:
                        wait(p['last_task'])
                    break
                time.sleep(1)
            else:
                raise AssertionError('Scheduler did not run')
        points = [b for b in request('GET', '/api/backups')['backups'] if b['vm_id'] in ('incus:' + source, 'libvirt:' + lv_source)]
        assert len([b for b in points if b['trigger'] == 'scheduled']) == 2, points
        assert len([b for b in points if b['trigger'] == 'manual']) == 2, points
        row = task('DELETE', '/api/storage/blocks/' + block['id'], dict(confirm_name=block['name']), 'failed')
        assert 'backup' in row['message'], row
        print('PASS: bulk VM plans, real scheduled execution for both providers, retention keeps latest scheduled + manual backups, storage deletion guard.', flush=True)
        ready_file = os.environ.get('VM_MANAGER_BACKUP_UI_READY')
        if ready_file:
            ready = Path(ready_file)
            ready.write_text(json.dumps(dict(token=raw, csrf=csrf, prefix=prefix, block_id=block['id'], source=source, libvirt=lv_source)))
            ready.chmod(0o600)
            print('Temporary resources ready for browser acceptance; remove the private marker file to continue cleanup.', flush=True)
            deadline = time.monotonic() + 600
            while ready.exists() and time.monotonic() < deadline:
                time.sleep(1)
            ready.unlink(missing_ok=True)
    finally:
        # Remove schedules first, then archives, then disposable guests/storage.
        try:
            for vm_id in ('incus:' + source, 'libvirt:' + lv_source):
                request('DELETE', '/api/backups/plans/' + vm_id)
            if block:
                for point in request('GET', '/api/backups')['backups']:
                    if point['block_id'] == block['id']:
                        if point['task_id']:
                            try:
                                wait(point['task_id'])
                            except AssertionError:
                                pass
                        task('DELETE', '/api/backups/' + point['id'], dict(confirm_name=point['vm_id'].split(':')[1]))
            for vm_name in (source, restored, prefix + '-bad'):
                if any(v['name'] == vm_name for v in incus.vms()):
                    task('DELETE', '/api/vms/incus:' + vm_name, dict(confirm_name=vm_name))
            for vm_name in (lv_source, lv_restored):
                if vm_name in domain.cli('list', '--all', '--name').splitlines():
                    if vm_name == lv_restored:
                        path = Path(domain.xml(vm_name).find('./devices/disk/source').get('file')).parent
                        if path.parent == Path('/var/lib/vm-manager/disks') and path.name.startswith('restore-'):
                            lv_restored_dirs.append(path)
                    domain.cli('undefine', vm_name)
            if block:
                # Config snapshots of temporary VM operations are not catalog
                # entries; remove only snapshots bearing these source VM names.
                for file in Path(block['path']).glob('*.json'):
                    if json.loads(file.read_text()).get('name', '').startswith(prefix):
                        file.unlink()
                task('DELETE', '/api/storage/blocks/' + block['id'], dict(confirm_name=block['name']))
            for path in set(lv_restored_dirs + [lv_dir]):
                shutil.rmtree(path)
            print('Removed temporary VMs, archives, backup plans, storage block and restored disk files.', flush=True)
        finally:
            client.close()
            with db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                db.execute('DELETE FROM users WHERE id=?', (uid,))
            db.close()


if __name__ == '__main__':
    main()
