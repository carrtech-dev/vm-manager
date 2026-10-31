"""Opt-in root-only acceptance against the deployed service, with disposable data.

Creates uniquely named VMs, a backup block and an operator/session. Existing VMs,
storage defaults, accounts and schedules are retained. Never use production disks.
Run with the deployed release Python environment and PYTHONPATH set to the release.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import httpx

from app.hypervisor import Incus, Libvirt, run
from app.security import hash_password, token_hash


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root on the hypervisor; creates disposable resources only')
    prefix = 'vmm-template-check-' + uuid.uuid4().hex[:8]
    source, copies = prefix + '-src', [prefix + '-copy1', prefix + '-copy2']
    lv_source, lv_copy = prefix + '-lv', prefix + '-lv-copy'
    incus, domain = Incus(), Libvirt()
    db = sqlite3.connect('/var/lib/vm-manager/manager.sqlite3', timeout=30)
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db:
        uid = db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)',
                         (prefix, hash_password(secrets.token_urlsafe(32)), 'operator', time.time())).lastrowid
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(raw), uid, csrf, time.time() + 3600))
    client = httpx.Client(base_url='https://192.168.8.235:8443', verify=False, timeout=200,
                         cookies={'vmm_session': raw}, headers={'X-CSRF-Token': csrf})
    block = None
    lv_dir = Path(tempfile.mkdtemp(prefix=prefix + '-', dir='/var/lib/vm-manager/disks'))
    restored_dirs = []

    def request(method, path, body=None):
        response = client.request(method, path, json=body)
        assert not response.is_error, (method, path, response.status_code, response.text)
        return response.json()

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

    def capture(vm_id, label):
        result = request('POST', '/api/templates', dict(vm_id=vm_id, name=label,
                         description='Disposable acceptance template', block_id=block['id'], mode='stopped'))
        wait(result['task_id'])
        assert all(b['id'] != result['template_id'] for b in request('GET', '/api/backups')['backups'])
        return result['template_id']

    def volume_path(pool, kind, vm_name):
        p = next(p for p in incus.pools() if p['name'] == pool)
        dataset = p['config'].get('zfs.pool_name') or p['config']['source']
        path = Path('/dev/zvol') / dataset / kind / (vm_name + '.block' if kind == 'virtual-machines' else 'default_' + vm_name)
        if not path.exists():
            run('zfs', 'set', 'volmode=dev', str(path.relative_to('/dev/zvol')))
            run('udevadm', 'settle')
        assert path.exists(), str(path)
        return path

    def marker(path, value=None):
        with path.open('r+b' if value is not None else 'rb', buffering=0) as stream:
            stream.seek(4 * 1024**2)
            if value is None:
                return stream.read(64)
            stream.write(value)
            os.fsync(stream.fileno())

    try:
        inv = request('GET', '/api/inventory')
        pool = next(p['name'] for p in inv['pools'] if p['driver'] == 'zfs' and not p['name'].startswith('vmm-block-'))
        network = next(n['name'] for n in inv['networks'] if n['provider'] == 'incus')
        task('POST', '/api/storage/blocks', dict(name=prefix, parent=pool, purpose='backup', size_gib=2))
        block = next(b for b in request('GET', '/api/inventory')['storage_blocks'] if b['name'] == prefix)
        task('POST', '/api/vms', dict(name=source, cpu=1, memory_gib=1, disk_gib=4,
                                    pool=pool, network=network, extra_disks=[1]))
        value = (prefix.encode() + b' independent template disks').ljust(64, b'!')[:64]
        for path in (volume_path(pool, 'virtual-machines', source),
                     volume_path(pool, 'custom', 'vmm-' + source + '-disk1')):
            marker(path, value)
        tid = capture('incus:' + source, prefix + ' Incus')
        tasks = request('POST', '/api/vms/bulk/action', dict(vm_ids=['incus:' + source], action='delete', confirmed=True))
        wait(tasks['tasks'][0]['task_id'])
        assert any(t['id'] == tid for t in request('GET', '/api/templates')['templates'])
        new_volumes, identities = [], []
        for vm_name in copies:
            task('POST', '/api/templates/' + tid + '/vms', dict(name=vm_name, pool=pool))
            vm = incus.vm(vm_name)
            assert vm['status'].lower() == 'stopped' and vm['config']['boot.autostart'] == 'false'
            new_volumes.append(vm['devices']['disk1']['source'])
            for path in (volume_path(pool, 'virtual-machines', vm_name),
                         volume_path(pool, 'custom', new_volumes[-1])):
                assert marker(path) == value, str(path)
            task('POST', '/api/vms/incus:' + vm_name + '/action', dict(action='start'))
            vm = incus.vm(vm_name)
            identities.append((vm['config']['volatile.uuid'], vm['config']['volatile.eth0.hwaddr']))
            task('POST', '/api/vms/incus:' + vm_name + '/action', dict(action='stop', confirm_name=vm_name))
            if vm_name == copies[0]:
                marker(volume_path(pool, 'custom', new_volumes[0]), b'changed copy'.ljust(64, b'?'))
        assert new_volumes[0] != new_volumes[1]
        assert all(identities[0][i] != identities[1][i] for i in range(2))
        task('POST', '/api/templates/' + tid + '/vms', dict(name=copies[0], pool=pool), 'failed')
        archive = Path(block['path']) / 'vm-backups' / tid / 'instance.tar.gz'
        with archive.open('r+b') as stream:
            original = stream.read(1)
            stream.seek(0)
            stream.write(bytes([original[0] ^ 0xff]))
        failed = task('POST', '/api/templates/' + tid + '/vms', dict(name=prefix + '-bad', pool=pool), 'failed')
        assert 'checksum' in failed['message'], failed
        with archive.open('r+b') as stream:
            stream.write(original)
        print('PASS: Incus source deletion, repeat creation, root/data markers, independent disks, boot, UUID/MAC, collision and checksum rejection.', flush=True)

        lv_dir.chmod(0o755)
        disk = lv_dir / 'disk.qcow2'
        run('qemu-img', 'create', '-f', 'qcow2', str(disk), '64M')
        run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x5a 1048576 4096', str(disk))
        xml = f'''<domain type="kvm"><name>{lv_source}</name><memory unit="MiB">256</memory><vcpu>1</vcpu><os><type arch="x86_64">hvm</type></os><devices><disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file="{disk}"/><target dev="vda" bus="virtio"/></disk><interface type="network"><source network="default"/><model type="virtio"/></interface></devices></domain>'''
        domain.define(ET.fromstring(xml))
        original = domain.xml(lv_source)
        lv_tid = capture('libvirt:' + lv_source, prefix + ' libvirt')
        task('DELETE', '/api/vms/libvirt:' + lv_source, dict(confirm_name=lv_source))
        task('POST', '/api/templates/' + lv_tid + '/vms', dict(name=lv_copy))
        root = domain.xml(lv_copy)
        restored = Path(root.find('./devices/disk/source').get('file'))
        restored_dirs.append(restored.parent)
        domain.stopped(lv_copy)
        run('qemu-img', 'compare', '-f', 'qcow2', '-F', 'qcow2', str(disk), str(restored))
        assert root.findtext('uuid') != original.findtext('uuid')
        assert root.find('./devices/interface/mac').get('address') != original.find('./devices/interface/mac').get('address')
        assert domain.cli('dominfo', lv_copy).split('Autostart:')[1].strip().startswith('disable')
        domain.action(lv_copy, 'start')
        domain.action(lv_copy, 'stop')
        print('PASS: libvirt template survives source deletion; independent stopped VM, disk content, UUID/MAC, autostart disabled and boot.', flush=True)
        blocked = task('DELETE', '/api/storage/blocks/' + block['id'], dict(confirm_name=prefix), 'failed')
        assert 'backup' in blocked['message'], blocked
        ready_file = os.environ.get('VM_MANAGER_TEMPLATE_UI_READY')
        if ready_file:
            ready = Path(ready_file)
            ready.write_text(json.dumps(dict(token=raw, csrf=csrf, prefix=prefix, pool=pool,
                             block_id=block['id'], source=copies[0], template_id=tid, libvirt_template_id=lv_tid)))
            ready.chmod(0o600)
            print('Temporary resources ready for browser acceptance; remove the private marker file to continue cleanup.', flush=True)
            deadline = time.monotonic() + 600
            while ready.exists() and time.monotonic() < deadline:
                time.sleep(1)
            ready.unlink(missing_ok=True)
        for template_id, label in ((tid, prefix + ' Incus'), (lv_tid, prefix + ' libvirt')):
            task('DELETE', '/api/templates/' + template_id, dict(confirm_name=label))
        assert all(incus.vm(n)['status'].lower() == 'stopped' for n in copies)
        domain.stopped(lv_copy)
        print('PASS: storage deletion guard; template deletion retains created VMs.', flush=True)
    finally:
        try:
            # Clean up only records in our uniquely named temporary block.
            if block:
                for point in request('GET', '/api/templates')['templates']:
                    if point['block_id'] == block['id']:
                        if point['task_id']:
                            try:
                                wait(point['task_id'])
                            except AssertionError:
                                pass
                        task('DELETE', '/api/templates/' + point['id'], dict(confirm_name=point['name']))
            for vm_name in (source, *copies, prefix + '-bad', prefix + '-ui'):
                if any(v['name'] == vm_name for v in incus.vms()):
                    if incus.vm(vm_name)['status'].lower() != 'stopped':
                        incus.action(vm_name, 'stop')
                    task('DELETE', '/api/vms/incus:' + vm_name, dict(confirm_name=vm_name))
            for vm_name in (lv_source, lv_copy):
                if vm_name in domain.cli('list', '--all', '--name').splitlines():
                    if vm_name == lv_copy:
                        path = Path(domain.xml(vm_name).find('./devices/disk/source').get('file')).parent
                        if path.parent == Path('/var/lib/vm-manager/disks') and path.name.startswith('restore-'):
                            restored_dirs.append(path)
                    if domain.cli('domstate', vm_name).strip() != 'shut off':
                        domain.action(vm_name, 'stop')
                    domain.cli('undefine', vm_name)
            if block:
                task('DELETE', '/api/storage/blocks/' + block['id'], dict(confirm_name=prefix))
            for path in set(restored_dirs + [lv_dir]):
                shutil.rmtree(path)
            print('Removed disposable VMs, templates, storage and disk files.', flush=True)
        finally:
            client.close()
            with db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                db.execute('DELETE FROM users WHERE id=?', (uid,))
            db.close()


if __name__ == '__main__':
    main()
