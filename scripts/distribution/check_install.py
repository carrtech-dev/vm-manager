#!/usr/bin/env python3
"""Acceptance checks INSIDE A DISPOSABLE package test host, never production.

Run `prepare` after initial install, `verify-upgrade` after an actual APT/DNF
upgrade, `verify-setup` after completing the browser wizard, and `verify-removed`
after uninstall. The setup phase creates, starts, stops and deletes a uniquely
named disposable VM for local-host mode. Requires an explicit root-owned
/root/vmm-distribution-disposable marker to avoid touching real account data.
Changes only the disposable host's initial admin password; never prints it.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
import platform
from pathlib import Path
import pwd
import secrets
import sqlite3
import ssl
import stat
import subprocess
import time
import uuid
import urllib.request

STATE = Path('/var/lib/vm-manager')
RECORD = Path('/root/vmm-distribution-check.json')
ORIGIN = 'https://127.0.0.1:8443'


def client():
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    def request(path, data=None, method=None, csrf=None):
        headers = {'Content-Type': 'application/json', 'Origin': ORIGIN}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        req = urllib.request.Request(ORIGIN + path, headers=headers,
            data=None if data is None else json.dumps(data).encode(), method=method)
        with opener.open(req, timeout=20) as response:
            return json.load(response)
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'verify-setup', 'verify-upgrade', 'verify-removed'))
    args = parser.parse_args()
    marker = Path('/root/vmm-distribution-disposable')
    if os.geteuid() or not marker.exists() or marker.stat().st_uid != 0:
        raise SystemExit('Requires root and explicit disposable-host marker; NEVER use production data')
    os.umask(0o077)
    if platform.freedesktop_os_release().get('ID') in ('rocky', 'almalinux'):
        assert subprocess.check_output(['getenforce'], text=True).strip() == 'Enforcing', 'RPM acceptance requires SELinux enforcing; disabled/permissive testing cannot validate package scriptlets'
    if args.phase == 'prepare':
        assert not RECORD.exists(), 'Already prepared; do not reset account data'
        credentials = STATE / 'initial-admin.txt'
        assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
        assert credentials.stat().st_uid == pwd.getpwnam('vm-manager').pw_uid
        password = credentials.read_text().split('Password: ')[1].splitlines()[0]
        request = client()
        assert request('/health')['status'] == 'ok'
        login = request('/api/login', {'username': 'admin', 'password': password})
        assert login['role'] == 'administrator'
        changed = secrets.token_urlsafe(24)
        assert request('/api/password', {'current': password, 'password': changed}, 'PUT', login['csrf'])['ok']
        assert not credentials.exists()
        with sqlite3.connect(STATE / 'manager.sqlite3') as db:
            account = db.execute('SELECT id,username,password,role FROM users').fetchall()
        RECORD.write_text(json.dumps({'password': changed, 'accounts': account,
            'tls': hashlib.sha256(Path('/etc/vm-manager/tls/server.key').read_bytes()).hexdigest()}))
        print('Fresh installation verified: HTTPS admin login, private credentials, password change and credential removal')
    else:
        record = json.loads(RECORD.read_text())
        with sqlite3.connect(STATE / 'manager.sqlite3') as db:
            assert [list(r) for r in db.execute('SELECT id,username,password,role FROM users')] == record['accounts']
        assert not (STATE / 'initial-admin.txt').exists()
        assert hashlib.sha256(Path('/etc/vm-manager/tls/server.key').read_bytes()).hexdigest() == record['tls']
        if args.phase == 'verify-setup':
            request = client()
            login = request('/api/login', {'username': 'admin', 'password': record['password']})
            setup = request('/api/setup')
            assert subprocess.check_output(['systemctl', 'show', 'vm-manager-storage', '--property=NoNewPrivileges', '--value'], text=True).strip() == 'yes'
            assert subprocess.check_output(['systemctl', 'show', 'vm-manager-setup', '--property=NoNewPrivileges', '--value'], text=True).strip() == 'no'
            assert setup['status'] == 'completed' and not setup['required'], setup
            inventory = request('/api/inventory')
            assert not inventory['warnings'], inventory['warnings']
            if setup['plan']['mode'] == 'cluster':
                assert not inventory['vms'] and not inventory['pools']
                print('Cluster-only setup verified: no local hypervisor probes; account and TLS preserved')
                return
            plan = setup['plan']
            assert any(p['name'] == plan['pool'] for p in inventory['pools'])
            network = next(n for n in inventory['networks'] if n['name'] == plan['network'] and n['provider'] == 'incus')
            if plan['create_network']:
                assert network['config']['ipv4.address'] == plan['network_address']
                assert network['config']['ipv4.nat'] == 'true'
            assert list(Path('/etc/vm-manager').glob('vm-manager.conf.before-setup-*'))
            vm_name = 'setup-check-' + uuid.uuid4().hex[:10]
            def task(path, body, method='POST'):
                task_id = request(path, body, method, login['csrf'])['task_id']
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    row = next(t for t in request('/api/tasks') if t['id'] == task_id)
                    if row['status'] not in ('running', 'queued'):
                        assert row['status'] == 'completed', row['message']
                        return
                    time.sleep(1)
                raise AssertionError('VM acceptance task timed out; inspect disposable resources')
            task('/api/vms', {'name': vm_name, 'pool': plan['pool'], 'network': plan['network'],
                             'cpu': 1, 'memory_gib': 0.5, 'disk_gib': 4})
            path = '/api/vms/incus:' + vm_name
            task(path + '/action', {'action': 'start'})
            time.sleep(2)
            vm = next(v for v in request('/api/inventory')['vms'] if v['name'] == vm_name)
            assert vm['status'] == 'running'
            task(path + '/action', {'action': 'stop', 'confirm_name': vm_name})
            task(path, {'confirm_name': vm_name}, 'DELETE')
            print('Local-host setup verified: warning-free inventory, confirmed NAT subnet, VM create/start/force-stop/delete through HTTPS; accounts and TLS preserved')
        elif args.phase == 'verify-upgrade':
            assert list(Path('/var/lib/vm-manager-runtime/backups').glob('*.tar.gz'))
            request = client()
            login = request('/api/login', {'username': 'admin', 'password': record['password']})
            assert login['role'] == 'administrator'
            subprocess.run(['systemctl', 'is-active', '--quiet', 'vm-manager', 'vm-manager-storage'], check=True)
            print('Package-manager upgrade verified: services healthy; accounts, changed password and TLS preserved; backup retained')
        else:
            assert subprocess.run(['systemctl', 'is-active', '--quiet', 'vm-manager']).returncode != 0
            print('Uninstall verified: services stopped; database and TLS retained; credentials not regenerated')


if __name__ == '__main__':
    main()
