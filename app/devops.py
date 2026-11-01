"""Persistent K3s orchestration across explicitly linked VM Manager hosts."""
import ipaddress
import json
import sqlite3
import subprocess
import threading
import time
import uuid
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from . import devops_guest, kubernetes
from .hypervisor import HypervisorError, HypervisorNotFound
from .task_output import emit

VERSION = 'v1.35.9+k3s1'
ASSETS = {
    'x86_64': ['k3s', '897acb21cca57d477a211102251a6591450ce767344a9faaa121a1007ca9fa9a'],
    'aarch64': ['k3s-arm64', '6176dd12c512e177af998e6035920f6283c5fdbd5adfdff85c103a7fdf2a00b5'],
}
ACTIVE = ('queued', 'deploying', 'deleting')


class Hardware(BaseModel):
    host_id: str = Field(default='', pattern=r'^(|[0-9a-f]{32})$')
    cpu: int = Field(default=2, ge=1, le=128)
    memory_gib: int = Field(default=4, ge=1, le=512)
    disk_gib: int = Field(default=30, ge=20, le=4096)
    pool: str = Field(min_length=1, max_length=63, pattern=r'^[\w-]+$')
    network: str = Field(default='', max_length=63, pattern=r'^[\w-]*$')


class Network(BaseModel):
    name: str = Field(pattern=r'^[a-z][a-z0-9-]{0,30}$')
    kind: str = Field(default='nat', pattern=r'^(nat|lan)$')
    subnet: str = '10.210.0.1/24'
    dhcp: bool = True
    nat: bool = True
    parents: dict[str, str] = Field(default_factory=dict, max_length=32)

    @model_validator(mode='after')
    def validate_network(self):
        if self.kind == 'nat':
            try:
                subnet = ipaddress.ip_interface(self.subnet)
                if subnet.version != 4 or not 16 <= subnet.network.prefixlen <= 28 or subnet.ip in (subnet.network.network_address, subnet.network.broadcast_address):
                    raise ValueError()
            except ValueError:
                raise ValueError('Enter an IPv4 gateway/subnet between /16 and /28')
            if not self.dhcp:
                raise ValueError('Automatic VM provisioning requires DHCP')
        for parent in self.parents.values() if self.kind == 'lan' else []:
            if not parent or len(parent) > 63 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-' for c in parent):
                raise ValueError('Invalid parent interface')
        return self


class Request(BaseModel):
    name: str = Field(pattern=r'^[a-z][a-z0-9-]{0,30}[a-z0-9]$|^[a-z]$')
    masters: list[Hardware] = Field(min_length=1, max_length=5)
    workers: list[Hardware] = Field(default_factory=list, max_length=20)
    new_network: Network | None = None
    pod_cidr: str = '10.42.0.0/16'
    service_cidr: str = '10.43.0.0/16'

    @model_validator(mode='after')
    def validate_cluster(self):
        if len(self.masters) not in (1, 3, 5):
            raise ValueError('Choose one, three or five masters for etcd quorum')
        if any(n.cpu < 2 or n.memory_gib < 2 for n in self.masters):
            raise ValueError('Masters need at least 2 vCPUs and 2 GiB memory')
        try:
            pod, service = [ipaddress.ip_network(c) for c in (self.pod_cidr, self.service_cidr)]
            if any(c.version != 4 or not 8 <= c.prefixlen <= limit for c, limit in ((pod, 20), (service, 24))) or pod.overlaps(service):
                raise ValueError()
        except ValueError:
            raise ValueError('Use non-overlapping IPv4 pod (/8–/20) and service (/8–/24) CIDRs')
        hosts = {n.host_id for n in self.masters + self.workers}
        if self.new_network:
            if self.new_network.kind == 'nat' and len(hosts) > 1:
                raise ValueError('A new private NAT network supports one host; choose shared LAN for multiple hosts')
            if self.new_network.kind == 'lan' and any(not self.new_network.parents.get(h) for h in hosts):
                raise ValueError('Select a LAN parent interface on every chosen host')
        elif any(not n.network for n in self.masters + self.workers):
            raise ValueError('Choose an existing network for every VM')
        return self


class NodeRequest(Hardware):
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    cluster_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    name: str = Field(pattern=r'^[a-z][a-z0-9-]{0,62}$')
    role: str = Field(pattern=r'^(master|worker)$')
    new_network: Network | None = None
    pod_cidr: str
    service_cidr: str

    @model_validator(mode='after')
    def valid_cidrs(self):
        for cidr in (self.pod_cidr, self.service_cidr):
            if ipaddress.ip_network(cidr).version != 4:
                raise ValueError('IPv4 CIDRs are required')
        return self


class Install(BaseModel):
    token: str = Field(min_length=32, max_length=512, pattern=r'^[A-Za-z0-9:._-]+$')
    server: str = ''

    @model_validator(mode='after')
    def valid_server(self):
        if self.server:
            self.server = str(ipaddress.IPv4Address(self.server))
        return self


def initialize(store):
    with store.db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS devops_clusters(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                spec TEXT NOT NULL, nodes TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
                created REAL NOT NULL, task_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS devops_nodes(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                spec TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '');
            CREATE UNIQUE INDEX IF NOT EXISTS devops_cluster_name ON devops_clusters(name) WHERE status!='deleted';
            CREATE UNIQUE INDEX IF NOT EXISTS devops_node_name ON devops_nodes(name) WHERE status!='deleted';
            CREATE TABLE IF NOT EXISTS devops_networks(name TEXT PRIMARY KEY, cluster_id TEXT NOT NULL);
        ''')
        db.execute("UPDATE devops_clusters SET status='interrupted',message='Manager restarted. Inspect nodes, then retry or delete.' WHERE status IN ('queued','deploying','deleting')")
        db.execute("UPDATE devops_nodes SET status='interrupted',message='Manager restarted; retry to inspect existing VMs.' WHERE status IN ('queued','preparing','deleting')")


def catalog(store):
    with store.db() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM devops_clusters ORDER BY created DESC')]
    for row in rows:
        row['spec'] = json.loads(row['spec'])
        row['nodes'] = json.loads(row['nodes'])
        row['version'] = VERSION
    return rows


def row(store, table, identity):
    assert table in ('devops_nodes', 'devops_clusters')
    with store.db() as db:
        found = db.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
    if not found:
        raise HTTPException(404, 'Deployment record not found')
    result = dict(found)
    result['spec'] = json.loads(result['spec'])
    if 'nodes' in result:
        result['nodes'] = json.loads(result['nodes'])
    return result


def wait_for(fn, message, timeout=900, interval=3):
    end = time.monotonic() + timeout
    while True:
        result = fn()
        if result:
            return result
        if time.monotonic() >= end:
            raise HypervisorError(message)
        time.sleep(interval)


class Agent:
    def __init__(self, store, incus, submit, remove=None):
        self.store, self.incus, self.submit = store, incus, submit
        self.remove = remove
        self.lock = threading.RLock()

    def options(self):
        networks = self.incus.get('/1.0/networks?recursion=1')
        return {'version': VERSION, 'pools': [{'name': p['name'], 'driver': p['driver']} for p in self.incus.pools()],
                'networks': [{'name': n['name'], 'type': n['type'], 'managed': n.get('managed'), 'config': n.get('config', {})} for n in networks],
                'interfaces': [n['name'] for n in networks if n['type'] in ('physical', 'bridge') and n.get('config', {}).get('ipv4.nat') != 'true'],
                'protocol': 1}

    def validate(self, spec):
        with self.store.db() as db:
            reserved = db.execute("SELECT id FROM devops_nodes WHERE name=? AND status!='deleted' AND id!=?", (spec.name, spec.id)).fetchone()
        if reserved:
            raise HypervisorError('Node name is already reserved by another deployment')
        if spec.pool not in {p['name'] for p in self.incus.pools()}:
            raise HypervisorError('Selected storage pool is unavailable')
        networks = self.incus.get('/1.0/networks?recursion=1')
        selected = next((n for n in networks if n['name'] == spec.network), None)
        for n in networks:
            address = n.get('config', {}).get('ipv4.address', '')
            if '/' in address:
                net = ipaddress.ip_interface(address).network
                if any(net.overlaps(ipaddress.ip_network(c)) for c in (spec.pod_cidr, spec.service_cidr)):
                    raise HypervisorError('Pod/service CIDRs overlap a host network')
        if spec.new_network:
            plan = spec.new_network
            with self.store.db() as db:
                owned = db.execute('SELECT cluster_id FROM devops_networks WHERE name=?', (plan.name,)).fetchone()
            if selected and (not owned or owned['cluster_id'] != spec.cluster_id or selected.get('description') != 'VM Manager K3s ' + spec.cluster_id):
                raise HypervisorError('New network name already exists; select it as an existing network')
            if plan.kind == 'nat':
                new_subnet = ipaddress.ip_interface(plan.subnet).network
                if any(new_subnet.overlaps(ipaddress.ip_network(c)) for c in (spec.pod_cidr, spec.service_cidr)):
                    raise HypervisorError('New network overlaps pod/service CIDRs')
                for network in networks:
                    address = network.get('config', {}).get('ipv4.address', '')
                    if network['name'] != plan.name and '/' in address and new_subnet.overlaps(ipaddress.ip_interface(address).network):
                        raise HypervisorError('New subnet overlaps an existing host network')
            else:
                parent = plan.parents.get(spec.host_id)
                interface = next((n for n in networks if n['name'] == parent and n['type'] in ('physical', 'bridge')), None)
                if not interface:
                    raise HypervisorError('LAN parent interface is unavailable')
                if interface.get('config', {}).get('ipv4.nat') == 'true':
                    raise HypervisorError('Use a NAT bridge as an existing network, not as a macvlan parent')
        elif not selected or not selected.get('managed') or selected['type'] not in ('bridge', 'macvlan'):
            raise HypervisorError('Choose a managed Incus bridge or macvlan network')
        elif selected['type'] == 'bridge' and (selected.get('config', {}).get('ipv4.address') in (None, 'none') or selected.get('config', {}).get('ipv4.dhcp') == 'false'):
            raise HypervisorError('The selected bridge needs IPv4 and DHCP')
        try:
            existing = self.incus.vm(spec.name)
        except HypervisorNotFound:
            existing = None
        if existing and existing.get('config', {}).get('user.vmm.devops-node') != spec.id:
            raise HypervisorError('A VM with this node name already exists')
        return {'ready': True}

    def reserve(self, spec):
        with self.lock:
            self.validate(spec)
            with self.store.db() as db:
                existing = db.execute('SELECT spec FROM devops_nodes WHERE id=?', (spec.id,)).fetchone()
                if existing:
                    if json.loads(existing['spec']) != spec.model_dump():
                        raise HypervisorError('Node reservation does not match this request')
                else:
                    try:
                        db.execute("INSERT INTO devops_nodes(id,name,spec,status) VALUES(?,?,?,'reserved')", (spec.id, spec.name, spec.model_dump_json()))
                    except sqlite3.IntegrityError:
                        raise HypervisorError('Node name is already reserved by another deployment')
        return {'id': spec.id}

    def record(self, identity):
        record = row(self.store, 'devops_nodes', identity)
        if record['status'] in ('queued', 'preparing', 'deleting') and record['task_id']:
            with self.store.db() as db:
                task = db.execute('SELECT status,message FROM tasks WHERE id=?', (record['task_id'],)).fetchone()
            if task and task['status'] in ('failed', 'interrupted', 'completed'):
                status = 'failed' if task['status'] == 'failed' else 'interrupted'
                message = task['message'] or 'Node task ended before updating its deployment record'
                self.update(identity, status, message)
                record.update(status=status, message=message)
        return record

    def owned(self, identity):
        record = row(self.store, 'devops_nodes', identity)
        spec = NodeRequest.model_validate(record['spec'])
        vm = self.incus.vm(spec.name)
        if vm.get('type') != 'virtual-machine' or vm.get('config', {}).get('user.vmm.devops-node') != identity:
            raise HypervisorError('VM ownership does not match this deployment; no changes made')
        return record, spec, vm

    def guest(self, identity, action, **body):
        _, spec, _ = self.owned(identity)
        try:
            result = subprocess.run(['incus', '--force-local', '--project', 'default', 'exec', spec.name,
                '--mode=non-interactive', '--', '/usr/bin/python3', '-c', devops_guest.SCRIPT],
                input=json.dumps(dict(body, action=action)), capture_output=True, text=True, timeout=100 if action == 'manage' else 20)
            if result.returncode:
                raise HypervisorError('Guest agent command unavailable; inspect the node VM')
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise HypervisorError('Guest agent unavailable; wait for Ubuntu to boot') from exc

    def update(self, identity, status, message=''):
        with self.store.db() as db:
            db.execute('UPDATE devops_nodes SET status=?,message=? WHERE id=?', (status, message, identity))
            if status in ('queued', 'deleting'):
                db.execute("UPDATE devops_nodes SET task_id='' WHERE id=?", (identity,))

    def manage(self, identity, user, operation):
        record, spec, _ = self.owned(identity)
        if spec.role != 'master' or record['status'] != 'installed':
            raise HTTPException(409, 'An installed master is required')
        body = operation.model_dump()
        if operation.action == 'create':
            body['manifest'] = kubernetes.kube_workloads.manifest(operation.resource, operation.name, operation.namespace, operation.workload)
        result = self.guest(identity, 'manage', request=body)
        if result.get('error'):
            raise HTTPException(409, result['error'])
        if operation.action not in ('inventory', 'edge-inventory', 'edge-status'):
            self.store.audit(user['username'], 'devops.' + operation.resource + '.' + operation.action,
                             spec.name, operation.namespace + '/' + operation.name)
        return result

    def prepare(self, identity, user, install):
        with self.lock:
            record = self.record(identity)
            if record['status'] in ('queued', 'preparing'):
                return {'task_id': record['task_id']}
            self.update(identity, 'queued')
            spec = NodeRequest.model_validate(record['spec'])
            def work():
                self.update(identity, 'preparing')
                try:
                    self.provision(spec)
                    def booted():
                        try: return self.guest(identity, 'probe').get('ready')
                        except HypervisorError: return False
                    wait_for(booted, 'Ubuntu guest agent/cloud-init did not become ready')
                    vm_state = self.incus.get('/1.0/instances/' + quote(spec.name) + '/state')
                    mac = self.incus.vm(spec.name).get('config', {}).get('volatile.eth0.hwaddr', '').lower()
                    addresses = [a['address'] for iface_name, interface in vm_state.get('network', {}).items() if (mac and interface.get('hwaddr', '').lower() == mac) or (not mac and iface_name in ('eth0', 'enp5s0'))
                        for a in interface.get('addresses', []) if a.get('family') == 'inet' and a.get('scope') == 'global'
                        and not ipaddress.ip_address(a['address']).is_loopback]
                    if not addresses:
                        raise HypervisorError('Node has no IPv4 address; check network DHCP')
                    address = addresses[0]
                    if install.server and not self.guest(identity, 'connect', address=install.server).get('ready'):
                        raise HypervisorError('Node cannot reach the first master on TCP 6443; use shared/routed networks across hosts')
                    token = install.token if install.server else self.guest(identity, 'token')['token']
                    self.guest(identity, 'launch', token=token, server=install.server, address=address,
                        name=spec.name, role=spec.role, version=VERSION, assets=ASSETS, pod_cidr=spec.pod_cidr,
                        service_cidr=spec.service_cidr, installer=devops_guest.INSTALLER)
                    def installed():
                        data = self.guest(identity, 'status')
                        if data['status'] == 'failed': raise HypervisorError(data['message'])
                        return data['status'] == 'installed'
                    wait_for(installed, 'K3s installation timed out; inspect the node installer log', timeout=1200)
                    self.update(identity, 'installed', address)
                    return 'K3s installed; waiting for cluster node readiness'
                except Exception as exc:
                    self.update(identity, 'failed', str(exc)[:1000])
                    raise
            try:
                result = self.submit(user, 'devops.node.prepare', spec.name, work, resources={'incus:' + spec.name})
                with self.store.db() as db:
                    db.execute('UPDATE devops_nodes SET task_id=? WHERE id=?', (result['task_id'], identity))
                return result
            except Exception:
                self.update(identity, 'failed', 'Could not queue installation')
                raise

    def bridge_address(self, network_name):
        network = self.incus.get('/1.0/networks/' + quote(network_name))
        if network['type'] != 'bridge':
            return None
        subnet = ipaddress.ip_interface(network['config']['ipv4.address'])
        used = {str(subnet.ip)}
        used.update(lease['address'] for lease in self.incus.get('/1.0/networks/' + quote(network_name) + '/leases'))
        for vm in self.incus.get('/1.0/instances?recursion=1'):
            for nic in (vm.get('expanded_devices') or vm.get('devices', {})).values():
                if nic.get('type') == 'nic' and nic.get('network') == network_name:
                    used.add(nic.get('ipv4.address', ''))
        for address in subnet.network.hosts():
            if str(address) not in used:
                return str(address)
        raise HypervisorError('No free IPv4 address remains on the selected network')

    def provision(self, spec):
        with self.lock:
            self.validate(spec)
            if spec.new_network:
                plan = spec.new_network
                names = {n['name'] for n in self.incus.get('/1.0/networks?recursion=1')}
                if plan.name not in names:
                    with self.store.db() as db:
                        db.execute('INSERT INTO devops_networks VALUES(?,?) ON CONFLICT(name) DO UPDATE SET cluster_id=excluded.cluster_id', (plan.name, spec.cluster_id))
                    config = ({'ipv4.address': plan.subnet, 'ipv4.nat': str(plan.nat).lower(), 'ipv4.dhcp': str(plan.dhcp).lower(), 'ipv6.address': 'none'}
                              if plan.kind == 'nat' else {'parent': plan.parents[spec.host_id]})
                    self.incus.request('POST', '/1.0/networks', {'name': plan.name,
                        'type': 'bridge' if plan.kind == 'nat' else 'macvlan', 'description': 'VM Manager K3s ' + spec.cluster_id, 'config': config})
            try:
                self.owned(spec.id)
            except HypervisorNotFound:
                emit('Creating Ubuntu node ' + spec.name + '\n')
                nic = {'type': 'nic', 'name': 'eth0', 'network': spec.network}
                address = self.bridge_address(spec.network)
                if address:
                    nic['ipv4.address'] = address
                self.incus.request('POST', '/1.0/instances', {'name': spec.name, 'type': 'virtual-machine', 'profiles': [],
                    'source': {'type': 'image', 'mode': 'pull', 'server': 'https://images.linuxcontainers.org', 'protocol': 'simplestreams', 'alias': 'ubuntu/24.04/cloud'},
                    'description': 'K3s ' + spec.role + ' · VM Manager DevOps',
                    'config': {'limits.cpu': str(spec.cpu), 'limits.memory': str(spec.memory_gib) + 'GiB', 'security.secureboot': 'false',
                        'boot.autostart': 'true', 'user.vm-manager': 'true', 'user.vmm.devops-node': spec.id,
                        'user.vmm.devops-cluster': spec.cluster_id},
                    'devices': {'root': {'type': 'disk', 'path': '/', 'pool': spec.pool, 'size': str(spec.disk_gib) + 'GiB', 'boot.priority': '1'},
                                'eth0': nic}})
            _, _, vm = self.owned(spec.id)
            if vm['status'].lower() == 'stopped':
                self.incus.request('PUT', '/1.0/instances/' + quote(spec.name) + '/state', {'action': 'start', 'timeout': 60})
            elif vm['status'].lower() != 'running':
                raise HypervisorError('Node is neither stopped nor running; inspect VM power state')

    def delete(self, identity, user):
        with self.lock:
            record = self.record(identity)
            if record['status'] in ('queued', 'preparing', 'deleting'):
                raise HypervisorError('Node task is still active; wait before deleting')
            spec = NodeRequest.model_validate(record['spec'])
            self.update(identity, 'deleting')
            def work():
                try:
                    try:
                        _, _, vm = self.owned(identity)
                        if self.remove:
                            self.remove(spec.name, user['username'])
                        else:
                            if vm['status'].lower() != 'stopped':
                                self.incus.request('PUT', '/1.0/instances/' + quote(spec.name) + '/state', {'action': 'stop', 'force': True, 'timeout': 30})
                            self.incus.request('DELETE', '/1.0/instances/' + quote(spec.name))
                    except HypervisorNotFound:
                        pass
                    self.update(identity, 'deleted')
                    return 'Deleted owned K3s node'
                except Exception as exc:
                    self.update(identity, 'failed', str(exc)[:1000]); raise
            try:
                result = self.submit(user, 'devops.node.delete', spec.name, work, resources={'incus:' + spec.name})
                with self.store.db() as db:
                    db.execute('UPDATE devops_nodes SET task_id=? WHERE id=?', (result['task_id'], identity))
                return result
            except Exception:
                self.update(identity, 'failed', 'Could not queue node deletion')
                raise


class Manager:
    def __init__(self, store, agent, cluster, submit):
        self.store, self.agent, self.cluster, self.submit = store, agent, cluster, submit
        self.lock = threading.RLock()

    def rpc(self, host_id, path, user, body=None):
        if not host_id:
            if path == 'options': return self.agent.options()
            if path == 'validate': return self.agent.validate(NodeRequest.model_validate(body))
            if path == 'reserve': return self.agent.reserve(NodeRequest.model_validate(body))
            identity, _, action = path.partition('/')
            if action == 'prepare': return self.agent.prepare(identity, user, Install.model_validate(body))
            if action == 'delete': return self.agent.delete(identity, user)
            if action in ('token', 'status', 'kubeconfig'): return self.agent.guest(identity, action)
            if action == 'record': return self.agent.record(identity)
            if action == 'manage': return self.agent.manage(identity, user, kubernetes.Operation.model_validate(body))
            raise HypervisorError('Unknown DevOps request')
        host = self.cluster.host(host_id)
        try:
            with httpx.Client(verify=self.cluster.tls(host), trust_env=False, timeout=110 if path.endswith('/manage') else 30) as client:
                response = client.request('GET' if body is None else 'POST', host['url'] + '/api/devops/agent/' + path,
                                          headers=self.cluster.headers(host, user), json=body)
                if response.status_code == 404:
                    raise HTTPException(404, 'Linked host deployment record not found')
                if response.is_error:
                    raise HypervisorError('Linked host rejected DevOps request: ' + str(response.json().get('detail', response.status_code))[:500])
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HypervisorError('Linked host unavailable; inspect its node task before retrying') from exc

    def options(self, user):
        hosts = [{'id': '', 'name': self.cluster.identity()['name']}] + self.cluster.hosts()
        for host in hosts:
            try: host['options'] = self.rpc(host['id'], 'options', user)
            except (HypervisorError, HTTPException): host['error'] = 'Host unavailable or DevOps update required'
        return {'hosts': hosts, 'version': VERSION}

    def update(self, identity, status, message, nodes=None):
        with self.store.db() as db:
            db.execute('UPDATE devops_clusters SET status=?,message=? WHERE id=?', (status, message, identity))
            if nodes is not None:
                db.execute('UPDATE devops_clusters SET nodes=? WHERE id=?', (json.dumps(nodes), identity))
        emit(message + '\n')

    def create(self, spec, user):
        identity = uuid.uuid4().hex
        nodes = []
        with self.lock:
            for role, specs in (('master', spec.masters), ('worker', spec.workers)):
                for index, hardware in enumerate(specs, 1):
                    node = NodeRequest(**hardware.model_dump(), id=uuid.uuid4().hex, cluster_id=identity,
                        name=f'{spec.name}-{role}-{index}', role=role, new_network=spec.new_network,
                        pod_cidr=spec.pod_cidr, service_cidr=spec.service_cidr)
                    if spec.new_network: node.network = spec.new_network.name
                    self.rpc(node.host_id, 'validate', user, node.model_dump())
                    host_name = self.cluster.identity()['name'] if not node.host_id else self.cluster.host(node.host_id)['name']
                    nodes.append(dict(node.model_dump(), status='pending', address='', host_name=host_name))
            try:
                with self.store.db() as db:
                    db.execute("INSERT INTO devops_clusters(id,name,spec,nodes,status,message,created) VALUES(?,?,?,?,'queued','Deployment queued',?)",
                               (identity, spec.name, spec.model_dump_json(), json.dumps(nodes), time.time()))
            except sqlite3.IntegrityError:
                raise HypervisorError('Cluster name is already in use')
            return self.queue(identity, user)

    def queue(self, identity, user, delete=False):
        record = row(self.store, 'devops_clusters', identity)
        nodes = record['nodes']
        def run():
            self.update(identity, 'deleting' if delete else 'deploying', 'Deleting cluster VMs' if delete else 'Provisioning K3s cluster')
            try:
                if delete:
                    for node in reversed(nodes):
                        if node['status'] == 'deleted': continue
                        try:
                            self.rpc(node['host_id'], node['id'] + '/record', user)
                        except HTTPException as exc:
                            if exc.status_code != 404:
                                raise
                            node['status'] = 'deleted'
                            self.update(identity, 'deleting', 'No VM was provisioned for ' + node['name'], nodes)
                            continue
                        self.rpc(node['host_id'], node['id'] + '/delete', user, {})
                        self.wait_node(node, user, deleted=True)
                        self.update(identity, 'deleting', 'Deleted ' + node['name'], nodes)
                    self.update(identity, 'deleted', 'Cluster VMs deleted; networks retained for inspection or reuse', nodes)
                    return 'Cluster VMs deleted; networks retained'
                first = nodes[0]
                token = '0' * 64  # First master generates/recovers its token inside the VM.
                for index, node in enumerate(nodes):
                    self.update(identity, 'deploying', 'Preparing ' + node['name'], nodes)
                    self.rpc(node['host_id'], 'reserve', user, NodeRequest.model_validate(node).model_dump())
                    self.rpc(node['host_id'], node['id'] + '/prepare', user, {'token': token, 'server': first['address'] if index else ''})
                    self.wait_node(node, user)
                    if index == 0:
                        token = self.rpc(node['host_id'], node['id'] + '/token', user)['token']
                    self.update(identity, 'deploying', 'Installed ' + node['name'], nodes)
                expected = {n['name'] for n in nodes}
                def healthy():
                    result = self.rpc(first['host_id'], first['id'] + '/status', user)
                    ready = {n['metadata']['name'] for n in result.get('nodes', [])
                        if any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in n.get('status', {}).get('conditions', []))}
                    for node in nodes: node['status'] = 'ready' if node['name'] in ready else 'waiting'
                    self.update(identity, 'deploying', f'{len(expected & ready)}/{len(expected)} Kubernetes nodes Ready', nodes)
                    return expected <= ready
                wait_for(healthy, 'Not every requested Kubernetes node became Ready; check cross-host routing and node services', interval=10)
                self.update(identity, 'ready', 'All requested Kubernetes nodes are Ready', nodes)
                return 'K3s cluster ready'
            except Exception as exc:
                self.update(identity, 'failed', str(exc)[:1000], nodes)
                raise
        try:
            result = self.submit(user, 'devops.cluster.delete' if delete else 'devops.cluster.deploy', record['name'], run,
                                 resources={'devops:' + identity})
            with self.store.db() as db:
                db.execute('UPDATE devops_clusters SET task_id=? WHERE id=?', (result['task_id'], identity))
            return dict(result, cluster_id=identity)
        except Exception:
            self.update(identity, 'failed', 'Could not queue cluster operation', nodes)
            raise

    def wait_node(self, node, user, deleted=False):
        def poll():
            record = self.rpc(node['host_id'], node['id'] + '/record', user)
            node['status'] = record['status']
            if record['status'] in ('failed', 'interrupted'):
                raise HypervisorError(node['name'] + ': ' + record['message'])
            if record['status'] == ('deleted' if deleted else 'installed'):
                node['address'] = record['message'] if not deleted else node.get('address', '')
                return True
            return False
        wait_for(poll, 'Node operation timed out; inspect its host task before retrying', timeout=2400)

    def retry(self, identity, user, delete=False):
        with self.lock:
            record = row(self.store, 'devops_clusters', identity)
            if record['status'] in ACTIVE or record['status'] == 'deleted':
                raise HypervisorError('Cluster operation is active or cluster has been deleted')
            if not delete and record['status'] == 'ready':
                raise HypervisorError('Cluster deployment is already complete')
            # Never rebuild a missing first master after other nodes joined.
            if not delete and len(record['nodes']) > 1 and record['nodes'][0].get('address'):
                first = record['nodes'][0]
                self.rpc(first['host_id'], first['id'] + '/token', user)
            self.update(identity, 'queued', 'Cluster operation queued')
            return self.queue(identity, user, delete)
