"""Opt-in deployed bulk VM acceptance; run as root with release PYTHONPATH.

Uses disposable Incus/ZFS and libvirt/qcow2 VMs, snapshots, an independent backup
block and a temporary operator/session. Existing VMs/accounts/defaults are kept.
Optional --browser-state writes temporary public-site browser authentication,
then waits up to five minutes for <path>.continue before exercising deletion.
Treat that file as a credential and remove it after browser verification.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser-state', type=Path)
    args = parser.parse_args()
    prefix = 'vmm-bulk-check-' + uuid.uuid4().hex[:8]
    incus, libvirt = Incus(), Libvirt()
    db = sqlite3.connect('/var/lib/vm-manager/manager.sqlite3', timeout=30)
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db:
        uid = db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)',
                         (prefix, hash_password(secrets.token_urlsafe(32)), 'operator', time.time())).lastrowid
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(raw), uid, csrf, time.time() + 1800))
    client = httpx.Client(base_url='https://192.168.8.235:8443', verify=False, timeout=200,
                         cookies={'vmm_session': raw}, headers={'X-CSRF-Token': csrf})
    scratch = Path(tempfile.mkdtemp(prefix=prefix + '-', dir='/var/lib/vm-manager/disks'))
    scratch.chmod(0o755)
    ids = ['incus:' + prefix + '-incus', 'libvirt:' + prefix + '-libvirt']
    block = None
    backup_ids = []

    def request(method, path, body=None):
        response = client.request(method, path, json=body)
        assert not response.is_error, (method, path, response.status_code, response.text)
        return response.json()

    def wait(tasks, expected='completed'):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            rows = {t['id']: t for t in request('GET', '/api/tasks')}
            selected = [rows[t['task_id']] for t in tasks]
            if all(t['status'] not in ('queued', 'running') for t in selected):
                assert all(t['status'] == expected for t in selected), selected
                return selected
            time.sleep(.2)
        raise AssertionError('Bulk tasks timed out')

    def task(method, path, body):
        return wait([request(method, path, body)])[0]

    def bulk(action, preserve=()):
        return request('POST', '/api/vms/bulk/action', dict(vm_ids=ids, action=action,
                       confirmed=True, preserve_backups=list(preserve)))['tasks']

    def refresh():
        time.sleep(5.1)  # Host inventory is cached for five seconds.
        return request('GET', '/api/inventory')

    try:
        inv = request('GET', '/api/inventory')
        pool = next(p['name'] for p in inv['pools'] if p['driver'] == 'zfs' and not p['name'].startswith('vmm-block-'))
        network = next(n['name'] for n in inv['networks'] if n['provider'] == 'incus')
        task('POST', '/api/storage/blocks', dict(name=prefix, parent=pool, purpose='backup', size_gib=2))
        block = next(b for b in request('GET', '/api/inventory')['storage_blocks'] if b['name'] == prefix)
        task('POST', '/api/vms', dict(name=ids[0].split(':')[1], cpu=1, memory_gib=1,
             disk_gib=4, pool=pool, network=network, extra_disks=[1], secure_boot=False))
        disk = scratch / 'root.qcow2'
        run('qemu-img', 'create', '-f', 'qcow2', str(disk), '32M')
        os.chown(disk, pwd.getpwnam('libvirt-qemu').pw_uid, grp.getgrnam('libvirt').gr_gid)
        disk.chmod(0o660)
        root = ET.fromstring(f'<domain type="kvm"><name>{ids[1].split(":")[1]}</name><memory unit="MiB">256</memory><vcpu>1</vcpu><os><type arch="x86_64">hvm</type></os><features><acpi/></features><devices><disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file="{disk}"/><target dev="vda" bus="virtio"/></disk></devices></domain>')
        libvirt.define(root)
        refresh()
        # Both guests wait at boot menus. Shutdown/restart run simultaneously;
        # emergency bulk stop interrupts both without waiting for guest timeout.
        for operation in ('shutdown', 'restart'):
            for vm in ids:
                task('POST', '/api/vms/' + vm + '/action', dict(action='start'))
            pending = bulk(operation)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rows = {t['id']: t for t in request('GET', '/api/tasks')}
                if all(rows[t['task_id']]['status'] == 'running' for t in pending):
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Power tasks did not run concurrently')
            stopped = wait(bulk('stop'))
            wait(pending, 'interrupted')
            assert incus.vm(ids[0].split(':')[1])['status'].lower() == 'stopped'
            assert libvirt.cli('domstate', ids[1].split(':')[1]).strip() == 'shut off'
            print('Verified concurrent bulk ' + operation + ' and emergency stop cancellation', flush=True)
        for vm in ids:
            for snapshot in ('first', 'second'):
                task('POST', '/api/vms/' + vm + '/snapshots', dict(name=snapshot, mode='stopped'))
        points = request('POST', '/api/backups', dict(vm_ids=ids, block_id=block['id'], mode='stopped'))['tasks']
        backup_ids = [p['backup_id'] for p in points]
        wait(points)
        assert len([b for b in request('GET', '/api/backups')['backups'] if b['id'] in backup_ids]) == 2
        if args.browser_state:
            args.browser_state.write_text(json.dumps({'cookies': [{'name': 'vmm_session', 'value': raw,
                'domain': 'vmm.carrtech.dev', 'path': '/', 'expires': time.time() + 1800,
                'httpOnly': True, 'secure': True, 'sameSite': 'Strict'}], 'origins': []}))
            args.browser_state.chmod(0o600)
            print('Ready for browser verification: search ' + prefix, flush=True)
            deadline = time.monotonic() + 300
            while not Path(str(args.browser_state) + '.continue').exists() and time.monotonic() < deadline:
                time.sleep(1)
        wait(bulk('delete', [ids[1]]))
        remaining = request('GET', '/api/backups')['backups']
        assert not any(b['vm_id'] == ids[0] for b in remaining)
        preserved = [b for b in remaining if b['vm_id'] == ids[1]]
        assert len(preserved) == 1 and preserved[0]['status'] == 'completed', preserved
        assert disk.exists(), 'Existing libvirt disk files must be retained'
        assert ids[0].split(':')[1] not in [v['name'] for v in incus.vms()]
        assert ids[1].split(':')[1] not in libvirt.cli('list', '--all', '--name').splitlines()
        with db:
            assert not db.execute('SELECT 1 FROM vm_snapshots WHERE vm_id IN (?,?)', ids).fetchone()
            assert not db.execute('SELECT 1 FROM backup_plans WHERE vm_id IN (?,?)', ids).fetchone()
            assert db.execute("SELECT count(*) FROM audit WHERE username=? AND action='vm.delete' AND detail LIKE 'completed:%'", (prefix,)).fetchone()[0] == 2
        # Restore the preserved backup to prove it remains usable without source VM.
        restored = prefix + '-restored'
        try:
            task('POST', '/api/backups/' + preserved[0]['id'] + '/restore', dict(name=restored, pool=pool))
            assert libvirt.cli('domstate', restored).strip() == 'shut off'
        finally:
            if restored in libvirt.cli('list', '--all', '--name').splitlines():
                restored_paths = [Path(s.get('file')) for s in libvirt.xml(restored).findall('./devices/disk/source') if s.get('file')]
                libvirt.cli('undefine', restored, '--nvram')
                for path in restored_paths:
                    if path.parent.parent == Path('/var/lib/vm-manager/disks') and path.parent.name.startswith('restore-'):
                        shutil.rmtree(path.parent)
        print('Verified bulk deletion: snapshot chains/data disks removed, backups default deleted, checked backup preserved and restored', flush=True)
    finally:
        # Only this uniquely named check's resources are eligible for cleanup.
        for vm in ids:
            try:
                provider, vm_name = vm.split(':')
                if provider == 'incus' and any(v['name'] == vm_name for v in incus.vms()):
                    incus.action(vm_name, 'stop') if incus.vm(vm_name)['status'].lower() != 'stopped' else None
                    run('incus', 'delete', vm_name, '--force')
                    for p in incus.pools():
                        for volume in incus.get('/1.0/storage-pools/' + p['name'] + '/volumes/custom?recursion=1'):
                            if volume['name'].startswith('vmm-' + vm_name + '-'):
                                run('incus', 'storage', 'volume', 'delete', p['name'], volume['name'])
                elif provider == 'libvirt' and vm_name in libvirt.cli('list', '--all', '--name').splitlines():
                    if libvirt.cli('domstate', vm_name).strip() != 'shut off':
                        libvirt.cli('destroy', vm_name)
                    for snapshot in libvirt.cli('snapshot-list', vm_name, '--name').splitlines():
                        libvirt.cli('snapshot-delete', vm_name, snapshot)
                    libvirt.cli('undefine', vm_name, '--nvram')
            except Exception as exc:
                print('Cleanup requires inspection: ' + vm + ': ' + str(exc), flush=True)
        for backup in request('GET', '/api/backups')['backups']:
            if backup['vm_id'] in ids:
                task('DELETE', '/api/backups/' + backup['id'], dict(confirm_name=backup['vm_id'].split(':')[1]))
        with db:
            for vm in ids:
                db.execute('DELETE FROM vm_snapshots WHERE vm_id=?', (vm,))
                db.execute('DELETE FROM snapshot_heads WHERE vm_id=?', (vm,))
        if block:
            task('DELETE', '/api/storage/blocks/' + block['id'], dict(confirm_name=prefix))
        shutil.rmtree(scratch)
        with db:
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            db.execute('DELETE FROM users WHERE id=?', (uid,))
        if args.browser_state:
            args.browser_state.unlink(missing_ok=True)
            Path(str(args.browser_state) + '.continue').unlink(missing_ok=True)
        print('Removed disposable bulk acceptance resources and account', flush=True)


if __name__ == '__main__':
    main()
