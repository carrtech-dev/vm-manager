"""Narrow Kubernetes operations, routed through an owned K3s master."""
from typing import Literal

from . import kube_workloads

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

NAME = r'^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$'


class Operation(BaseModel):
    action: Literal['inventory', 'describe', 'delete', 'restart', 'exec-check', 'create', 'update', 'remove', 'edge-inventory', 'edge-status'] = 'inventory'
    resource: Literal['node', 'pod', 'deployment', 'service', 'ingress'] = 'pod'
    namespace: str = Field(default='default', min_length=1, max_length=63, pattern=r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$')
    name: str = Field(default='', max_length=253, pattern=r'^$|' + NAME)
    uid: str = Field(default='', max_length=128, pattern=r'^[a-zA-Z0-9-]*$')
    container: str = Field(default='', max_length=63, pattern=r'^$|' + NAME)
    shell: Literal['/bin/sh', '/bin/bash'] = '/bin/sh'
    confirm_name: str = ''
    workload: kube_workloads.Workload | None = None
    statuses: list[kube_workloads.ServiceStatus] = Field(default_factory=list, max_length=200)

    @model_validator(mode='after')
    def valid_target(self):
        if self.action in ('create', 'update', 'remove'):
            if self.resource not in ('deployment', 'service', 'ingress') or len(self.name) > 63:
                raise ValueError('Choose a Deployment, Service or Ingress with a name of up to 63 characters')
            if self.action != 'create' and not self.uid:
                raise ValueError('Select the current resource UID')
            if self.action == 'remove' and self.confirm_name != self.name:
                raise ValueError('Type the resource name to confirm')
            if self.action in ('create', 'update'):
                if not self.workload:
                    raise ValueError('Workload settings are required')
                kube_workloads.manifest(self.resource, self.name, self.namespace, self.workload)
            if self.action == 'update' and self.resource != 'deployment':
                raise ValueError('Only Deployment updates are supported')
        if self.action not in ('inventory', 'edge-inventory', 'edge-status') and not self.name:
            raise ValueError('Select a resource')
        if self.action in ('delete', 'restart', 'exec-check'):
            if self.resource != 'pod' or not self.uid:
                raise ValueError('Select a pod and its current UID')
            if self.action != 'exec-check' and self.confirm_name != self.name:
                raise ValueError('Type the pod name to confirm')
            if self.action == 'exec-check' and not self.container:
                raise ValueError('Select a container')
        return self


def manage(manager, store, identity, user, operation):
    from .devops import row
    record = row(store, 'devops_clusters', identity)
    if record['status'] != 'ready':
        raise HTTPException(409, 'Cluster must be Ready before managing workloads')
    first = record['nodes'][0]
    result = manager.rpc(first['host_id'], first['id'] + '/manage', user, operation.model_dump())
    return result


# Included in the existing guest dispatcher, using its validated request body b.
# Only summaries enter inventory; pod specs, environment and credentials do not.
GUEST = r'''
r = b['request']
def kube(args, payload=None):
    p = subprocess.run(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=8s'] + args,
        input=payload, capture_output=True, text=True, timeout=12)
    if p.returncode:
        raise ValueError((p.stderr.strip() or 'Kubernetes request failed')[:1500])
    return p.stdout
def get(kind, name='', namespace=None):
    return json.loads(kube(['get', kind] + ([name] if name else []) +
        (['--namespace', namespace] if namespace else []) + ['-o', 'json']))
def owner(p):
    return next((o for o in p['metadata'].get('ownerReferences', []) if o.get('controller')), {})
def restartable(p):
    return owner(p).get('kind') in ('ReplicaSet', 'StatefulSet', 'DaemonSet', 'ReplicationController') and not p['metadata'].get('annotations', {}).get('kubernetes.io/config.mirror')
try:
    if r['action'] == 'edge-inventory':
        result = workload_inventory()
    elif r['action'] == 'edge-status':
        result = edge_status()
    elif r['action'] in ('create', 'update', 'remove'):
        result = workload_mutation()
    elif r['action'] == 'inventory':
        nodes = get('nodes')['items']
        pods = json.loads(kube(['get', 'pods', '--all-namespaces', '-o', 'json']))['items']
        result = dict(workload_inventory(), nodes=[], pods=[])
        for n in nodes:
            m, s = n['metadata'], n.get('status', {})
            ready = next((c['status'] for c in s.get('conditions', []) if c['type'] == 'Ready'), 'Unknown')
            result['nodes'].append({'name': m['name'], 'uid': m['uid'], 'ready': ready,
                'unschedulable': n.get('spec', {}).get('unschedulable', False),
                'roles': [k.split('/', 1)[1] for k in m.get('labels', {}) if k.startswith('node-role.kubernetes.io/')] or ['worker'],
                'addresses': s.get('addresses', []), 'version': s.get('nodeInfo', {}).get('kubeletVersion', ''),
                'capacity': s.get('capacity', {}), 'allocatable': s.get('allocatable', {})})
        for p in pods:
            m, s, spec = p['metadata'], p.get('status', {}), p.get('spec', {})
            statuses = s.get('containerStatuses', [])
            waiting = [c.get('state', {}).get('waiting', {}).get('reason') for c in s.get('initContainerStatuses', []) + statuses]
            reason = next((v for v in waiting if v), '')
            containers = [{'name': c['name'], 'running': any(x['name'] == c['name'] and 'running' in x.get('state', {}) for x in statuses)} for c in spec.get('containers', [])]
            result['pods'].append({'name': m['name'], 'namespace': m['namespace'], 'uid': m['uid'],
                'created': m.get('creationTimestamp', ''), 'phase': s.get('phase', 'Unknown'),
                'status': 'Terminating' if m.get('deletionTimestamp') else reason or s.get('reason') or s.get('phase', 'Unknown'),
                'ready': sum(bool(c.get('ready')) for c in statuses), 'containers': containers,
                'restarts': sum(c.get('restartCount', 0) for c in statuses), 'node': spec.get('nodeName', ''),
                'ip': s.get('podIP', ''), 'owner': owner(p).get('kind', '') + ('/' + owner(p)['name'] if owner(p) else ''),
                'restartable': bool(restartable(p) and not m.get('deletionTimestamp')),
                'deletable': not bool(m.get('annotations', {}).get('kubernetes.io/config.mirror') or m.get('deletionTimestamp'))})
    elif r['action'] == 'describe':
        result = {'text': kube(['describe', r['resource'], r['name']] + (['--namespace', r['namespace']] if r['resource'] != 'node' else []))}
    else:
        p = get('pod', r['name'], r['namespace'])
        if p['metadata']['uid'] != r['uid']:
            raise ValueError('Pod changed since it was selected. Refresh before continuing.')
        if p['metadata'].get('deletionTimestamp'):
            raise ValueError('Pod is already terminating. Refresh to see its replacement.')
        if r['action'] == 'exec-check':
            if p.get('status', {}).get('phase') != 'Running' or not any(c['name'] == r['container'] and 'running' in c.get('state', {}) for c in p.get('status', {}).get('containerStatuses', [])):
                raise ValueError('Choose a running container; refresh the pod list.')
            result = {'ready': True}
        else:
            if p['metadata'].get('annotations', {}).get('kubernetes.io/config.mirror'):
                raise ValueError('Static mirror pods must be managed on their node.')
            if r['action'] == 'restart' and not restartable(p):
                raise ValueError('Restart requires a ReplicaSet, StatefulSet, DaemonSet or ReplicationController. Standalone and Job pods are not restarted.')
            options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': r['uid']}}
            kube(['delete', '--raw', '/api/v1/namespaces/' + r['namespace'] + '/pods/' + r['name'], '-f', '-'], json.dumps(options))
            result = {'message': 'Pod recreation requested; its controller manages replacement.' if r['action'] == 'restart' else 'Pod deletion requested. A controller may create a replacement.'}
    print(json.dumps(result))
except (ValueError, subprocess.TimeoutExpired) as exc:
    print(json.dumps({'error': str(exc)}))
'''

GUEST = GUEST.replace('try:\n    if r[', kube_workloads.GUEST + '\ntry:\n    if r[')
