"""Validated workload authoring and native edge-controller requests."""
import ipaddress
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DNS = r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$'
CLASS = 'vmm.carrtech.dev/edge'
SYSTEM_NAMESPACES = ('kube-system', 'kube-public', 'kube-node-lease')
MANAGED = 'app.kubernetes.io/managed-by'


class Workload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    image: str = Field(default='', max_length=512, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._/@:-]*$|^$')
    replicas: int = Field(default=1, ge=0, le=100)
    port: int = Field(default=80, ge=1, le=65535)
    target_port: int = Field(default=80, ge=1, le=65535)
    selector: str = Field(default='', max_length=63, pattern=r'^$|' + DNS)
    service_type: Literal['ClusterIP', 'NodePort', 'LoadBalancer'] = 'ClusterIP'
    hostname: str = Field(default='', max_length=253, pattern=r'^$|^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$')
    path: str = Field(default='/', min_length=1, max_length=512, pattern=r'^/[^\s{}]*$')
    ingress_class: str = Field(default='traefik', max_length=63, pattern=DNS)
    tls_secret: str = Field(default='', max_length=63, pattern=r'^$|' + DNS)
    cpu_millis: int = Field(default=100, ge=10, le=64000)
    memory_mib: int = Field(default=128, ge=16, le=131072)

    @field_validator('hostname')
    @classmethod
    def hostname_valid(cls, value):
        if value and ('.' not in value or any(not p or len(p) > 63 or p.startswith('-') or p.endswith('-') for p in value.split('.'))):
            raise ValueError('Enter a fully qualified hostname')
        return value


class ServiceStatus(BaseModel):
    name: str = Field(max_length=63, pattern=DNS)
    namespace: str = Field(max_length=63, pattern=DNS)
    uid: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9-]+$')
    address: str = ''

    @field_validator('address')
    @classmethod
    def valid_address(cls, value):
        return str(ipaddress.IPv4Address(value)) if value else ''


def manifest(resource, name, namespace, spec):
    metadata = {'name': name, 'namespace': namespace, 'labels': {MANAGED: 'vm-manager'}}
    if resource == 'deployment':
        if not spec.image:
            raise ValueError('Container image is required')
        return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata, 'spec': {
            'replicas': spec.replicas, 'selector': {'matchLabels': {'app': name}},
            'template': {'metadata': {'labels': {'app': name}}, 'spec': {'containers': [{
                'name': name, 'image': spec.image, 'ports': [{'containerPort': spec.port}],
                'resources': {'requests': {'cpu': str(spec.cpu_millis) + 'm', 'memory': str(spec.memory_mib) + 'Mi'},
                              'limits': {'memory': str(spec.memory_mib * 2) + 'Mi'}}}]}}}}
    if resource == 'service':
        if not spec.selector:
            raise ValueError('Deployment / app selector is required')
        service = {'type': spec.service_type, 'selector': {'app': spec.selector},
                   'ports': [{'name': 'tcp', 'port': spec.port, 'targetPort': spec.target_port, 'protocol': 'TCP'}]}
        return {'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata, 'spec': service}
    if not spec.hostname or not spec.selector:
        raise ValueError('Hostname and backend Service are required')
    ingress = {'ingressClassName': spec.ingress_class, 'rules': [{'host': spec.hostname, 'http': {'paths': [{
        'path': spec.path, 'pathType': 'Prefix', 'backend': {'service': {'name': spec.selector, 'port': {'number': spec.port}}}}]}}]}
    if spec.tls_secret:
        ingress['tls'] = [{'hosts': [spec.hostname], 'secretName': spec.tls_secret}]
    return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'Ingress', 'metadata': metadata, 'spec': ingress}


# Passed in the validated Operation, generated server-side; never arbitrary YAML.
GUEST = r'''
def workload_inventory():
    items = json.loads(kube(['get', 'deployments,services,ingresses,namespaces,mutatingwebhookconfigurations', '--all-namespaces', '-o', 'json']))['items']
    out = {'deployments': [], 'services': [], 'ingresses': [], 'namespaces': [], 'admission_version': ''}
    for obj in items:
        m, s, status = obj['metadata'], obj.get('spec', {}), obj.get('status', {})
        base = {k: m.get(k, '') for k in ('name', 'namespace', 'uid')}
        base['managed'] = m.get('labels', {}).get('app.kubernetes.io/managed-by') == 'vm-manager'
        kind = obj['kind']
        if kind == 'MutatingWebhookConfiguration' and m['name'] == 'vmm-edge-admission':
            out['admission_version'] = m.get('annotations', {}).get('vmm.carrtech.dev/admission-version', '')
        elif kind == 'Namespace':
            out['namespaces'].append(m['name'])
        elif kind == 'Deployment':
            containers = s.get('template', {}).get('spec', {}).get('containers', [])
            base.update(replicas=s.get('replicas', 1), ready=status.get('readyReplicas', 0),
                images=[c['image'] for c in containers], available=status.get('availableReplicas', 0))
            out['deployments'].append(base)
        elif kind == 'Service':
            base.update(type=s.get('type', 'ClusterIP'), cluster_ip=s.get('clusterIP', ''),
                ports=s.get('ports', []), selector=s.get('selector', {}), lb_class=s.get('loadBalancerClass', ''),
                external=status.get('loadBalancer', {}).get('ingress', []),
                external_traffic_policy=s.get('externalTrafficPolicy', 'Cluster'),
                deleting=bool(m.get('deletionTimestamp')),
                cleanup_pending='service.kubernetes.io/load-balancer-cleanup' in m.get('finalizers', []),
                automatic_lb=m.get('annotations', {}).get('vmm.carrtech.dev/load-balancer') != 'disabled')
            out['services'].append(base)
        elif kind == 'Ingress':
            base.update(rules=s.get('rules', []), tls=s.get('tls', []), ingress_class=s.get('ingressClassName', ''))
            out['ingresses'].append(base)
    return out

def workload_mutation():
    resource = r['resource']
    if r['action'] == 'create':
        kube(['create', '-f', '-'], json.dumps(r['manifest']))
        return {'message': resource.title() + ' created. Check readiness in the resource table.'}
    obj = get(resource, r['name'], r['namespace'])
    if obj['metadata']['uid'] != r['uid']:
        raise ValueError('Resource changed since selection. Refresh and try again.')
    if obj['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') != 'vm-manager':
        raise ValueError('Only resources created through VM Manager can be edited or removed here; use kubectl for other resources.')
    paths = {'deployment': '/apis/apps/v1', 'service': '/api/v1', 'ingress': '/apis/networking.k8s.io/v1'}
    plural = {'deployment': 'deployments', 'service': 'services', 'ingress': 'ingresses'}
    url = paths[resource] + '/namespaces/' + r['namespace'] + '/' + plural[resource] + '/' + r['name']
    if r['action'] == 'remove':
        kube(['delete', '--raw', url, '-f', '-'], json.dumps({'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':r['uid']}}))
        return {'message': 'Deletion requested. Related Services, Ingresses and storage are retained.'}
    if resource != 'deployment':
        raise ValueError('Only Deployment image and replica updates are supported')
    # JSON Patch preconditions prevent an update racing with replacement/deletion.
    patch = [{'op':'test','path':'/metadata/uid','value':r['uid']},
             {'op':'test','path':'/metadata/resourceVersion','value':obj['metadata']['resourceVersion']},
             {'op':'replace','path':'/spec/replicas','value':r['workload']['replicas']},
             {'op':'replace','path':'/spec/template/spec/containers/0/image','value':r['workload']['image']}]
    kube(['patch', resource, r['name'], '--namespace', r['namespace'], '--type=json', '-p', json.dumps(patch)])
    return {'message':'Deployment update requested. Kubernetes manages the rollout.'}

def edge_status():
    for entry in r['statuses']:
        obj = get('service', entry['name'], entry['namespace'])
        if obj['metadata']['uid'] != entry['uid'] or obj.get('spec', {}).get('loadBalancerClass') != 'vmm.carrtech.dev/edge':
            raise ValueError('Service changed or uses another load-balancer controller')
        if 'service.kubernetes.io/load-balancer-cleanup' in obj['metadata'].get('finalizers', []):
            raise ValueError('Waiting for the previous load balancer controller to finish cleanup')
        status = {'loadBalancer': {'ingress': [{'ip': entry['address']} ] if entry['address'] else []}}
        patch = [{'op':'test', 'path':'/metadata/uid', 'value':entry['uid']},
                 {'op':'add', 'path':'/status/loadBalancer', 'value':status['loadBalancer']}]
        kube(['patch', 'service', entry['name'], '-n', entry['namespace'], '--subresource=status', '--type=json', '-p', json.dumps(patch)])
    return {'message': 'Service addresses reconciled'}
'''
