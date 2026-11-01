import json
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault('VM_MANAGER_DATA', tempfile.mkdtemp(prefix='vmm-devops-tests-'))

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import devops, main, kubernetes, features
from app.hypervisor import HypervisorError, HypervisorNotFound
from app.security import Store, hash_password

HOST = 'a' * 32
USER = {'username': 'operator', 'role': 'operator'}


class FakeIncus:
    def __init__(self):
        self.instances = {}
        self.nets = [{'name': 'existing', 'type': 'bridge', 'managed': True, 'config': {'ipv4.address': '10.200.0.1/24'}},
                     {'name': 'eth0', 'type': 'physical', 'managed': False, 'config': {}}]
        self.calls = []

    def pools(self): return [{'name': 'local', 'driver': 'dir'}]
    def vm(self, name):
        if name not in self.instances: raise HypervisorNotFound('VM not found')
        return self.instances[name]
    def get(self, path):
        if path == '/1.0/networks?recursion=1': return self.nets
        if path.endswith('/leases'): return [{'address': '10.200.0.2'}]
        if path == '/1.0/instances?recursion=1': return list(self.instances.values())
        if path.startswith('/1.0/networks/'): return next(n for n in self.nets if n['name'] == path.split('/')[-1])
        if path.endswith('/state'): return {'network': {'eth0': {'addresses': [{'family': 'inet', 'scope': 'global', 'address': '10.200.0.3'}]}}}
        raise AssertionError(path)
    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == '/1.0/networks': self.nets.append(dict(body, managed=True))
        elif path == '/1.0/instances': self.instances[body['name']] = dict(body, status='Stopped')
        elif path.endswith('/state'): self.instances[path.split('/')[-2]]['status'] = 'Running' if body['action'] == 'start' else 'Stopped'
        elif method == 'DELETE': del self.instances[path.split('/')[-1]]
        return {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path)
    devops.initialize(store)
    jobs = []
    def submit(user, action, resource, fn, resources=None):
        jobs.append(SimpleNamespace(action=action, resource=resource, fn=fn, resources=resources))
        return {'task_id': str(len(jobs))}
    incus = FakeIncus()
    agent = devops.Agent(store, incus, submit)
    federation = SimpleNamespace(identity=lambda: {'name': 'Local'}, hosts=lambda: [{'id': HOST, 'name': 'Remote'}],
                                 host=lambda identity: {'id': identity, 'name': 'Remote'})
    manager = devops.Manager(store, agent, federation, submit)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'incus', incus)
    monkeypatch.setattr(main, 'devops_agent', agent)
    monkeypatch.setattr(main, 'devops_manager', manager)
    with store.db() as db:
        for role in ('operator', 'viewer'):
            db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,0)', (role, hash_password('test-password'), role))
    client = TestClient(main.app)
    def login(role='operator'):
        return {'X-CSRF-Token': client.post('/api/login', json={'username': role, 'password': 'test-password'}).json()['csrf']}
    return SimpleNamespace(store=store, jobs=jobs, incus=incus, agent=agent, manager=manager, client=client, login=login)


def request(**changes):
    return devops.Request.model_validate(dict(name='test', masters=[{'pool': 'local', 'network': 'existing'}],
        workers=[{'pool': 'local', 'network': 'existing'}]) | changes)


def test_validation_quorum_resources_networks_and_injection():
    base = request().model_dump()
    for changes in ({'name':'bad;command'}, {'masters':[base['masters'][0]] * 2},
                    {'masters':[dict(base['masters'][0], cpu=1)]}, {'workers':[dict(base['workers'][0], memory_gib=0)]},
                    {'service_cidr':'10.42.0.0/16'}, {'pod_cidr':'bad'}, {'workers':[dict(base['workers'][0], host_id='../secret')]},
                    {'new_network':{'name':'new', 'dhcp':False}},
                    {'workers':[dict(base['workers'][0], host_id=HOST)], 'new_network':{'name':'new'}},
                    {'new_network':{'name':'new', 'kind':'lan', 'parents':{'':'eth0;rm'}}}):
        with pytest.raises(ValidationError): devops.Request.model_validate(base | changes)
    devops.Request.model_validate(base | {'new_network': {'name':'new', 'kind':'lan', 'parents':{'':'eth0'}}})


def test_api_permissions_csrf_duplicates_and_no_secrets(env):
    body = request().model_dump()
    assert env.client.post('/api/devops/clusters', json=body).status_code == 401
    env.login()
    assert env.client.post('/api/devops/clusters', json=body).status_code == 403
    viewer = env.login('viewer')
    assert env.client.get('/api/devops/clusters').status_code == 200
    for path in ('clusters', 'agent/reserve', 'agent/' + HOST + '/prepare'):
        assert env.client.post('/api/devops/' + path, headers=viewer, json=body).status_code == 403
    assert env.client.get('/api/devops/agent/' + HOST + '/token').status_code == 403
    headers = env.login()
    result = env.client.post('/api/devops/clusters', headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert env.client.post('/api/devops/clusters', headers=headers, json=body).status_code == 409
    identity = result.json()['cluster_id']
    assert env.client.post(f'/api/devops/clusters/{identity}/retry', headers=headers).status_code == 409
    assert env.client.post(f'/api/devops/clusters/{identity}/kubeconfig', headers=headers).status_code == 409
    assert env.client.request('DELETE', f'/api/devops/clusters/{identity}', headers=headers, json={'confirm_name':'wrong'}).status_code == 400
    data = env.client.get('/api/devops/clusters').json()
    assert 'token' not in json.dumps(data) and 'kubeconfig' not in json.dumps(data)
    assert len(env.jobs) == 1


def test_provision_is_idempotent_and_protects_unrelated_vms(env):
    cluster_id = env.manager.create(request(), USER)['cluster_id']
    node = devops.row(env.store, 'devops_clusters', cluster_id)['nodes'][0]
    spec = devops.NodeRequest.model_validate(node)
    env.agent.reserve(spec)
    env.agent.provision(spec)
    created = env.incus.instances[spec.name]
    assert created['source']['alias'] == 'ubuntu/24.04/cloud'
    assert created['profiles'] == []
    assert created['devices']['eth0']['ipv4.address'] == '10.200.0.3'
    assert created['config']['user.vmm.devops-node'] == spec.id
    env.agent.provision(spec)
    assert len([c for c in env.incus.calls if c[1] == '/1.0/instances']) == 1
    created['config']['user.vmm.devops-node'] = 'someone-else'
    with pytest.raises(HypervisorError, match='already exists'): env.agent.provision(spec)
    with pytest.raises(HypervisorError, match='ownership'): env.agent.owned(spec.id)


def test_new_network_settings_and_overlap_checks(env):
    spec = request(new_network={'name':'k3s-lan','kind':'lan','parents':{'':'eth0'}})
    identity = env.manager.create(spec, USER)['cluster_id']
    node = devops.NodeRequest.model_validate(devops.row(env.store, 'devops_clusters', identity)['nodes'][0])
    env.agent.reserve(node); env.agent.provision(node)
    network = next(c[2] for c in env.incus.calls if c[1] == '/1.0/networks')
    assert network['type'] == 'macvlan' and network['config']['parent'] == 'eth0'
    node.pod_cidr = '10.200.0.0/16'
    with pytest.raises(HypervisorError, match='overlap'): env.agent.validate(node)


def test_cross_host_dispatch_readiness_retry_and_recovery(env, monkeypatch):
    remote = dict(pool='local', network='existing', host_id=HOST)
    calls = []
    original_rpc = env.manager.rpc
    names = ['test-master-1', 'test-worker-1']
    def rpc(host, path, user, body=None):
        calls.append((host, path, body))
        if path == 'validate' and host: return {'ready':True}
        if path == 'reserve': return {}
        if path.endswith('/prepare'): return {'task_id':'node'}
        if path.endswith('/record'): return {'status':'installed','message': '10.200.0.3' if not host else '10.200.0.4'}
        if path.endswith('/token'): return {'token': 'secret-token-never-persisted-0123456789'}
        if path.endswith('/status'): return {'nodes':[{'metadata':{'name':n},'status':{'conditions':[{'type':'Ready','status':'True'}]}} for n in names]}
        return original_rpc(host, path, user, body)
    # Creation validated before queueing, so remote validation is emulated separately below.
    monkeypatch.setattr(env.manager, 'rpc', rpc)
    identity = env.manager.create(request(workers=[remote]), USER)['cluster_id']
    env.jobs[0].fn()
    record = devops.row(env.store, 'devops_clusters', identity)
    assert record['status'] == 'ready'
    assert all(n['status'] == 'ready' for n in record['nodes'])
    joined = next(c for c in calls if c[0] == HOST and c[1].endswith('/prepare'))
    assert joined[2]['server'] == '10.200.0.3'
    assert 'secret-token' not in json.dumps(record)
    with env.store.db() as db: db.execute("UPDATE devops_clusters SET status='deploying'")
    devops.initialize(env.store)
    assert devops.row(env.store, 'devops_clusters', identity)['status'] == 'interrupted'
    env.manager.retry(identity, USER)
    assert len(env.jobs) == 2


def test_failure_retains_nodes_and_delete_checks_ownership(env, monkeypatch):
    identity = env.manager.create(request(), USER)['cluster_id']
    node = devops.NodeRequest.model_validate(devops.row(env.store, 'devops_clusters', identity)['nodes'][0])
    env.agent.reserve(node); env.agent.provision(node)
    monkeypatch.setattr(env.agent, 'guest', lambda *a, **kw: (_ for _ in ()).throw(HypervisorError('Guest failed')))
    monkeypatch.setattr(devops, 'wait_for', lambda fn, *a, **kw: fn() or (_ for _ in ()).throw(HypervisorError('Agent timeout')))
    env.agent.prepare(node.id, USER, devops.Install(token='a'*64))
    with pytest.raises(HypervisorError): env.jobs[-1].fn()
    assert node.name in env.incus.instances
    assert devops.row(env.store, 'devops_nodes', node.id)['status'] == 'failed'
    env.incus.instances[node.name]['config']['user.vmm.devops-node'] = 'other'
    env.agent.delete(node.id, USER)
    with pytest.raises(HypervisorError, match='ownership'): env.jobs[-1].fn()
    assert node.name in env.incus.instances


def test_terminal_rejects_viewer_csrf_and_non_master(env):
    env.login('viewer')
    with pytest.raises(Exception):
        with env.client.websocket_connect('/api/devops/agent/' + HOST + '/terminal?csrf=bad', headers={'Origin':'http://testserver'}): pass


def test_kubeconfig_download_is_operator_only_audited_and_not_cached(env, monkeypatch):
    identity = env.manager.create(request(), USER)['cluster_id']
    record = devops.row(env.store, 'devops_clusters', identity)
    record['nodes'][0]['address'] = '10.200.0.3'
    with env.store.db() as db:
        db.execute("UPDATE devops_clusters SET status='ready',nodes=? WHERE id=?", (json.dumps(record['nodes']), identity))
    monkeypatch.setattr(env.manager, 'rpc', lambda *a: {'kubeconfig':'server: https://127.0.0.1:6443\nclient-key-data: PRIVATE'})
    headers = env.login()
    response = env.client.post(f'/api/devops/clusters/{identity}/kubeconfig', headers=headers, json={})
    assert response.status_code == 200
    assert 'https://10.200.0.3:6443' in response.json()['kubeconfig']
    assert response.headers['cache-control'] == 'no-store'
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='devops.kubeconfig.download'").fetchone()[0] == 1
        assert 'PRIVATE' not in str(db.execute('SELECT * FROM tasks').fetchall())
    viewer = env.login('viewer')
    assert env.client.post(f'/api/devops/clusters/{identity}/kubeconfig', headers=viewer, json={}).status_code == 403


def test_readiness_requires_every_requested_node_and_does_not_claim_success(env, monkeypatch):
    identity = env.manager.create(request(), USER)['cluster_id']
    def rpc(host, path, user, body=None):
        if path == 'reserve' or path.endswith('/prepare'): return {}
        if path.endswith('/record'): return {'status':'installed','message':'10.200.0.3'}
        if path.endswith('/token'): return {'token':'a'*64}
        if path.endswith('/status'): return {'nodes':[{'metadata':{'name':'test-master-1'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}]}
        raise AssertionError(path)
    monkeypatch.setattr(env.manager, 'rpc', rpc)
    monkeypatch.setattr(devops, 'wait_for', lambda fn, message, **kw: fn() or (_ for _ in ()).throw(HypervisorError(message)))
    with pytest.raises(HypervisorError, match='Not every requested'):
        env.jobs[0].fn()
    record = devops.row(env.store, 'devops_clusters', identity)
    assert record['status'] == 'failed'
    assert record['nodes'][0]['status'] == 'ready' and record['nodes'][1]['status'] == 'waiting'


@pytest.fixture
def ready_cluster(env):
    identity = env.manager.create(request(), USER)['cluster_id']
    node = devops.NodeRequest.model_validate(devops.row(env.store, 'devops_clusters', identity)['nodes'][0])
    env.agent.reserve(node)
    env.agent.provision(node)
    with env.store.db() as db:
        db.execute("UPDATE devops_clusters SET status='ready' WHERE id=?", (identity,))
        db.execute("UPDATE devops_nodes SET status='installed' WHERE id=?", (node.id,))
    return identity, node


def test_management_read_write_roles_csrf_and_audit(env, ready_cluster, monkeypatch):
    identity, node = ready_cluster
    calls = []
    monkeypatch.setattr(env.agent, 'guest', lambda *args, **kw: calls.append(kw) or {'nodes':[], 'pods':[], 'text':'description'})
    endpoint = '/api/devops/clusters/' + identity
    viewer = env.login('viewer')
    assert env.client.get(endpoint + '/resources').status_code == 200
    assert env.client.post(endpoint + '/manage', headers=viewer, json={'action':'describe','name':'node','resource':'node'}).status_code == 200
    body = {'action':'delete','name':'pod','namespace':'default','uid':'old-uid','confirm_name':'pod'}
    for path in (endpoint + '/manage', '/api/devops/agent/' + node.id + '/manage'):
        assert env.client.post(path, headers=viewer, json=body).status_code == 403
    operator = env.login()
    assert env.client.post(endpoint + '/manage', json=body).status_code == 403
    response = env.client.post(endpoint + '/manage', headers=operator, json=body)
    assert response.status_code == 200, response.text
    with env.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='devops.pod.delete'").fetchone()[0] == 1
    body['confirm_name'] = 'wrong'
    assert env.client.post(endpoint + '/manage', headers=operator, json=body).status_code == 422
    assert len(calls) == 3


def test_management_routes_to_first_master_and_requires_ready(env, monkeypatch):
    identity = env.manager.create(request(), USER)['cluster_id']
    with pytest.raises(Exception, match='Ready'):
        kubernetes.manage(env.manager, env.store, identity, USER, kubernetes.Operation())
    record = devops.row(env.store, 'devops_clusters', identity)
    record['nodes'][0]['host_id'] = HOST
    with env.store.db() as db:
        db.execute("UPDATE devops_clusters SET status='ready',nodes=? WHERE id=?", (json.dumps(record['nodes']), identity))
    calls = []
    monkeypatch.setattr(env.manager, 'rpc', lambda *args: calls.append(args) or {'nodes':[], 'pods':[]})
    kubernetes.manage(env.manager, env.store, identity, USER, kubernetes.Operation())
    assert calls[0][:2] == (HOST, record['nodes'][0]['id'] + '/manage')


@pytest.mark.parametrize('changes', [{'name':'--all'}, {'name':'x;touch file'}, {'namespace':'../x'},
    {'uid':'x/../../'}, {'container':'x;env'}, {'shell':'/bin/sh -c env'}, {'resource':'node'}, {'confirm_name':'wrong'}])
def test_management_rejects_unsafe_or_unconfirmed_targets(changes):
    with pytest.raises(ValidationError):
        kubernetes.Operation.model_validate({'action':'delete','name':'pod','uid':'abc','confirm_name':'pod'} | changes)


def guest_operation(operation, pod=None):
    """Execute the real guest dispatcher branch with a fake kubectl transport."""
    import subprocess
    from contextlib import redirect_stdout
    from io import StringIO
    calls = []
    pod = pod or {'metadata':{'name':'pod','namespace':'default','uid':'abc',
        'ownerReferences':[{'controller':True,'kind':'ReplicaSet','name':'app'}]},
        'spec':{'containers':[{'name':'web','env':[{'name':'SECRET','value':'do-not-return'}]}]},
        'status':{'phase':'Running','containerStatuses':[{'name':'web','ready':True,'state':{'running':{}},'restartCount':2}]}}
    def run(args, **kw):
        calls.append((args, kw))
        if 'nodes' in args:
            output = {'items':[{'metadata':{'name':'master','uid':'node'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}]}
        elif 'pods' in args:
            output = {'items':[pod]}
        elif 'get' in args:
            output = pod
        else:
            output = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr='')
    output = StringIO()
    with redirect_stdout(output):
        exec(kubernetes.GUEST, {'b':{'request':operation.model_dump()}, 'json':json,
             'subprocess':SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)})
    return json.loads(output.getvalue()), calls


def test_guest_inventory_returns_safe_summaries():
    result, calls = guest_operation(kubernetes.Operation())
    assert result['nodes'][0]['ready'] == 'True'
    pod = result['pods'][0]
    assert pod['restartable'] and pod['ready'] == 1 and pod['restarts'] == 2
    assert pod['containers'] == [{'name':'web','running':True}]
    assert 'do-not-return' not in json.dumps(result)


def test_guest_delete_precondition_and_restart_semantics():
    operation = kubernetes.Operation(action='restart', name='pod', uid='abc', confirm_name='pod')
    result, calls = guest_operation(operation)
    assert 'recreation requested' in result['message']
    args, kw = calls[-1]
    assert args[-5:] == ['delete','--raw','/api/v1/namespaces/default/pods/pod','-f','-']
    assert json.loads(kw['input'])['preconditions'] == {'uid':'abc'}
    result, calls = guest_operation(operation.model_copy(update={'uid':'different'}))
    assert 'changed' in result['error'] and len(calls) == 1
    standalone = {'metadata':{'name':'pod','namespace':'default','uid':'abc'}, 'spec':{}, 'status':{}}
    result, calls = guest_operation(operation, standalone)
    assert 'Standalone and Job pods' in result['error'] and len(calls) == 1
    standalone['metadata']['annotations'] = {'kubernetes.io/config.mirror':'hash'}
    result, calls = guest_operation(operation.model_copy(update={'action':'delete'}), standalone)
    assert 'Static mirror' in result['error'] and len(calls) == 1


def test_pod_exec_validates_running_container_and_uses_literal_arguments():
    from app.guest_terminal import kubernetes_command
    operation = kubernetes.Operation(action='exec-check', name='pod', uid='abc', container='web')
    assert guest_operation(operation)[0] == {'ready':True}
    assert 'running container' in guest_operation(operation.model_copy(update={'container':'missing'}))[0]['error']
    args = kubernetes_command(operation)
    assert '--reuid=vmm-kube' in args
    assert args[args.index('/usr/local/bin/k3s'):] == ['/usr/local/bin/k3s','kubectl','exec','--stdin','--tty','--namespace','default','pod','--container','web','--','/bin/sh']
    assert '-c' not in args


def test_terminal_validates_pod_and_preserves_transport(env, ready_cluster, monkeypatch):
    identity, node = ready_cluster
    headers = env.login()
    calls = []
    monkeypatch.setattr(env.agent, 'guest', lambda *args, **kw: {'ready':True})
    async def bridge(ws, *args, **kw):
        calls.append(kw)
        await ws.accept()
        await ws.send_json({'type':'ready'})
        await ws.close()
    monkeypatch.setattr(main.guest_terminal, 'bridge', bridge)
    path = f'/api/devops/agent/{node.id}/terminal?csrf={headers["X-CSRF-Token"]}&pod=pod&namespace=default&uid=abc&container=web&shell=/bin/sh'
    with env.client.websocket_connect(path, headers={'Origin':'http://testserver'}) as ws:
        assert ws.receive_json()['type'] == 'ready'
    assert calls[0]['pod'].container == 'web'
    assert calls[0]['mode'] == 'kubernetes'
    with pytest.raises(Exception):
        with env.client.websocket_connect(path.replace('container=web', 'container=bad;command'), headers={'Origin':'http://testserver'}): pass
    assert len(calls) == 1


def test_devops_entitlement_covers_api_agent_proxy_and_websockets(env, monkeypatch):
    headers = env.login()
    assert env.client.get('/api/me').json()['features']['devops']['enabled']
    monkeypatch.setenv('VM_MANAGER_DEVOPS_ENABLED', 'false')
    assert not env.client.get('/api/me').json()['features']['devops']['enabled']
    for path in ('/api/devops/options', '/api/devops/clusters', '/api/devops/agent/options',
                 f'/api/cluster/hosts/{HOST}/proxy/devops/agent/options'):
        assert env.client.get(path).status_code == 403
    assert env.client.post('/api/devops/clusters', headers=headers, json=request().model_dump()).status_code == 403
    for path in (f'/api/devops/agent/{HOST}/terminal', f'/api/cluster/hosts/{HOST}/proxy/devops/agent/{HOST}/terminal'):
        with pytest.raises(Exception):
            with env.client.websocket_connect(path, headers={'Origin':'http://testserver'}): pass
    with env.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM devops_clusters').fetchone()[0] == 0
    monkeypatch.delenv('VM_MANAGER_DEVOPS_ENABLED')
    assert features.catalog()['devops']['enabled']


def test_delete_restart_is_recoverable_and_deleted_names_can_be_reused(env):
    identity = env.manager.create(request(), USER)['cluster_id']
    node = devops.NodeRequest.model_validate(devops.row(env.store, 'devops_clusters', identity)['nodes'][0])
    env.agent.reserve(node)
    with env.store.db() as db:
        db.execute("UPDATE devops_nodes SET status='deleting'")
        db.execute("UPDATE devops_clusters SET status='deleting'")
    devops.initialize(env.store)
    assert devops.row(env.store, 'devops_nodes', node.id)['status'] == 'interrupted'
    assert devops.row(env.store, 'devops_clusters', identity)['status'] == 'interrupted'
    with env.store.db() as db:
        db.execute("UPDATE devops_clusters SET status='deleted'")
        db.execute("UPDATE devops_nodes SET status='deleted'")
    assert env.manager.create(request(), USER)['cluster_id'] != identity


def test_delete_does_not_require_a_working_network_or_reserve_uncreated_nodes(env, monkeypatch):
    identity = env.manager.create(request(), USER)['cluster_id']
    record = devops.row(env.store, 'devops_clusters', identity)
    node = devops.NodeRequest.model_validate(record['nodes'][0])
    env.agent.reserve(node); env.agent.provision(node)
    env.incus.nets.clear()  # Cleanup must remain possible after network changes.
    with env.store.db() as db: db.execute("UPDATE devops_clusters SET status='failed'")
    original_submit = env.agent.submit
    def immediate(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        env.jobs[-1].fn()
        return result
    monkeypatch.setattr(env.agent, 'submit', immediate)
    env.manager.retry(identity, USER, delete=True)
    env.jobs[-1].fn()
    assert devops.row(env.store, 'devops_clusters', identity)['status'] == 'deleted'
    assert node.name not in env.incus.instances
    with env.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM devops_nodes').fetchone()[0] == 1


def test_owned_node_deletion_uses_application_protections(env, monkeypatch):
    identity = env.manager.create(request(), USER)['cluster_id']
    node = devops.NodeRequest.model_validate(devops.row(env.store, 'devops_clusters', identity)['nodes'][0])
    env.agent.reserve(node); env.agent.provision(node)
    calls = []
    def remove(name, actor):
        calls.append((name, actor))
        raise HypervisorError('Detach external storage before deleting this node')
    env.agent.remove = remove
    env.agent.delete(node.id, USER)
    with pytest.raises(HypervisorError, match='Detach external storage'): env.jobs[-1].fn()
    assert calls == [(node.name, USER['username'])]
    assert node.name in env.incus.instances


def test_failed_task_preflight_releases_node_for_retry(env):
    identity = env.manager.create(request(), USER)['cluster_id']
    node = devops.NodeRequest.model_validate(devops.row(env.store, 'devops_clusters', identity)['nodes'][0])
    env.agent.reserve(node)
    result = env.agent.prepare(node.id, USER, devops.Install(token='a'*64))
    with env.store.db() as db:
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)', (result['task_id'],'devops.node.prepare',node.name,'operator','failed',0,1,'Patch operation is active'))
    assert env.agent.record(node.id)['status'] == 'failed'
    env.agent.prepare(node.id, USER, devops.Install(token='a'*64))
    assert len(env.jobs) == 3


def test_delete_tracks_its_own_task_instead_of_the_completed_install(env):
    identity = env.manager.create(request(), USER)['cluster_id']
    node = devops.NodeRequest.model_validate(devops.row(env.store, 'devops_clusters', identity)['nodes'][0])
    env.agent.reserve(node); env.agent.provision(node)
    with env.store.db() as db:
        db.execute("UPDATE devops_nodes SET status='installed',task_id='old-install'")
        db.execute("INSERT INTO tasks VALUES('old-install','devops.node.prepare',?,'operator','completed',0,1,'Installed')", (node.name,))
    result = env.agent.delete(node.id, USER)
    with env.store.db() as db:
        db.execute("INSERT INTO tasks VALUES(?,'devops.node.delete',?,'operator','running',2,NULL,'')", (result['task_id'], node.name))
    record = env.agent.record(node.id)
    assert record['status'] == 'deleting' and record['task_id'] == result['task_id']
    env.jobs[-1].fn()
    assert env.agent.record(node.id)['status'] == 'deleted'
