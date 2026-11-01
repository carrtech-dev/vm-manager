#!/usr/bin/env python3
"""Acceptance checks INSIDE A DISPOSABLE package test host, never production.

Run `prepare` after initial install, `verify-upgrade` after an actual APT/DNF
upgrade, and `verify-removed` after uninstall. Requires an explicit root-owned
/root/vmm-distribution-disposable marker to avoid touching real account data.
Changes only the disposable host's initial admin password; never prints it.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import pwd
import secrets
import sqlite3
import ssl
import stat
import subprocess
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
    parser.add_argument('phase', choices=('prepare', 'verify-upgrade', 'verify-removed'))
    args = parser.parse_args()
    marker = Path('/root/vmm-distribution-disposable')
    if os.geteuid() or not marker.exists() or marker.stat().st_uid != 0:
        raise SystemExit('Requires root and explicit disposable-host marker; NEVER use production data')
    os.umask(0o077)
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
        if args.phase == 'verify-upgrade':
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
