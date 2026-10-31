"""Opt-in deployed acceptance on pre-prepared, disposable Linux VMs only.

Run as root with the release Python and PYTHONPATH set to that release:
  python scripts/patching_check.py --vms incus:vmm-patch-check-ubuntu libvirt:vmm-patch-check-libvirt
VM names MUST start with vmm-patch-check-. Updates packages on those VMs only.
Creates/removes temporary operator/viewer sessions and schedules; preserves all
existing accounts and VMs. --restart-check kills/restarts only the VM Manager
process during an active patch (never the hypervisors); run outside other app work.
--browser-state writes a private, short-lived Playwright session and pauses until
the file is removed. The caller owns guest creation/deletion and downloaded images.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
import uuid

import httpx

from app.security import hash_password, token_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vms', nargs='+', required=True)
    parser.add_argument('--restart-check', action='store_true')
    parser.add_argument('--reboot-check', action='store_true', help='Mark a disposable Incus Ubuntu guest reboot-required and verify an actual reboot')
    parser.add_argument('--browser-state', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or any(not v.startswith(('incus:vmm-patch-check-', 'libvirt:vmm-patch-check-')) for v in args.vms):
        raise SystemExit('Run as root with disposable vmm-patch-check-* Linux VMs only')
    prefix = 'vmm-patch-check-' + uuid.uuid4().hex[:8]
    db = sqlite3.connect('/var/lib/vm-manager/manager.sqlite3', timeout=30)
    clients, users, schedules = [], [], []
    root = '/api/operations/patching'
    for role in ('operator', 'viewer'):
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with db:
            uid = db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)',
                (prefix + '-' + role, hash_password(secrets.token_urlsafe(32)), role, time.time())).lastrowid
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(raw), uid, csrf, time.time() + 3600))
        users.append(uid)
        clients.append(httpx.Client(base_url='https://192.168.8.235:8443', verify=False, timeout=120,
            cookies={'vmm_session': raw}, headers={'X-CSRF-Token': csrf}))
    client, viewer = clients

    def request(method, path, body=None):
        response = client.request(method, path, json=body)
        assert response.status_code == 200, (method, path, response.status_code, response.text)
        return response.json()

    def wait_check(tid):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            row = next(t for t in request('GET', '/api/tasks') if t['id'] == tid)
            if row['status'] not in ('queued', 'running'):
                assert row['status'] == 'completed', row
                return
            time.sleep(1)
        raise AssertionError('Readiness timed out')

    def wait_batch(bid):
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            runs = [r for r in request('GET', root)['runs'] if r['batch_id'] == bid]
            if runs and all(r['status'] not in ('pending', 'dispatched', 'running', 'reconnecting') for r in runs):
                assert all(r['status'] == 'completed' for r in runs), [(r['vm_id'], r['status'], r['result'].get('message')) for r in runs]
                return runs
            time.sleep(2)
        raise AssertionError('Patch batch timed out')

    try:
        assert viewer.get(root).status_code == 200
        assert viewer.post(root + '/runs', json={'vm_ids': args.vms}).status_code == 403
        for job in request('POST', root + '/check', {'vm_ids': args.vms})['tasks']:
            wait_check(job['task_id'])
        guests = request('GET', root)['guests']
        assert all(guests[v]['ready'] for v in args.vms), {v: guests[v] for v in args.vms}
        print('PASS: operator checks, viewer permissions, Linux guest-agent readiness for ' + ', '.join(args.vms), flush=True)
        batch = request('POST', root + '/runs', {'vm_ids': args.vms, 'concurrency': 2, 'reboot': 'never'})['batch_id']
        assert client.post(root + '/runs', json={'vm_ids': args.vms}).status_code == 409
        if args.restart_check:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                active = [r for r in request('GET', root)['runs'] if r['batch_id'] == batch and r['status'] == 'running' and r['attempted']]
                if active:
                    break
                time.sleep(.2)
            assert active, 'No active guest transaction observed for restart check'
            unrelated = [t for t in request('GET', '/api/tasks') if t['status'] in ('queued', 'running') and t['resource'] not in args.vms]
            assert not unrelated, 'Wait for unrelated management tasks before crash-recovery acceptance'
            subprocess.run(['systemctl', 'kill', '--signal=KILL', '--kill-whom=main', 'vm-manager.service'], check=True)
            subprocess.run(['systemctl', 'restart', 'vm-manager.service'], check=True)
            for _ in range(60):
                try:
                    if client.get('/health').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            print('Restarted manager during guest transaction; waiting for recovery using original run IDs', flush=True)
        runs = wait_batch(batch)
        for run in runs:
            assert run['result']['status'] == 'completed'
            print('PASS: ' + run['vm_id'] + ' real package transaction: ' + run['result']['message'], flush=True)
        if args.reboot_check:
            vm = next(v for v in args.vms if v.startswith('incus:') and guests[v]['manager'] == 'apt-get')
            subprocess.run(['incus', 'exec', vm.split(':', 1)[1], '--', 'touch', '/var/run/reboot-required'], check=True)
            reboot_batch = request('POST', root + '/runs', {'vm_ids': [vm], 'reboot': 'if-required'})['batch_id']
            rebooted = wait_batch(reboot_batch)
            assert rebooted[0]['result'].get('rebooted'), rebooted[0]
            print('PASS: required reboot, changed boot identity and agent reconnection verified', flush=True)
        schedule = request('POST', root + '/schedules', dict(name=prefix, vm_ids=args.vms, frequency='daily', timezone='Europe/London', time='02:00', window_minutes=30))['id']
        schedules.append(schedule)
        with db:
            db.execute('UPDATE patch_schedules SET next_run=? WHERE id=?', (time.time() - 3600, schedule))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            plan = next(p for p in request('GET', root)['schedules'] if p['id'] == schedule)
            if 'missed' in plan['last_message']:
                break
            time.sleep(1)
        assert 'missed' in plan['last_message'], plan
        with db:
            db.execute('UPDATE patch_schedules SET next_run=? WHERE id=?', (time.time() - 1, schedule))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            scheduled = [r for r in request('GET', root)['runs'] if r['schedule_id'] == schedule]
            if scheduled:
                break
            time.sleep(1)
        assert scheduled
        wait_batch(scheduled[0]['batch_id'])
        print('PASS: missed-window skip and scheduled dispatch with per-VM results', flush=True)
        if args.browser_state:
            args.browser_state.write_text(json.dumps({'cookies': [{'name': 'vmm_session', 'value': client.cookies.get('vmm_session'),
                'domain': '192.168.8.235', 'path': '/', 'expires': time.time() + 900, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}], 'origins': []}))
            args.browser_state.chmod(0o600)
            print('Browser session ready; remove the private state file to continue cleanup', flush=True)
            deadline = time.monotonic() + 900
            while args.browser_state.exists() and time.monotonic() < deadline:
                time.sleep(1)
    finally:
        for sid in schedules:
            client.delete(root + '/schedules/' + sid)
        with db:
            for uid in users:
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                db.execute('DELETE FROM users WHERE id=?', (uid,))
        for connection in clients:
            connection.close()
        db.close()
        if args.browser_state:
            args.browser_state.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
