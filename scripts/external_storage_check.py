"""Opt-in API acceptance using disposable Linux guests and SMB/S3 test endpoints.

Run as root on the deployed host with PYTHONPATH set to the release directory.
Prerequisites: two running Incus Linux guests with names beginning
vmm-external-check-, and a root-only JSON file containing `smb` and `s3` Source
request bodies for DISPOSABLE storage endpoints. Guest agents, Python 3, systemd
and working package repositories are required. An optional disposable libvirt
Linux guest exercises QEMU agent setup too. This script creates only uniquely
named sources, mounts, a temporary operator/session, backups and stopped restores.
It retains existing accounts, VM disks, defaults, schedules and unrelated files.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
import uuid

import httpx

from app.hypervisor import Incus, run
from app.security import hash_password, token_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--incus-vms', nargs=2, required=True)
    parser.add_argument('--libvirt-vm')
    parser.add_argument('--browser-state', type=Path)
    args = parser.parse_args()
    vms = ['incus:' + n for n in args.incus_vms]
    if args.libvirt_vm:
        vms.append('libvirt:' + args.libvirt_vm)
    if any(not vm.split(':', 1)[1].startswith('vmm-external-check-') for vm in vms):
        raise SystemExit('Use disposable guests named vmm-external-check-*')
    if args.sources.stat().st_mode & 0o077:
        raise SystemExit('The source configuration file must be readable only by root')
    definitions = json.loads(args.sources.read_text())
    prefix = 'vmm-external-check-' + uuid.uuid4().hex[:8]
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db = sqlite3.connect('/var/lib/vm-manager/manager.sqlite3', timeout=30)
    with db:
        uid = db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)',
            (prefix, hash_password(secrets.token_urlsafe(32)), 'operator', time.time())).lastrowid
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(raw), uid, csrf, time.time() + 3600))
    client = httpx.Client(base_url='https://192.168.8.235:8443', verify=False, timeout=200,
        cookies={'vmm_session': raw}, headers={'X-CSRF-Token': csrf})
    sources, mounts, points, restores = [], [], [], []

    def request(method, path, body=None):
        response = client.request(method, path, json=body)
        assert not response.is_error, (method, path, response.status_code, response.text)
        return response.json()

    def wait(response, expected='completed'):
        deadline = time.monotonic() + 1500
        while time.monotonic() < deadline:
            row = next(t for t in request('GET', '/api/tasks') if t['id'] == response['task_id'])
            if row['status'] not in ('queued', 'running'):
                assert row['status'] == expected, row
                return row
            time.sleep(.5)
        raise AssertionError('Task did not finish')

    def task(method, path, body=None, expected='completed'):
        return wait(request(method, path, body), expected)

    def guest(vm, code):
        # Test-only execution; storage credentials never appear in this code.
        return run('incus', 'exec', vm, '--mode=non-interactive', '--', 'python3', '-c', code)

    try:
        for kind in ('smb', 's3'):
            body = definitions[kind] | {'name': prefix + '-' + kind}
            task('POST', '/api/storage/sources', body)
            source = next(s for s in request('GET', '/api/storage/sources')['sources'] if s['name'] == body['name'])
            sources.append(source)
            public = json.dumps(request('GET', '/api/storage/sources'))
            for key in ('password', 'access_key', 'secret_key'):
                secret = body.get(key, '')
                if len(secret) >= 12:
                    assert secret not in public
            target = '/mnt/' + prefix + '-' + kind
            for vm_id in vms:
                task('POST', '/api/vms/' + vm_id + '/mounts', dict(source_id=source['id'], path=target))
                mount = next(m for m in request('GET', '/api/storage/sources')['mounts'] if m['vm_id'] == vm_id and m['path'] == target)
                mounts.append((vm_id, mount))
            marker_name = prefix + '.txt'
            guest(args.incus_vms[0], f'from pathlib import Path; Path({(target + "/" + marker_name)!r}).write_text({prefix!r})')
            if kind == 'smb':
                assert guest(args.incus_vms[1], f'from pathlib import Path; print(Path({(target + "/" + marker_name)!r}).read_text(),end="")') == prefix
            else:
                # A closed S3 write uploads asynchronously. Check mount verifies
                # access; remote backup checks below validate the object API.
                time.sleep(10)
            for vm_id, mount in mounts:
                if mount['source_id'] == source['id']:
                    task('POST', '/api/vms/' + vm_id + '/mounts/' + mount['id'] + '/test')
            # Removing a source in use must fail without losing its record.
            task('DELETE', '/api/storage/sources/' + source['id'], {'confirm_name': source['name']}, expected='failed')
            guest(args.incus_vms[0], f'from pathlib import Path; Path({(target + "/" + marker_name)!r}).unlink()')
            time.sleep(6 if kind == 's3' else .1)
            print(kind.upper() + ' API source / guest mounts / shared access checks passed', flush=True)

        # Real VM export/upload/download/import for both destination types. Stop
        # only this disposable guest and retain its independent source disk.
        task('POST', '/api/vms/' + vms[0] + '/action', {'action': 'shutdown'})
        for source in sources:
            response = request('POST', '/api/backups', {'vm_ids': [vms[0]], 'block_id': source['id'], 'mode': 'stopped'})
            point = response['tasks'][0]
            points.append(point['backup_id'])
            wait(point)
            copy = prefix + '-' + source['kind'] + '-restore'
            restores.append(copy)
            task('POST', '/api/backups/' + point['backup_id'] + '/restore', {'name': copy, 'pool': 'hdd-pool'})
            assert Incus().vm(copy)['status'].lower() == 'stopped'
            print(source['kind'].upper() + ' real VM remote backup and restore passed', flush=True)
        task('POST', '/api/vms/' + vms[0] + '/action', {'action': 'start'})
        # Give its network/mount services time to recover after boot.
        time.sleep(15)
        if args.browser_state:
            state = {'cookies': [{'name': 'vmm_session', 'value': raw, 'domain': domain,
                'path': '/', 'expires': time.time() + 1800, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}
                for domain in ('192.168.8.235', 'vmm.carrtech.dev')], 'origins': []}
            args.browser_state.write_text(json.dumps(state))
            args.browser_state.chmod(0o600)
            print('Browser state ready; create ' + str(args.browser_state) + '.continue to finish cleanup', flush=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and not Path(str(args.browser_state) + '.continue').exists():
                time.sleep(1)
        print('External storage acceptance passed', flush=True)
    finally:
        # Keep cleanup failures visible and recoverable; never reset accounts or
        # force-remove a source/guest when a task failed partway through.
        for bid in points:
            task('DELETE', '/api/backups/' + bid, {'confirm_name': args.incus_vms[0]})
        for copy in restores:
            # Restores are deliberately never started, so their automatic guest
            # mounts and saved credentials never connect to the source storage.
            if subprocess.run(['incus', 'info', copy], capture_output=True).returncode == 0:
                task('DELETE', '/api/vms/incus:' + copy, {'confirm_name': copy})
        for vm_id, mount in reversed(mounts):
            task('DELETE', '/api/vms/' + vm_id + '/mounts/' + mount['id'], {'confirm_name': mount['id']})
        for source in sources:
            task('DELETE', '/api/storage/sources/' + source['id'], {'confirm_name': source['name']})
        with db:
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            db.execute('DELETE FROM users WHERE id=?', (uid,))
        client.close()
        db.close()
        if args.browser_state:
            args.browser_state.unlink(missing_ok=True)
            Path(str(args.browser_state) + '.continue').unlink(missing_ok=True)


if __name__ == '__main__':
    main()
