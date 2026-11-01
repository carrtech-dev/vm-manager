"""Network access for native packages. Never resets or enables a host firewall."""
import ipaddress
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socket
import ssl
import subprocess
import urllib.request
from urllib.parse import urlsplit

CONFIG = Path('/etc/vm-manager')
RECORD = CONFIG / 'network-access.json'
PORT = 8443


def command(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'})
    if check and result.returncode:
        raise RuntimeError(' '.join(args) + ': ' + (result.stderr or result.stdout).strip())
    return result


def settings():
    values = {'VM_MANAGER_HOST': '0.0.0.0', 'VM_MANAGER_MANAGE_FIREWALL': 'yes'}
    path = CONFIG / 'vm-manager.conf'
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            if separator and key.strip().startswith('VM_MANAGER_'):
                parts = shlex.split(value, comments=True)
                values[key.strip()] = ' '.join(parts)
    return values


def addresses():
    """Prefer default-route interfaces; omit link-local/loopback and Docker links."""
    if not shutil.which('ip'):
        return []
    result = command('ip', '-j', 'address', 'show', 'up', 'scope', 'global', check=False)
    if result.returncode:
        return []
    interfaces = json.loads(result.stdout)
    route = command('ip', '-j', 'route', 'show', 'default', check=False)
    routes = json.loads(route.stdout) if route.returncode == 0 and route.stdout.strip() else []
    primary = {r.get('dev') for r in routes}
    found = []
    for interface in sorted(interfaces, key=lambda item: item.get('ifname') not in primary):
        if interface.get('ifname', '').startswith(('lo', 'docker', 'veth', 'br-', 'virbr', 'incusbr')):
            continue
        for item in interface.get('addr_info', []):
            # iproute can emit empty objects for addresses excluded by scope.
            if not item.get('local'):
                continue
            address = ipaddress.ip_address(item['local'])
            if not address.is_loopback and not address.is_link_local and not address.is_unspecified:
                if str(address) not in found:
                    found.append(str(address))
    return found


def urls(values):
    host = values['VM_MANAGER_HOST']
    hosts = addresses() if host in ('0.0.0.0', '::') else [host]
    if host == '0.0.0.0':
        hosts = [value for value in hosts if ':' not in value]
    result = ['https://' + ('[' + value + ']' if ':' in value else value) + ':' + str(PORT)
              for value in (hosts or ['localhost'])]
    public = values.get('VM_MANAGER_PUBLIC_URL', '').rstrip('/')
    if public:
        parsed = urlsplit(public)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('VM_MANAGER_PUBLIC_URL must be an HTTPS origin, such as https://manager.example.com')
        result.insert(0, public)
    return list(dict.fromkeys(result))


def certificate_names():
    names = ['DNS:localhost', 'IP:127.0.0.1', 'IP:::1']
    hostname = socket.gethostname().rstrip('.')
    if hostname and all(part and all(c.isalnum() or c == '-' for c in part) for part in hostname.split('.')):
        names.append('DNS:' + hostname)
    names.extend('IP:' + address for address in addresses())
    return ','.join(dict.fromkeys(names))


def records():
    return json.loads(RECORD.read_text()) if RECORD.exists() else []


def save(items):
    temporary = RECORD.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(items, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, RECORD)


def remove_firewall():
    remaining = records()
    for item in list(remaining):
        if item['manager'] == 'firewalld':
            active = shutil.which('firewall-cmd') and command('firewall-cmd', '--state', check=False).returncode == 0
            if active:
                args = ['firewall-cmd'] + (['--permanent'] if item['permanent'] else [])
            elif item['permanent'] and shutil.which('firewall-offline-cmd'):
                args = ['firewall-offline-cmd']
            elif not item['permanent']:
                remaining.remove(item)
                save(remaining)
                continue
            else:
                raise RuntimeError('Cannot remove VM Manager firewall rule; firewalld tooling is unavailable')
            command(*args, '--zone=' + item['zone'], '--remove-port=8443/tcp')
        elif item['manager'] == 'ufw':
            if not shutil.which('ufw'):
                raise RuntimeError('Cannot remove VM Manager UFW rule; ufw is unavailable')
            command('ufw', '--force', 'delete', 'allow', 'VMManager')
        remaining.remove(item)
        save(remaining)
    RECORD.unlink(missing_ok=True)


def configure_firewall(values):
    enabled = values.get('VM_MANAGER_MANAGE_FIREWALL', 'yes').lower()
    if enabled not in ('yes', 'no', 'true', 'false', '1', '0'):
        raise ValueError('VM_MANAGER_MANAGE_FIREWALL must be yes or no')
    if enabled in ('no', 'false', '0') or values['VM_MANAGER_HOST'] in ('127.0.0.1', '::1', 'localhost'):
        remove_firewall()
        return 'Automatic firewall access disabled by local configuration.'
    owned = records()
    messages = []
    if shutil.which('firewall-cmd') and command('firewall-cmd', '--state', check=False).returncode == 0:
        explicit = values.get('VM_MANAGER_FIREWALL_ZONES', '').split()
        if explicit:
            zones = explicit
        else:
            text = command('firewall-cmd', '--get-active-zones').stdout
            zones = [line.split()[0] for line in text.splitlines() if line and not line[0].isspace()]
            if not zones:
                zones = [command('firewall-cmd', '--get-default-zone').stdout.strip()]
            # Do not turn an intentionally dropping interface into an accessible one.
            zones = [zone for zone in zones if zone not in ('drop', 'block')]
        for zone in dict.fromkeys(zones):
            for permanent in (True, False):
                args = ['firewall-cmd', '--zone=' + zone] + (['--permanent'] if permanent else [])
                rich = command(*args, '--list-rich-rules').stdout
                if re.search(r'\bport port="8443" protocol="tcp"', rich):
                    messages.append('firewalld: existing HTTPS access policy preserved in ' + zone + '.')
                    continue
                query = command(*args, '--query-port=8443/tcp', check=False)
                if query.returncode == 1:
                    item = {'manager': 'firewalld', 'zone': zone, 'permanent': permanent}
                    if item not in owned:
                        owned.append(item)
                        save(owned)
                    command(*args, '--add-port=8443/tcp')
                elif query.returncode:
                    raise RuntimeError('Unable to query firewalld zone ' + zone + ': ' + query.stderr)
        messages.append('firewalld: HTTPS access configured for ' + ', '.join(zones) if zones else
                        'firewalld: active zones intentionally block incoming access; no zone was changed.')
    if shutil.which('ufw') and command('ufw', 'status').stdout.startswith('Status: active'):
        added = command('ufw', 'show', 'added').stdout
        existing = any(shlex.split(line)[:3] == ['ufw', 'allow', 'VMManager']
                       for line in added.splitlines() if line.startswith('ufw '))
        if not existing:
            item = {'manager': 'ufw'}
            if item not in owned:
                owned.append(item)
                save(owned)
            command('ufw', 'allow', 'VMManager')
        messages.append('UFW: VMManager HTTPS access allowed.')
    return '\n'.join(messages) if messages else 'No active firewalld/UFW firewall needed configuration.'


def show_status(values, verify=True):
    ready = True
    if verify:
        host = values['VM_MANAGER_HOST']
        if host in ('0.0.0.0', '::'):
            host = '127.0.0.1' if host == '0.0.0.0' else '::1'
        authority = '[' + host + ']' if ':' in host else host
        try:
            with urllib.request.urlopen('https://' + authority + ':8443/health',
                    context=ssl._create_unverified_context(), timeout=5) as response:
                ready = json.load(response).get('status') == 'ok'
        except Exception:
            ready = False
    if not verify:
        print('\nVM Manager configured. Services require a running systemd host.')
    else:
        print('\nVM Manager is ready.' if ready else '\nVM Manager is not responding; check journalctl -u vm-manager.')
    for url in urls(values):
        print('  Open in your browser: ' + url)
    print('  Sign in with your VM Manager account.')
    if Path('/var/lib/vm-manager/initial-admin.txt').exists():
        print('  Initial username: admin')
        print('  Read your password: sudo cat /var/lib/vm-manager/initial-admin.txt')
    print('  Show these details again: sudo vm-manager status')
    print('  The generated HTTPS certificate is self-signed; trust it or install your own certificate.')
    print('  For VMs on a private NAT network, use the VM platform\'s published address.')
    return ready
