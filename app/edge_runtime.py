"""Fixed-image data plane, reached only through the local privileged broker.

No user supplied commands, paths, container images or proxy configuration.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .hypervisor import HypervisorError
from .kube_workloads import Workload

ROOT = Path('/var/lib/vm-manager-edge')
TUNNEL = Path('/opt/carrtech/vm-manager/tunnel')
TLS = Path('/opt/carrtech/vm-manager/tls')
CADDY = 'caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
HAPROXY = 'haproxy:3.2.6-alpine@sha256:3a819392f5a6af2a9203d1c63ff0feba74f0c940623a558511be06d7d85e361c'
CLOUDFLARED = 'cloudflare/cloudflared:2026.10.3@sha256:faa8e0a1475f137c910be6025f43c8e4092fdd35461b31e19fd1390657a1ff39'
LOCK = threading.RLock()


class Route(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z0-9-]{1,64}$')
    hostname: str = Field(min_length=1, max_length=253, pattern=r'^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$')
    mode: str = Field(pattern=r'^(manual|letsencrypt|cloudflare|manager)$')
    backend: str = Field(default='http', pattern=r'^(http|vm-manager)$')
    upstreams: list[str] = Field(min_length=1, max_length=32)
    certificate: str = Field(default='', max_length=65536, repr=False)
    private_key: str = Field(default='', max_length=65536, repr=False)
    email: str = Field(default='', max_length=254, pattern=r'^$|^[A-Za-z0-9.!+_-]+@[A-Za-z0-9.-]+$')

    @field_validator('hostname')
    @classmethod
    def hostname_valid(cls, value):
        try:
            return str(ipaddress.IPv4Address(value))
        except ValueError:
            return Workload.hostname_valid(value)

    @field_validator('upstreams')
    @classmethod
    def upstreams_valid(cls, values):
        for value in values:
            host, sep, port = value.rpartition(':')
            if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError('Upstreams must be IPv4:port')
            ipaddress.IPv4Address(host)
        return values


class Listener(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z0-9-]{1,64}$')
    port: int = Field(ge=1024, le=65535)
    upstreams: list[str] = Field(min_length=1, max_length=64)
    _upstreams = field_validator('upstreams')(Route.upstreams_valid.__func__)


class Configuration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    address: str
    routes: list[Route] = Field(max_length=100)
    listeners: list[Listener] = Field(default_factory=list, max_length=200)
    tunnel_token: str = Field(default='', max_length=8192, pattern=r'^[A-Za-z0-9=+/_-]*$', repr=False)
    migrate_tunnel: bool = False

    @field_validator('address')
    @classmethod
    def address_valid(cls, value):
        return str(ipaddress.IPv4Address(value))


def run(args, *, check=True):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=160)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HypervisorError('Load balancer runtime unavailable') from exc
    if check and p.returncode:
        # Runtime errors must never include generated config, keys or tunnel tokens.
        raise HypervisorError('Load balancer command failed: ' + ' '.join(args[:3]) + '. Check the managed container logs on the host.')
    return p


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.next')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(value)
    os.replace(tmp, path)


def container(name):
    p = run(['docker', 'inspect', name], check=False)
    if p.returncode:
        return None
    obj = json.loads(p.stdout)[0]
    if obj.get('Config', {}).get('Labels', {}).get('dev.carrtech.vm-manager.edge') != 'true':
        raise HypervisorError('Managed load balancer container name is occupied by another application')
    return obj


def info():
    legacy = {}
    saved = ROOT / 'tunnel-origin.json'
    source = saved if saved.exists() else TUNNEL / 'config.yml'
    if source.exists() and (TUNNEL / 'credentials.json').exists():
        import yaml
        config = yaml.safe_load(source.read_text())
        creds = json.loads((TUNNEL / 'credentials.json').read_text())
        legacy = {'id': creds['TunnelID'], 'account_id': creds['AccountTag'],
                  'hostnames': [r['hostname'] for r in config.get('ingress', []) if r.get('hostname')]}
    return {'existing_tunnel': legacy}


def certificate_id(route):
    return route.id + '-' + hashlib.sha256((route.certificate + route.private_key).encode()).hexdigest()[:16]


def caddyfile(config):
    lines = ['{', ' admin 127.0.0.1:20191', ' auto_https disable_redirects', ' servers {', '  trusted_proxies static 127.0.0.1/32', ' }', '}']
    for route in config.routes:
        tunnel = route.mode == 'cloudflare'
        site = 'http://' + route.hostname + ':18080' if tunnel else 'https://' + route.hostname
        lines += [site + ' {', ' bind ' + ('127.0.0.1' if tunnel else config.address)]
        if tunnel:
            lines += [' @visitor_http header X-Forwarded-Proto http', ' redir @visitor_http https://{host}{uri} 308']
        if route.mode == 'manager':
            lines += [' tls /edge/manager.crt /edge/manager.key']
        elif route.mode == 'manual':
            lines += [' tls /edge/' + certificate_id(route) + '.crt /edge/' + certificate_id(route) + '.key']
        elif route.mode == 'letsencrypt':
            if not route.email:
                raise HypervisorError('An email address is required for Let’s Encrypt')
            lines += [' tls {', '  issuer acme {', '   dir https://acme-v02.api.letsencrypt.org/directory',
                      '   email ' + route.email, '   disable_http_challenge', '  }', ' }']
        upstreams = ['https://' + u if (route.id.startswith('manager') or route.backend == 'vm-manager') else 'http://' + u for u in route.upstreams]
        lines += [' reverse_proxy ' + ' '.join(upstreams) + ' {', '  lb_policy round_robin']
        if (route.id.startswith('manager') or route.backend == 'vm-manager'):
            lines += ['  header_up Host {http.request.host}', '  transport http {', '   tls',
                      '   tls_trust_pool file /edge/manager.crt', '  }']
        else:
            lines += ['  fail_duration 15s', '  lb_try_duration 5s']
        lines += [' }', '}']
    # Unknown tunnel hostnames never reach an application.
    lines += ['http://:18080 {', ' bind 127.0.0.1', ' respond 404', '}']
    return '\n'.join(lines) + '\n'


def haproxyfile(config):
    lines = ['global', ' log stdout format raw local0', 'defaults', ' mode tcp', ' timeout connect 5s',
             ' timeout client 1h', ' timeout server 1h']
    used = set()
    for listener in config.listeners:
        if listener.port in (8443, 18080, 20191) or listener.port in used:
            raise HypervisorError('Listener port is reserved or already in use')
        used.add(listener.port)
        lines += ['listen ' + listener.id + '-' + str(listener.port), ' bind ' + config.address + ':' + str(listener.port), ' balance roundrobin']
        lines += [' server n' + str(i) + ' ' + u + ' check init-state down inter 1s' for i, u in enumerate(listener.upstreams)]
    return '\n'.join(lines) + '\n'


def ensure(name, image, command, mounts):
    obj = container(name)
    if obj:
        if not obj['State']['Running']:
            run(['docker', 'start', name])
        return
    args = ['docker', 'run', '-d', '--name', name, '--label', 'dev.carrtech.vm-manager.edge=true', '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
            '--user', '0', '--network', 'host', '--restart', 'unless-stopped', '--security-opt', 'no-new-privileges:true',
            '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3']
    for source, target, readonly in mounts:
        args += ['-v', str(source) + ':' + target + (':ro' if readonly else '')]
    run(args + [image] + command)


def sync_tcp():
    previous = container('vmm-edge-tcp')
    ensure('vmm-edge-tcp', HAPROXY, ['haproxy', '-W', '-db', '-f', '/edge/haproxy.cfg'], [(ROOT, '/edge', True)])
    # A newly started master reads the new file itself. Reloading during its
    # initial worker startup can fail socket transfer and leave a dead listener.
    if previous and previous['State']['Running']:
        run(['docker', 'kill', '--signal', 'USR2', 'vmm-edge-tcp'])


def available(address, port):
    try:
        with socket.socket() as sock:
            sock.bind((address, port))
    except OSError as exc:
        raise HypervisorError('Port ' + str(port) + ' is already occupied on ' + address + '; existing services were retained') from exc


def status():
    result = {'running': False, 'components': {}}
    for key, name in [('https', 'vmm-edge-https'), ('tcp', 'vmm-edge-tcp')]:
        obj = container(name)
        result['components'][key] = bool(obj and obj['State']['Running'])
    result['running'] = result['components']['https']
    return result


def migrate_tunnel():
    """Switch the established connector only after the proxy origin is healthy."""
    if not (TUNNEL / 'config.yml').exists():
        return
    import yaml
    path = TUNNEL / 'config.yml'
    original = path.read_text()
    current = yaml.safe_load(original)
    if current.get('ingress') == [{'service': 'http://127.0.0.1:18080'}]:
        return
    hosts = [r['hostname'] for r in current['ingress'] if r.get('hostname')]
    if not hosts:
        raise HypervisorError('Existing tunnel has no hostnames to verify')
    for host in hosts:
        req = urllib.request.Request('http://127.0.0.1:18080/health', headers={'Host': host, 'X-Forwarded-Proto': 'https'})
        with urllib.request.urlopen(req, timeout=8) as response:
            if json.load(response).get('service') != 'vm-manager':
                raise HypervisorError('VM Manager proxy health verification failed; tunnel retained')
    private(ROOT / 'tunnel-origin.json', original)
    current['ingress'] = [{'service': 'http://127.0.0.1:18080'}]
    # Preserve the connector's existing file owner and mode.
    path.write_text(json.dumps(current, indent=2))
    run(['docker', 'restart', 'vm-manager-tunnel-1'])
    try:
        for host in hosts:
            for attempt in range(8):
                try:
                    with urllib.request.urlopen(urllib.request.Request('https://' + host + '/health', headers={'User-Agent':'CarrTech-VM-Manager-Deploy/1.0'}), timeout=5) as response:
                        if json.load(response).get('service') == 'vm-manager':
                            break
                except Exception:
                    pass
                time.sleep(1)
            else:
                raise HypervisorError('Public proxy verification failed; original tunnel restored')
    except Exception:
        path.write_text(original)
        run(['docker', 'restart', 'vm-manager-tunnel-1'])
        raise


def apply(body):
    config = Configuration.model_validate(body)
    local = {a.address for addresses in psutil.net_if_addrs().values() for a in addresses if a.family == socket.AF_INET}
    if config.address not in local:
        raise HypervisorError('Load balancer address must belong to this host')
    with LOCK:
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not container('vmm-edge-https'):
            available(config.address, 443)
            available('127.0.0.1', 18080)
            available('127.0.0.1', 20191)
        old = ROOT / 'active.json'
        previous = json.loads(old.read_text()) if old.exists() else None
        old_ports = {l['port'] for l in previous.get('listeners', [])} if previous else set()
        for listener in config.listeners:
            if listener.port not in old_ports:
                available(config.address, listener.port)
        for route in config.routes:
            if route.mode == 'manual':
                if not route.certificate or not route.private_key:
                    raise HypervisorError('Supply a PEM certificate chain and private key')
                private(ROOT / (certificate_id(route) + '.crt'), route.certificate)
                private(ROOT / (certificate_id(route) + '.key'), route.private_key)
                try:
                    ipaddress.IPv4Address(route.hostname)
                    check_host = '-checkip'
                except ValueError:
                    check_host = '-checkhost'
                checked = run(['openssl', 'x509', '-in', str(ROOT / (certificate_id(route) + '.crt')), '-noout', '-checkend', '0', check_host, route.hostname], check=False)
                if checked.returncode:
                    raise HypervisorError('Manual certificate is invalid, expired or does not match the route hostname')
        for extension in ('crt', 'key'):
            shutil.copyfile(TLS / ('server.' + extension), ROOT / ('manager.' + extension))
            (ROOT / ('manager.' + extension)).chmod(0o600)
        candidate = caddyfile(config)
        private(ROOT / 'candidate', candidate)
        # Validate before touching the active configuration; docker only runs fixed images.
        run(['docker', 'run', '--rm', '--user', '0', '-v', str(ROOT) + ':/edge:ro', CADDY,
             'caddy', 'validate', '--config', '/edge/candidate', '--adapter', 'caddyfile'])
        tcp = haproxyfile(config)
        private(ROOT / 'haproxy.candidate', tcp)
        if config.listeners:
            run(['docker', 'run', '--rm', '--user', '0', '-v', str(ROOT) + ':/edge:ro', HAPROXY, 'haproxy', '-c', '-f', '/edge/haproxy.candidate'])
        tcp_active = ROOT / 'haproxy.cfg'
        prior_tcp = tcp_active.read_text() if tcp_active.exists() else None
        active = ROOT / 'Caddyfile'
        prior_config = active.read_text() if active.exists() else None
        private(active, candidate)
        try:
            ensure('vmm-edge-https', CADDY, ['caddy', 'run', '--config', '/edge/Caddyfile', '--adapter', 'caddyfile'],
                   [(ROOT, '/edge', True), (ROOT / 'caddy-data', '/data', False), (ROOT / 'caddy-config', '/config', False)])
            run(['docker', 'exec', 'vmm-edge-https', 'caddy', 'reload', '--config', '/edge/Caddyfile', '--adapter', 'caddyfile', '--address', '127.0.0.1:20191'])
            private(tcp_active, tcp)
            if config.listeners:
                running_tcp = container('vmm-edge-tcp')
                if prior_tcp != tcp or not running_tcp or not running_tcp['State']['Running']:
                    sync_tcp()
            elif container('vmm-edge-tcp'):
                run(['docker', 'stop', 'vmm-edge-tcp'])
            if config.tunnel_token:
                private(ROOT / 'tunnel-token', config.tunnel_token)
                ensure('vmm-edge-tunnel', CLOUDFLARED,
                    ['tunnel', '--no-autoupdate', 'run', '--token-file', '/edge/tunnel-token'], [(ROOT, '/edge', True)])
            import ssl
            for attempt in range(10):
                try:
                    context = ssl.create_default_context(cafile=str(ROOT / 'manager.crt'))
                    with urllib.request.urlopen('https://' + config.address + '/health', context=context, timeout=3) as response:
                        if json.load(response).get('service') != 'vm-manager':
                            raise HypervisorError('VM Manager proxy health check failed')
                    for listener in config.listeners:
                        with socket.create_connection((config.address, listener.port), timeout=2):
                            pass
                    break
                except Exception:
                    if attempt == 9:
                        raise HypervisorError('Load balancer listener health verification failed')
                    time.sleep(0.2)
            if config.migrate_tunnel:
                migrate_tunnel()
            private(old, config.model_dump_json())
        except Exception:
            if prior_tcp is not None:
                private(tcp_active, prior_tcp)
                if container('vmm-edge-tcp'):
                    if previous and previous.get('listeners'):
                        try:
                            sync_tcp()
                        except HypervisorError:
                            pass  # Still restore HTTPS if TCP recovery itself fails.
                    else:
                        run(['docker', 'stop', 'vmm-edge-tcp'], check=False)
            elif container('vmm-edge-tcp'):
                run(['docker', 'stop', 'vmm-edge-tcp'], check=False)
            if prior_config is not None:
                private(active, prior_config)
                run(['docker', 'exec', 'vmm-edge-https', 'caddy', 'reload', '--config', '/edge/Caddyfile', '--adapter', 'caddyfile', '--address', '127.0.0.1:20191'], check=False)
            raise
        for attempt in range(10):
            observed = status()
            if observed['running'] and (not config.listeners or observed['components']['tcp']):
                break
            time.sleep(0.2)
        return observed


def dispatch(operation, request):
    if operation == 'edge_info':
        return info()
    if operation == 'edge_status':
        return status()
    if operation == 'edge_apply':
        return apply(request['body'])
    raise HypervisorError('Unsupported load balancer operation')
