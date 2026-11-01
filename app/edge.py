"""Persistent edge management and a controller for native Kubernetes Services."""
import ipaddress
import json
import os
import threading
import time
import uuid
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from . import devops, edge_runtime, features, kubernetes, storage
from .hypervisor import HypervisorError
from .kube_workloads import CLASS, DNS, SYSTEM_NAMESPACES

CONTROLLER = {'username': 'load-balancer-controller', 'role': 'administrator'}


class Route(edge_runtime.Route):
    id: str = Field(default='', pattern=r'^$|^[a-z0-9-]{1,64}$')
    upstreams: list[str] = Field(default_factory=list, max_length=32)
    cluster_id: str = Field(default='', pattern=r'^$|^[0-9a-f]{32}$')
    namespace: str = Field(default='default', max_length=63, pattern=DNS)
    service: str = Field(default='', max_length=63, pattern=r'^$|' + DNS)
    service_port: int = Field(default=80, ge=1, le=65535)

    @model_validator(mode='after')
    def valid_target(self):
        if bool(self.cluster_id) != bool(self.service):
            raise ValueError('Choose both a cluster and Service')
        if self.backend == 'vm-manager' and (self.cluster_id or self.upstreams):
            raise ValueError('VM Manager uses its protected built-in upstream')
        if self.backend != 'vm-manager' and not self.cluster_id and not self.upstreams:
            raise ValueError('Supply upstream IPv4:port targets or a Kubernetes Service')
        if self.mode == 'manager' or self.id.startswith('manager'):
            raise ValueError('The built-in VM Manager route is protected')
        if self.mode == 'letsencrypt' and (not self.email or self.hostname.replace('.', '').isdigit()):
            raise ValueError('Let’s Encrypt requires a public DNS hostname and contact email')
        return self


class Cloudflare(BaseModel):
    token: str = Field(min_length=20, max_length=512, pattern=r'^[A-Za-z0-9_-]+$', repr=False)
    account_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    zone_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class Binding(BaseModel):
    enabled: bool = True


def initialize(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS devops_edge(id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL, status TEXT NOT NULL)')
        db.execute("INSERT OR IGNORE INTO devops_edge VALUES(1,?,?)", (json.dumps({'enabled': False, 'routes': [], 'clusters': [], 'cloudflare': {}, 'excluded_clusters': []}), json.dumps({'state': 'not-deployed', 'message': 'Load balancer has not been launched', 'services': []})))


class Manager:
    def __init__(self, store, manager, submit):
        self.store, self.manager, self.submit = store, manager, submit
        self.lock = threading.RLock()
        self.last_tick = 0
        self.applied_hash = ''

    def read(self):
        with self.store.db() as db:
            row = db.execute('SELECT settings,status FROM devops_edge WHERE id=1').fetchone()
        return json.loads(row[0]), json.loads(row[1])

    def save(self, settings, status):
        with self.store.db() as db:
            db.execute('UPDATE devops_edge SET settings=?,status=? WHERE id=1', (json.dumps(settings), json.dumps(status)))

    def catalog(self):
        settings, status = self.read()
        public = {k: settings.get(k) for k in ('enabled', 'address', 'clusters', 'excluded_clusters')}
        public['automatic'] = True
        public['routes'] = [{k: v for k, v in r.items() if k not in ('certificate', 'private_key')} | {'has_certificate': bool(r.get('certificate'))} for r in settings['routes']]
        cf = settings.get('cloudflare', {})
        public['cloudflare'] = {k: cf.get(k, '') for k in ('account_id', 'zone_id', 'tunnel_id', 'zone_name', 'local')}
        public['cloudflare']['configured'] = bool(cf.get('token'))
        return {'settings': public, 'status': status, 'load_balancer_class': CLASS}

    def queue(self, user, change):
        with self.lock:
            settings, status = self.read()
            change(settings)
            status.update(state='pending', message='Configuration queued')
            self.save(settings, status)
            self.store.audit(user['username'], 'devops.edge.configure', 'load-balancer')
        def work():
            with self.lock:
                self.reconcile(force=True)
            return 'Load balancer configuration applied; inspect certificate and Kubernetes readiness separately'
        return self.submit(user, 'devops.edge.configure', 'load-balancer', work, resources={'devops-edge'})

    def launch(self, user):
        def change(settings):
            settings['enabled'] = True
            settings.setdefault('address', str(ipaddress.IPv4Address(os.environ.get('VM_MANAGER_HOST', '192.168.8.235'))))
        return self.queue(user, change)

    def route(self, user, route):
        def change(settings):
            if not settings['enabled']:
                raise HTTPException(409, 'Launch the load balancer first')
            if any(r['hostname'] == route.hostname and r['id'] != route.id for r in settings['routes']):
                raise HTTPException(409, 'Hostname already has a route')
            if route.id and not any(r['id'] == route.id for r in settings['routes']):
                raise HTTPException(404, 'Route not found')
            body = route.model_dump()
            body['id'] = route.id or uuid.uuid4().hex
            existing = next((r for r in settings['routes'] if r['id'] == body['id']), {})
            for key in ('certificate', 'private_key'):
                if not body[key]: body[key] = existing.get(key, '')
            if body['mode'] == 'manual' and (not body['certificate'] or not body['private_key']):
                raise HTTPException(400, 'Supply the PEM certificate chain and private key')
            settings['routes'] = [r for r in settings['routes'] if r['id'] != body['id']] + [body]
        return self.queue(user, change)

    def remove(self, user, identity, confirm):
        def change(settings):
            target = next((r for r in settings['routes'] if r['id'] == identity), None)
            if not target: raise HTTPException(404, 'Route not found')
            if identity.startswith('manager'): raise HTTPException(409, 'VM Manager recovery routes are protected')
            if confirm != target['hostname']: raise HTTPException(400, 'Type the hostname to confirm')
            settings['routes'] = [r for r in settings['routes'] if r['id'] != identity]
        return self.queue(user, change)

    def bind(self, user, identity, enabled):
        def change(settings):
            if devops.row(self.store, 'devops_clusters', identity)['status'] != 'ready':
                raise HTTPException(409, 'Cluster must be Ready')
            ids = set(settings['clusters'])
            withdrawals = set(settings.get('withdrawals', []))
            excluded = set(settings.get('excluded_clusters', []))
            if enabled:
                excluded.discard(identity)
                ids.add(identity)
                withdrawals.discard(identity)
            else:
                excluded.add(identity)
                ids.discard(identity)
                withdrawals.add(identity)
            settings['excluded_clusters'] = sorted(excluded)
            settings['withdrawals'] = sorted(withdrawals)
            settings['clusters'] = sorted(ids)
        return self.queue(user, change)

    def cloudflare(self, user, body):
        def change(settings):
            cf = settings.get('cloudflare', {})
            if cf.get('account_id') and cf['account_id'] != body.account_id:
                raise HTTPException(409, 'Existing tunnel belongs to a different account')
            if cf.get('zone_id') and cf['zone_id'] != body.zone_id and cf.get('records'):
                raise HTTPException(409, 'Remove existing Cloudflare routes before changing zones')
            cf.update(body.model_dump())
            settings['cloudflare'] = cf
        return self.queue(user, change)

    @staticmethod
    def cf_request(cf, method, path, body=None):
        try:
            with httpx.Client(trust_env=False, timeout=15) as client:
                response = client.request(method, 'https://api.cloudflare.com/client/v4/' + path,
                    headers={'Authorization': 'Bearer ' + cf['token']}, json=body)
            data = response.json()
            if response.is_error or not data.get('success'):
                raise HypervisorError('Cloudflare rejected the request. Check the token’s Tunnel Edit, Zone Read and DNS Edit permissions and account/zone IDs.')
            return data['result']
        except (httpx.HTTPError, ValueError) as exc:
            raise HypervisorError('Cloudflare API is unavailable; credentials were retained for retry') from exc

    def sync_cloudflare(self, settings, status):
        cf = settings['cloudflare']
        hosts = sorted({r['hostname'] for r in settings['routes'] if r['mode'] == 'cloudflare'})
        if not cf.get('token'):
            if any(not r['id'].startswith('manager') for r in settings['routes'] if r['mode'] == 'cloudflare'):
                raise HypervisorError('Configure a Cloudflare API token and zone before publishing a tunnel hostname')
            return
        zone = self.cf_request(cf, 'GET', 'zones/' + cf['zone_id'])
        if zone['account']['id'] != cf['account_id']:
            raise HypervisorError('Cloudflare zone belongs to another account')
        cf['zone_name'] = zone['name']
        for host in hosts:
            if host != zone['name'] and not host.endswith('.' + zone['name']):
                raise HypervisorError('Tunnel hostname must belong to the configured Cloudflare zone')
        if not cf.get('tunnel_id'):
            created = self.cf_request(cf, 'POST', 'accounts/' + cf['account_id'] + '/cfd_tunnel',
                {'name': 'vm-manager-edge-' + uuid.uuid4().hex[:10], 'config_src': 'cloudflare'})
            cf.update(tunnel_id=created['id'], tunnel_token=created['token'], local=False)
            self.save(settings, status)  # Persist ownership immediately, including after partial failure.
        tunnel_path = 'accounts/' + cf['account_id'] + '/cfd_tunnel/' + cf['tunnel_id']
        if not cf.get('local'):
            # One transport origin. Application host routing lives exclusively in Caddy.
            self.cf_request(cf, 'PUT', tunnel_path + '/configurations',
                {'config': {'ingress': [{'service': 'http://127.0.0.1:18080'}]}})
        records = cf.setdefault('records', {})
        for host in hosts:
            found = self.cf_request(cf, 'GET', 'zones/' + cf['zone_id'] + '/dns_records?name=' + host)
            if found:
                if len(found) != 1 or found[0].get('type') != 'CNAME' or found[0].get('content') != cf['tunnel_id'] + '.cfargotunnel.com' or not found[0].get('proxied'):
                    raise HypervisorError('DNS record conflict for ' + host + '; existing record was retained')
                # Existing records are usable but never adopted for deletion.
            else:
                record = self.cf_request(cf, 'POST', 'zones/' + cf['zone_id'] + '/dns_records',
                    {'type': 'CNAME', 'name': host, 'content': cf['tunnel_id'] + '.cfargotunnel.com', 'proxied': True})
                records[host] = record['id']
                self.save(settings, status)
        for host in list(records):
            if host in hosts: continue
            record = self.cf_request(cf, 'GET', 'zones/' + cf['zone_id'] + '/dns_records/' + records[host])
            if record.get('name') == host and record.get('content') == cf['tunnel_id'] + '.cfargotunnel.com':
                self.cf_request(cf, 'DELETE', 'zones/' + cf['zone_id'] + '/dns_records/' + records[host])
            del records[host]
            self.save(settings, status)
        remote = self.cf_request(cf, 'GET', tunnel_path)
        status['tunnel_status'] = remote.get('status', 'unknown')

    def inventory(self, identity):
        return kubernetes.manage(self.manager, self.store, identity, CONTROLLER,
                                 kubernetes.Operation(action='edge-inventory'))

    @staticmethod
    def automatic_service(service):
        return (service.get('type') == 'LoadBalancer' and not service.get('lb_class')
                and service.get('namespace') not in SYSTEM_NAMESPACES
                and service.get('automatic_lb', True) and not service.get('deleting'))

    def reconcile(self, force=False):
        settings, status = self.read()
        if not features.catalog()['devops']['enabled']:
            return
        try:
            # Discover existing and newly ready clusters on every pass. An explicit
            # opt-out survives polling and manager restarts.
            with self.store.db() as db:
                records = {r['id']: r['status'] for r in db.execute('SELECT id,status FROM devops_clusters')}
            excluded = set(settings.get('excluded_clusters', []))
            connected = set(settings['clusters']) | {i for i, state in records.items() if state == 'ready'}
            settings['clusters'] = sorted(connected - excluded)
            inventories, errors = {}, []
            ids = set(settings['clusters']) | set(settings.get('withdrawals', [])) | {r['cluster_id'] for r in settings['routes'] if r.get('cluster_id')}
            for identity in sorted(ids):
                try:
                    record = devops.row(self.store, 'devops_clusters', identity)
                    if record['status'] == 'deleted':
                        inventories[identity] = {'services': [], 'addresses': []}
                        continue
                    inventory = self.inventory(identity)
                    inventory['addresses'] = [n['address'] for n in record['nodes'] if n.get('address')]
                    inventories[identity] = inventory
                except Exception:
                    errors.append('Cluster ' + identity[:8] + ' is unavailable; its last routes are retained')
            demand = any(s.get('type') == 'LoadBalancer' and not s.get('deleting')
                         and (s.get('lb_class') == CLASS or self.automatic_service(s))
                         for identity in settings['clusters'] for s in inventories.get(identity, {}).get('services', []))
            if not settings['enabled'] and demand:
                settings['enabled'] = True
                settings.setdefault('address', str(ipaddress.IPv4Address(os.environ.get('VM_MANAGER_HOST', '192.168.8.235'))))
                self.store.audit(CONTROLLER['username'], 'devops.edge.automatic-launch', 'load-balancer')
            self.save(settings, status)
            if not settings['enabled']:
                status.update(state='standby', message='Automatic integration is enabled; the load balancer starts when an application Service needs it')
                self.save(settings, status)
                return
            if not settings.get('initialized'):
                info = storage.broker('edge_info')['existing_tunnel']
                address = settings['address']
                settings['routes'].insert(0, dict(id='manager-lan', hostname=address, mode='manager', upstreams=[address + ':8443']))
                for i, hostname in enumerate(info.get('hostnames', [])):
                    settings['routes'].append(dict(id='manager-tunnel-' + str(i), hostname=hostname, mode='cloudflare', upstreams=[address + ':8443']))
                if info:
                    settings['cloudflare'].update(tunnel_id=info['id'], account_id=info['account_id'], local=True)
                settings['initialized'] = True
                self.save(settings, status)
            snapshots = settings.setdefault('snapshots', {})
            for identity, data in inventories.items(): snapshots[identity] = data
            snapshots = {key: val for key, val in snapshots.items() if key in ids}
            settings['snapshots'] = snapshots
            routes = []
            for route in settings['routes']:
                body = {k: v for k, v in route.items() if k in edge_runtime.Route.model_fields}
                if route.get('backend') == 'vm-manager':
                    body['upstreams'] = [settings['address'] + ':8443']
                if route.get('cluster_id'):
                    inv = snapshots.get(route['cluster_id'], {})
                    service = next((s for s in inv.get('services', []) if s['namespace'] == route['namespace'] and s['name'] == route['service']), None)
                    port = next((p for p in (service or {}).get('ports', []) if p['port'] == route['service_port'] and p.get('protocol', 'TCP') == 'TCP'), {})
                    if not port.get('nodePort'):
                        errors.append('Route ' + route['hostname'] + ' needs a reachable NodePort or LoadBalancer Service')
                        continue
                    body['upstreams'] = [ip + ':' + str(port['nodePort']) for ip in inv.get('addresses', [])]
                    if not body['upstreams']: continue
                routes.append(body)
            listeners, services, allocations = [], [], {}
            used = {80, 443, 8443, 18080, 20191}
            # Retain existing port ownership when new Services/clusters appear.
            live = {(identity, s['uid']): s for identity, inv in snapshots.items() for s in inv.get('services', [])
                    if s.get('type') == 'LoadBalancer' and s.get('lb_class') == CLASS and not s.get('deleting')}
            owners = {port: (s['cluster_id'], s['uid']) for s in status.get('services', []) if s.get('state') == 'configured'
                      and (s['cluster_id'], s['uid']) in live
                      for port in s['ports'] if any(p['port'] == port for p in live[(s['cluster_id'], s['uid'])]['ports'])}
            for identity in sorted(settings['clusters']):
                inv = snapshots.get(identity, {})
                for service in sorted(inv.get('services', []), key=lambda s: (s['namespace'], s['name'])):
                    if service['type'] != 'LoadBalancer' or service.get('lb_class') != CLASS or service.get('deleting'): continue
                    ports = service['ports']
                    message = ''
                    if service.get('cleanup_pending'):
                        message = 'Waiting for the previous load balancer controller to finish cleanup'
                    elif service.get('external_traffic_policy') != 'Cluster':
                        message = 'externalTrafficPolicy must be Cluster'
                    elif not ports or any(p.get('protocol', 'TCP') != 'TCP' or not p.get('nodePort') or p['port'] < 1024 for p in ports):
                        message = 'TCP NodePorts and external ports 1024–65535 are required; publish HTTP/HTTPS using a hostname route'
                    elif any(p['port'] in used or (p['port'] in owners and owners[p['port']] != (identity, service['uid'])) for p in ports):
                        message = 'Listener port is reserved or allocated to another Service'
                    elif not inv.get('addresses'):
                        message = 'No node address is available'
                    entry = {'cluster_id': identity, 'name': service['name'], 'namespace': service['namespace'], 'uid': service['uid'],
                             'ports': [p['port'] for p in ports], 'state': 'pending' if message else 'configured', 'message': message,
                             'address': '' if message else settings['address']}
                    services.append(entry)
                    if not service.get('cleanup_pending'):
                        allocations.setdefault(identity, []).append({k: entry[k] for k in ('name', 'namespace', 'uid', 'address')})
                    if message:
                        errors.append(service['namespace'] + '/' + service['name'] + ': ' + message)
                        continue
                    for p in ports:
                        used.add(p['port'])
                        listeners.append({'id': identity[:8] + '-' + service['uid'], 'port': p['port'],
                                          'upstreams': [ip + ':' + str(p['nodePort']) for ip in inv['addresses']]})
            # Withdraw status when a cluster is disconnected from the controller.
            for identity in settings.get('withdrawals', []):
                for service in inventories.get(identity, {}).get('services', []):
                    if service.get('lb_class') == CLASS:
                        allocations.setdefault(identity, []).append({k: service[k] for k in ('name', 'namespace', 'uid')} | {'address': ''})
            cf_fingerprint = json.dumps([r['hostname'] for r in routes if r['mode'] == 'cloudflare']) + str(settings['cloudflare'].get('zone_id'))
            if force or settings.get('cf_fingerprint') != cf_fingerprint:
                self.sync_cloudflare(settings, status)
                settings['cf_fingerprint'] = cf_fingerprint
            config = dict(address=settings['address'], routes=routes, listeners=listeners,
                          tunnel_token=settings['cloudflare'].get('tunnel_token', ''), migrate_tunnel=bool(settings['cloudflare'].get('local')))
            import hashlib
            digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
            health = storage.broker('edge_status') if digest == self.applied_hash else {}
            if force or digest != self.applied_hash or not health.get('running') or (listeners and not health.get('components', {}).get('tcp')):
                runtime = storage.broker('edge_apply', body=config)
                if not runtime['running'] or (listeners and not runtime['components']['tcp']):
                    raise HypervisorError('Load balancer containers did not become ready')
                self.applied_hash = digest
            # A classless application Service is adopted with UID and version
            # preconditions. K3s cleans up its former listener after the class
            # changes; status writes wait for that controller's finalizer.
            for identity in settings['clusters']:
                for service in inventories.get(identity, {}).get('services', []):
                    if not self.automatic_service(service):
                        continue
                    try:
                        result = kubernetes.manage(self.manager, self.store, identity, CONTROLLER,
                            kubernetes.Operation(action='edge-claim', resource='service', statuses=[{k: service[k] for k in ('name', 'namespace', 'uid')}]))
                        claimed = next(c for c in result['claimed'] if c['uid'] == service['uid'])
                        service.update(lb_class=CLASS, cleanup_pending=claimed['cleanup_pending'])
                        services.append({'cluster_id':identity, 'name':service['name'], 'namespace':service['namespace'],
                            'uid':service['uid'], 'ports':[p['port'] for p in service['ports']], 'state':'pending', 'address':'',
                            'message':'Automatic setup is in progress; waiting for controller handover and listener provisioning'})
                        self.store.audit(CONTROLLER['username'], 'devops.service.automatic-load-balancer', identity, service['namespace'] + '/' + service['name'])
                    except Exception:
                        errors.append('Could not claim Service ' + service['namespace'] + '/' + service['name'] + '; retrying automatically')
            status['runtime'] = runtime if 'runtime' in locals() else health
            # Publish Kubernetes status only after the data plane accepted the configuration.
            for identity, entries in allocations.items():
                if identity not in inventories: continue
                previous = {s['uid']: [{k: v for k, v in a.items() if k in ('ip', 'hostname')} for a in s.get('external', [])] for s in inventories[identity]['services']}
                changes = [s for s in entries if previous.get(s['uid']) != ([{'ip': s['address']}] if s['address'] else [])]
                if changes:
                    try:
                        kubernetes.manage(self.manager, self.store, identity, CONTROLLER,
                            kubernetes.Operation(action='edge-status', statuses=changes))
                    except Exception:
                        errors.append('Could not update Kubernetes Service addresses for ' + identity[:8])
                        continue
                if identity in settings.get('withdrawals', []):
                    settings['withdrawals'].remove(identity)
            provisioning = any(s['state'] == 'pending' for s in services)
            status.update(state='degraded' if errors else 'provisioning' if provisioning else 'ready',
                message='; '.join(errors) if errors else 'Load balancer is running; new Service setup is in progress' if provisioning else 'Load balancer is running',
                services=services, updated=time.time())
            self.save(settings, status)
        except Exception as exc:
            status.update(state='failed', message=str(exc)[:500], updated=time.time())
            self.save(settings, status)
            raise

    def tick(self):
        if not self.lock.acquire(blocking=False): return
        try:
            if time.monotonic() - self.last_tick >= 15:
                self.last_tick = time.monotonic()
                self.reconcile()
        finally:
            self.lock.release()
