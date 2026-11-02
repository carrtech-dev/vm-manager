"""First-run host provisioning, executed only by the privileged local broker.

The HTTP process supplies a validated plan, never shell commands, repository URLs
or filesystem paths. Intent and progress survive browser disconnects and restarts.
"""
import grp
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
import urllib.request

import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .hypervisor import HypervisorError, Incus, Libvirt, name

RECORD = Path('/var/lib/vm-manager-storage/setup.json')
CONFIG = Path('/etc/vm-manager/vm-manager.conf')
REPO = Path('/etc/yum.repos.d/vm-manager-incus.repo')
QEMU_REPO = Path('/etc/yum.repos.d/vm-manager-qemu-spice.repo')
QEMU_REPO_URL = 'https://copr.fedorainfracloud.org/coprs/ligenix/enterprise-qemu-spice/repo/epel-10/ligenix-enterprise-qemu-spice-epel-10.repo'
INCUS_KEY = Path(__file__).with_name('incus-signing-key.asc')
INCUS_REPO = '''[vm-manager-incus]
name=Incus current stable EL10 (cofob)
mirrorlist=https://incusrpmrepo.cofob.dev/channels/latest/el10/$basearch/mirrorlist
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///usr/lib/vm-manager/app/incus-signing-key.asc
enabled=1
metadata_expire=1h
priority=50
includepkgs=incus* cowsql* raft*
'''
_lock = threading.RLock()
_active = False


class SetupPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['local', 'cluster'] = 'local'
    libvirt: bool = True
    pool: str = Field(default='vm-manager', max_length=63)
    create_pool: bool = True
    network: str = Field(default='vmmbr0', max_length=15)
    create_network: bool = True
    network_address: str = Field(default='', max_length=32)
    bind_address: str = Field(default='0.0.0.0', max_length=45)
    public_url: str = Field(default='', max_length=2048)
    manage_firewall: bool = True
    confirmed: Literal[True]

    @field_validator('pool', 'network')
    @classmethod
    def resource_name(cls, value):
        try:
            return name(value)
        except HypervisorError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator('bind_address')
    @classmethod
    def bind_ip(cls, value):
        return str(ipaddress.ip_address(value))

    @field_validator('public_url')
    @classmethod
    def https_origin(cls, value):
        if value:
            parsed = urlsplit(value)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                    or any(c in value for c in ('\n', '\r', '"', "'", '\\', ' ', '$', '`'))):
                raise ValueError('Use an HTTPS origin without a path or credentials')
            parsed.port  # Validate the port as well.
        return value.rstrip('/')

    @model_validator(mode='after')
    def gateway(self):
        if self.mode == 'local' and self.create_network:
            try:
                address = ipaddress.IPv4Interface(self.network_address)
            except ValueError as exc:
                raise ValueError('Enter the VM bridge gateway and prefix, for example 10.210.0.1/24') from exc
            if (not address.network.is_private or address.ip.is_loopback or address.ip.is_link_local
                    or address.ip.is_multicast or address.network.prefixlen not in range(16, 30)
                    or address.ip in (address.network.network_address, address.network.broadcast_address)
                    or not any(address.network.subnet_of(ipaddress.IPv4Network(cidr)) for cidr in
                               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))):
                raise ValueError('Use a private IPv4 gateway with a /16 to /29 prefix')
            self.network_address = str(address)
        return self


def command(*args, timeout=1800):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                            env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive',
                                 'NEEDRESTART_MODE': 'l', 'LC_ALL': 'C'})
    if result.returncode:
        raise HypervisorError(f'{args[0]} failed: ' + (result.stderr or result.stdout)[-2500:])
    return result.stdout.strip()


def access():
    sys.path.insert(0, '/usr/libexec')
    import vm_manager_access
    return vm_manager_access


def load():
    return json.loads(RECORD.read_text()) if RECORD.exists() else {'status': 'pending'}


def save(record):
    RECORD.parent.mkdir(parents=True, exist_ok=True, mode=0o711)
    temporary = RECORD.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, RECORD)


def progress(message):
    with _lock:
        record = load()
        record.setdefault('steps', []).append({'at': time.time(), 'message': message})
        save(record)


def os_info():
    values = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator:
            values[key] = value.strip('"')
    return values


def interfaces():
    return [dict(name=nic, address=item.address, netmask=item.netmask)
            for nic, addresses in psutil.net_if_addrs().items() for item in addresses
            if item.family == 2 and item.netmask]


def discover():
    result = {'pools': [], 'networks': [], 'incus': False, 'incus_vm': False, 'libvirt': False}
    try:
        incus = Incus(timeout=5)
        result['pools'] = [p['name'] for p in incus.pools()]
        result['networks'] = [dict(name=n['name'], address=n['config'].get('ipv4.address', ''))
                              for n in incus.networks() if n['type'] == 'bridge']
        result['incus'] = True
        result['incus_vm'] = 'qemu' in incus.get('/1.0').get('environment', {}).get('driver', '').split(' | ')
    except Exception:
        pass
    try:
        Libvirt().cli('list', '--all', '--name')
        result['libvirt'] = True
    except Exception:
        pass
    return result


def status():
    with _lock:
        record = load()
        if record['status'] == 'running' and not _active:
            record.update(status='failed', error='Setup was interrupted. Review the saved plan and retry; existing resources are retained.')
            save(record)
        result = dict(record, required=record['status'] != 'completed')
    if result['required'] and result['status'] != 'running':
        values = access().settings()
        os_values = os_info()
        result.update(discover(), interfaces=interfaces(), kvm=Path('/dev/kvm').exists(),
                      os=os_values.get('PRETTY_NAME', ''),
                      supported=(os_values.get('ID') in ('rocky', 'almalinux') and os_values.get('VERSION_ID', '').split('.')[0] == '10')
                                or (os_values.get('ID') == 'debian' and os_values.get('VERSION_ID') == '13'),
                      config={'bind_address': values.get('VM_MANAGER_HOST', '0.0.0.0'),
                              'public_url': values.get('VM_MANAGER_PUBLIC_URL', ''),
                              'manage_firewall': values.get('VM_MANAGER_MANAGE_FIREWALL', 'yes') not in ('no', 'false', '0')})
    return result


def validate_host(plan):
    host = ipaddress.ip_address(plan.bind_address)
    addresses = {item.address.split('%')[0] for values in psutil.net_if_addrs().values()
                 for item in values if item.family in (2, 10)}
    if not host.is_unspecified and str(host) not in addresses:
        raise HypervisorError('The listen address must belong to this host, or use 0.0.0.0 / :: for all interfaces')
    if plan.mode == 'local' and plan.create_network:
        network = ipaddress.ip_interface(plan.network_address).network
        for item in interfaces():
            if item['name'] == plan.network:
                continue  # A retry can reuse the exact bridge it created earlier.
            existing = ipaddress.ip_interface(item['address'] + '/' + item['netmask']).network
            if network.overlaps(existing):
                raise HypervisorError(f'The VM subnet overlaps {item["name"]} ({existing}). Choose a different private subnet.')
        # Include non-default host routes and VPN routes as well as interface nets.
        for route in json.loads(command('ip', '-j', '-4', 'route', 'show', 'table', 'all')):
            cidr = route.get('dst', 'default')
            if cidr != 'default' and route.get('dev') != plan.network and network.overlaps(ipaddress.ip_network(cidr, strict=False)):
                raise HypervisorError(f'The VM subnet overlaps a host route ({cidr}). Choose a different private subnet.')


def install_components(plan):
    info = os_info()
    distro, major = info.get('ID'), info.get('VERSION_ID', '').split('.')[0]
    existing = discover()
    ready = existing['incus_vm']
    if not ready and existing['incus'] and Incus().get('/1.0/instances'):
        raise HypervisorError('The existing Incus installation has instances but no working VM driver. Back up and repair that hypervisor before retrying; setup will not replace it or migrate its database.')
    if ready:
        progress('Reusing the existing working Incus and QEMU installation')
        if plan.libvirt and not existing['libvirt']:
            if distro in ('rocky', 'almalinux') and major == '10':
                command('dnf', '-y', 'install', 'libvirt-daemon-kvm', 'libvirt-daemon-config-network')
            elif distro == 'debian' and major == '13':
                command('apt-get', 'update')
                command('apt-get', '-y', 'install', 'libvirt-daemon-system')
            else:
                raise HypervisorError('Automatic libvirt installation is not supported on this OS')
    elif distro in ('rocky', 'almalinux') and major == '10':
        progress('Enabling EPEL, CRB and the signed current Incus repository')
        command('dnf', '-y', 'install', 'epel-release', 'dnf-plugins-core')
        command('dnf', 'config-manager', '--set-enabled', 'crb')
        command('rpm', '--import', str(INCUS_KEY))
        if not REPO.exists():
            REPO.write_text(INCUS_REPO)
            REPO.chmod(0o644)
        # EL's stock QEMU omits SPICE. Limit the rebuild repository to QEMU
        # and its additional SPICE/graphics/iSCSI runtime dependencies.
        if not QEMU_REPO.exists():
            progress('Enabling the community QEMU build with Incus and SPICE support')
            with urllib.request.urlopen(QEMU_REPO_URL, timeout=30) as response:
                content = response.read(65537)
            if len(content) > 65536 or b'gpgcheck=1' not in content or b'https://download.copr.fedorainfracloud.org/' not in content:
                raise HypervisorError('QEMU repository configuration failed validation')
            QEMU_REPO.write_bytes(content + b'\npriority=50\nincludepkgs=qemu* spice-server virglrenderer* libiscsi*\n')
            QEMU_REPO.chmod(0o644)
        packages = ['incus', 'incus-tools', 'incus-agent', 'qemu-kvm', 'qemu-img', 'edk2-ovmf']
        if plan.libvirt:
            packages += ['libvirt-daemon-kvm', 'libvirt-daemon-config-network']
        progress('Installing Incus, QEMU/KVM and selected libvirt components with DNF')
        command('dnf', '-y', 'install', *packages)
        # EL supplies QEMU under libexec; Incus looks up the upstream binary name.
        binary = Path('/usr/local/bin/qemu-system-x86_64')
        binary.parent.mkdir(parents=True, exist_ok=True)
        if not shutil.which('qemu-system-x86_64') and Path('/usr/libexec/qemu-kvm').is_file() and not binary.exists():
            binary.symlink_to('/usr/libexec/qemu-kvm')
    elif distro == 'debian' and major == '13':
        progress('Installing Incus, QEMU/KVM and selected libvirt components with APT')
        command('apt-get', 'update')
        packages = ['incus', 'qemu-system-x86', 'qemu-utils', 'ovmf']
        if plan.libvirt:
            packages += ['libvirt-daemon-system']
        command('apt-get', '-y', 'install', *packages)
    else:
        raise HypervisorError('Automatic host setup supports Debian 13 and Rocky/AlmaLinux 10 only')
    progress('Starting hypervisor services and granting the VM Manager account access')
    command('systemctl', 'enable', '--now', 'incus.service')
    if 'qemu' not in Incus().get('/1.0').get('environment', {}).get('driver', '').split(' | '):
        # Retry a daemon whose VM driver failed before QEMU was installed.
        command('systemctl', 'restart', 'incus.service')
    if 'qemu' not in Incus().get('/1.0').get('environment', {}).get('driver', '').split(' | '):
        raise HypervisorError('Incus VM support is unavailable; inspect journalctl -u incus for the QEMU feature-check error')
    if plan.libvirt:
        if distro == 'debian':
            command('systemctl', 'enable', '--now', 'libvirtd.socket')
        else:
            command('systemctl', 'enable', '--now', 'virtqemud.socket', 'virtnetworkd.socket',
                    'virtstoraged.socket', 'virtnodedevd.socket', 'virtnwfilterd.socket', 'virtsecretd.socket')
    for group in ('incus-admin', 'libvirt'):
        try:
            grp.getgrnam(group)
        except KeyError:
            continue
        command('usermod', '-a', '-G', group, 'vm-manager')


def provision(plan):
    incus = Incus()
    networks = incus.networks()
    if plan.create_network:
        requested = ipaddress.ip_interface(plan.network_address).network
        for existing in networks:
            address = existing['config'].get('ipv4.address', '')
            if existing['name'] != plan.network and '/' in address and requested.overlaps(ipaddress.ip_interface(address).network):
                raise HypervisorError('The VM subnet overlaps existing Incus network ' + existing['name'])
    pools = incus.pools()
    pool = next((p for p in pools if p['name'] == plan.pool), None)
    if not pool:
        if not plan.create_pool:
            raise HypervisorError('The selected existing storage pool was not found')
        progress('Creating directory storage pool ' + plan.pool + ' on the host filesystem')
        incus.request('POST', '/1.0/storage-pools', {'name': plan.pool, 'driver': 'dir', 'description': 'VM Manager first-run setup'})
    elif plan.create_pool and pool['description'] != 'VM Manager first-run setup':
        raise HypervisorError('That storage pool already exists. Select Use existing storage to preserve its configuration.')
    else:
        progress('Using existing storage pool ' + plan.pool)
    existing = next((n for n in networks if n['name'] == plan.network), None)
    if existing:
        if plan.create_network and (existing['description'] != 'VM Manager first-run setup' or existing['config'].get('ipv4.address') != plan.network_address):
            raise HypervisorError('That network already exists. Select Use existing network to preserve its configuration.')
        if existing['type'] != 'bridge':
            raise HypervisorError('Select an existing managed bridge network')
        progress('Using existing VM network ' + plan.network)
    elif not plan.create_network:
        raise HypervisorError('The selected existing VM network was not found')
    else:
        progress('Creating private NAT bridge ' + plan.network + ' at ' + plan.network_address)
        incus.request('POST', '/1.0/networks', {'name': plan.network, 'type': 'bridge',
                      'description': 'VM Manager first-run setup',
                      'config': {'ipv4.address': plan.network_address, 'ipv4.nat': 'true', 'ipv6.address': 'none'}})
    progress('Verifying hypervisor access as the VM Manager service account')
    command('runuser', '-u', 'vm-manager', '--', 'incus', 'list', '--format=json')
    if plan.libvirt:
        command('runuser', '-u', 'vm-manager', '--', 'virsh', '-c', 'qemu:///system', 'list', '--all')
        command('runuser', '-u', 'vm-manager', '--', 'virsh', '-c', 'qemu:///system', 'net-list', '--all')


def configure(plan):
    values = {'VM_MANAGER_HOST': plan.bind_address, 'VM_MANAGER_PUBLIC_URL': plan.public_url,
              'VM_MANAGER_MANAGE_FIREWALL': 'yes' if plan.manage_firewall else 'no'}
    previous = CONFIG.read_text()
    # Back up the previous settings once per attempt; retain unrelated options.
    backup = CONFIG.with_name('vm-manager.conf.before-setup-' + str(time.time_ns()))
    shutil.copy2(CONFIG, backup)
    backup.chmod(0o600)
    lines = [line for line in previous.splitlines() if line.split('=', 1)[0].strip() not in values]
    lines.extend(key + '=' + value for key, value in values.items())
    temporary = CONFIG.with_suffix('.setup.tmp')
    temporary.write_text('\n'.join(lines) + '\n')
    shutil.copystat(CONFIG, temporary)
    os.chown(temporary, CONFIG.stat().st_uid, CONFIG.stat().st_gid)
    os.replace(temporary, CONFIG)
    access().configure_firewall(access().settings())


def execute(plan):
    global _active
    try:
        progress('Checking host addresses and the confirmed setup plan')
        validate_host(plan)
        if plan.mode == 'local':
            if not Path('/dev/kvm').exists():
                raise HypervisorError('KVM is unavailable. Enable hardware or nested virtualization before configuring local hosting, or choose cluster management only.')
            install_components(plan)
            validate_host(plan)  # Package services can introduce additional networks.
            provision(plan)
        progress('Saving confirmed VM Manager settings and updating host firewall access')
        configure(plan)
        # Schedule before completion so a scheduling failure remains retryable.
        command('systemd-run', '--unit=vm-manager-setup-restart', '--on-active=5s',
                '/usr/bin/systemctl', 'restart', 'vm-manager.service')
        progress('Setup complete. VM Manager will reconnect with the confirmed settings.')
        with _lock:
            record = load()
            record.update(status='completed', completed=time.time())
            save(record)
    except Exception as exc:
        with _lock:
            record = load()
            step = (record.get('steps') or [{'message': 'Setup'}])[-1]['message']
            record.update(status='failed', error=(step + ': ' + str(exc))[-3000:])
            save(record)
    finally:
        with _lock:
            _active = False


def start(body):
    global _active
    plan = SetupPlan.model_validate(body)
    with _lock:
        if _active or load()['status'] == 'completed':
            raise HypervisorError('Host setup is already running or completed')
        validate_host(plan)
        save({'status': 'running', 'plan': plan.model_dump(), 'steps': [], 'started': time.time()})
        _active = True
        threading.Thread(target=execute, args=(plan,), daemon=True).start()
    return {'status': 'running', 'required': True}


def dispatch(operation, body=None):
    if os.environ.get('VM_MANAGER_FIRST_RUN') != '1':
        raise HypervisorError('First-run provisioning is available for native package installations only')
    if operation == 'setup_status':
        return status()
    if operation == 'setup_start':
        return start(body)
    raise HypervisorError('Unsupported setup operation')
