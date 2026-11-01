import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import devops, edge, edge_runtime, kube_workloads, kubernetes, main, kube_admission
from app.hypervisor import HypervisorError
from app.security import Store, hash_password

IDENTITY = 'a' * 32
ADMIN = {'username': 'admin', 'role': 'administrator'}


def service(name='web', port=18088, uid='first'):
    return dict(name=name, namespace='default', uid=uid, type='LoadBalancer', lb_class=kube_workloads.CLASS,
                ports=[{'port':port, 'nodePort':31001, 'protocol':'TCP'}], external=[], external_traffic_policy='Cluster')


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path)
    devops.initialize(store)
    edge.initialize(store)
    with store.db() as db:
        db.execute('INSERT INTO devops_clusters VALUES(?,?,?,?,?,?,?,?)',
            (IDENTITY, 'test', '{}', json.dumps([{'id':'b'*32, 'host_id':'', 'address':'10.210.0.2'}]), 'ready', '', 0, ''))
        for role in ('administrator', 'operator', 'viewer'):
            db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,0)', (role, hash_password('password-test'), role))
    calls, requests, jobs = [], [], []
    inventory = {'services':[service()], 'deployments':[], 'ingresses':[], 'namespaces':['default'], 'admission_version':kube_admission.VERSION}
    def rpc(host, path, user, body=None):
        requests.append((host, path, body))
        if body['action'] == 'edge-inventory': return json.loads(json.dumps(inventory))
        if body['action'] == 'edge-bootstrap': return {'ready':True,'version':kube_admission.VERSION}
        return {}
    def submit(user, action, resource, fn, resources):
        jobs.append(fn)
        return {'task_id':str(len(jobs))}
    manager = edge.Manager(store, SimpleNamespace(rpc=rpc), submit)
    def broker(action, **kwargs):
        calls.append((action, kwargs))
        if action == 'edge_info': return {'existing_tunnel':{'id':'legacy', 'account_id':'c'*32, 'hostnames':['vmm.example.test']}}
        return {'running':True, 'components':{'https':True,'tcp':True}}
    monkeypatch.setattr(edge.storage, 'broker', broker)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'edge_manager', manager)
    return SimpleNamespace(store=store, manager=manager, calls=calls, requests=requests, inventory=inventory, jobs=jobs, client=TestClient(main.app))


def launch(env):
    env.manager.launch(ADMIN)
    env.jobs[-1]()


def test_edge_api_admin_csrf_feature_boundary_and_secret_redaction(env, monkeypatch):
    path = '/api/devops/load-balancer'
    assert env.client.get(path).status_code == 401
    for role in ('viewer', 'operator', 'administrator'):
        login = env.client.post('/api/login', json={'username':role, 'password':'password-test'}).json()
        assert env.client.get(path).status_code == 200
        assert env.client.post(path + '/launch').status_code == 403
        response = env.client.post(path + '/launch', headers={'X-CSRF-Token':login['csrf']})
        assert response.status_code == (200 if role == 'administrator' else 403)
    settings, status = env.manager.read()
    settings['cloudflare']['token'] = 'never-return-this-token'
    settings['routes'].append({'id':'route', 'hostname':'a.example.test', 'certificate':'private-cert', 'private_key':'never-return-this-key'})
    env.manager.save(settings, status)
    public = env.client.get(path).text
    assert 'never-return' not in public and 'private-cert' not in public
    monkeypatch.setenv('VM_MANAGER_DEVOPS_ENABLED', 'false')
    assert env.client.get(path).status_code == 403
    env.manager.tick()
    assert env.calls == []


def test_launch_reuses_one_tunnel_and_native_service_addresses_follow_apply(env):
    launch(env)
    config = next(call[1]['body'] for call in env.calls if call[0] == 'edge_apply')
    assert config['migrate_tunnel'] is True
    assert [(r['mode'], r['hostname']) for r in config['routes']] == [('manager','192.168.8.235'),('cloudflare','vmm.example.test')]
    assert not config['tunnel_token']
    env.manager.bind(ADMIN, IDENTITY, True)
    env.jobs[-1]()
    config = [c[1]['body'] for c in env.calls if c[0] == 'edge_apply'][-1]
    assert config['listeners'][0]['upstreams'] == ['10.210.0.2:31001']
    status_request = [r[2] for r in env.requests if r[2]['action'] == 'edge-status'][-1]
    assert status_request['statuses'][0]['address'] == '192.168.8.235'
    assert env.manager.catalog()['status']['services'][0]['state'] == 'configured'


def test_failed_data_plane_never_advertises_service_ip(env, monkeypatch):
    launch(env)
    env.manager.bind(ADMIN, IDENTITY, True)
    env.requests.clear()
    def failed(action, **kwargs): raise HypervisorError('Listener port occupied')
    monkeypatch.setattr(edge.storage, 'broker', failed)
    with pytest.raises(HypervisorError): env.jobs[-1]()
    assert not [r for r in env.requests if r[2]['action'] == 'edge-status']
    assert env.manager.catalog()['status']['state'] == 'failed'


def test_port_conflicts_unsupported_protocol_and_controller_ownership(env):
    launch(env)
    env.inventory['services'] = [service(), service('other', uid='other'), service('https',443,uid='https'), service('foreign',9999,uid='foreign')]
    env.inventory['services'][-1]['lb_class'] = 'someone-else/controller'
    env.manager.bind(ADMIN, IDENTITY, True)
    env.jobs[-1]()
    statuses = env.manager.catalog()['status']['services']
    assert len(statuses) == 3
    assert sum(s['state'] == 'configured' for s in statuses) == 1
    assert all(s['address'] == '' for s in statuses if s['state'] == 'pending')
    env.inventory['services'] = [service() | {'external_traffic_policy':'Local'}]
    env.manager.reconcile(force=True)
    assert 'externalTrafficPolicy' in env.manager.catalog()['status']['services'][0]['message']


def test_deleted_services_withdraw_listeners_and_disconnect_withdraws_status(env):
    launch(env)
    env.manager.bind(ADMIN, IDENTITY, True); env.jobs[-1]()
    env.inventory['services'][0]['external'] = [{'ip':'192.168.8.235'}]
    env.manager.bind(ADMIN, IDENTITY, False); env.jobs[-1]()
    patch = [r[2] for r in env.requests if r[2]['action'] == 'edge-status'][-1]
    assert patch['statuses'][0]['address'] == ''
    assert [c[1]['body'] for c in env.calls if c[0] == 'edge_apply'][-1]['listeners'] == []
    env.manager.bind(ADMIN, IDENTITY, True); env.jobs[-1]()
    env.inventory['services'] = []
    env.manager.reconcile()
    assert env.manager.catalog()['status']['services'] == []


def test_unreachable_cluster_retains_last_configuration(env, monkeypatch):
    launch(env)
    env.manager.bind(ADMIN, IDENTITY, True); env.jobs[-1]()
    def unavailable(identity): raise HypervisorError('Unavailable')
    monkeypatch.setattr(env.manager, 'inventory', unavailable)
    env.manager.reconcile(force=True)
    assert env.manager.catalog()['status']['state'] == 'degraded'
    assert len([c[1]['body'] for c in env.calls if c[0] == 'edge_apply'][-1]['listeners']) == 1


def test_route_validation_protection_and_secret_replacement(env):
    launch(env)
    for changes in ({'hostname':'x\nrespond evil'}, {'upstreams':['http://evil:80']}, {'id':'manager-lan'}, {'mode':'manager'}):
        with pytest.raises(ValidationError):
            edge.Route.model_validate(dict(hostname='app.example.test', mode='manual', upstreams=['10.0.0.1:8080']) | changes)
    route = edge.Route(hostname='app.example.test', mode='manual', upstreams=['10.0.0.1:8080'], certificate='CERT', private_key='KEY')
    env.manager.route(ADMIN, route)
    saved = env.manager.catalog()['settings']['routes'][-1]
    assert saved['has_certificate'] and 'private_key' not in saved
    env.manager.route(ADMIN, route.model_copy(update={'id':saved['id'], 'certificate':'', 'private_key':''}))
    assert env.manager.read()[0]['routes'][-1]['private_key'] == 'KEY'
    with pytest.raises(Exception, match='protected'): env.manager.remove(ADMIN, 'manager-lan', '192.168.8.235')


def test_cloudflare_only_one_transport_origin_and_owned_dns_cleanup(env, monkeypatch):
    launch(env)
    settings, status = env.manager.read()
    cf = settings['cloudflare']
    cf.update(token='token-test', zone_id='d'*32, local=False, tunnel_token='connector-test')
    settings['routes'].append(dict(id='app',hostname='app.example.test',mode='cloudflare',upstreams=['10.0.0.1:80']))
    calls = []
    def request(cf, method, path, body=None):
        calls.append((method,path,body))
        if path == 'zones/' + cf['zone_id']: return {'name':'example.test','account':{'id':cf['account_id']}}
        if '/dns_records?' in path: return []
        if path.endswith('/dns_records'): return {'id':'owned-record'}
        return {'status':'healthy'}
    monkeypatch.setattr(env.manager, 'cf_request', request)
    env.manager.sync_cloudflare(settings, status)
    config = next(c[2] for c in calls if c[1].endswith('/configurations'))
    assert config == {'config':{'ingress':[{'service':'http://127.0.0.1:18080'}]}}
    assert cf['records']['app.example.test'] == 'owned-record'
    assert not any(method == 'POST' and path.endswith('/cfd_tunnel') for method,path,_ in calls)


def test_cloudflare_dns_conflicts_are_never_overwritten(env, monkeypatch):
    launch(env)
    settings,status = env.manager.read()
    settings['cloudflare'].update(token='test', zone_id='d'*32)
    def request(cf, method, path, body=None):
        assert method == 'GET'
        if path.endswith(cf['zone_id']): return {'name':'example.test','account':{'id':cf['account_id']}}
        return [{'type':'A','content':'192.0.2.1','name':'vmm.example.test'}]
    monkeypatch.setattr(env.manager, 'cf_request', request)
    with pytest.raises(HypervisorError, match='conflict'): env.manager.sync_cloudflare(settings, status)


def test_proxy_config_is_bounded_uses_single_host_routing_and_tls_challenges():
    config = edge_runtime.Configuration(address='192.0.2.10', routes=[
        {'id':'app', 'hostname':'app.example.test','mode':'letsencrypt','email':'admin@example.test','upstreams':['10.0.0.1:8080']},
        {'id':'tunnel','hostname':'other.example.test','mode':'cloudflare','upstreams':['10.0.0.2:8080']}],
        listeners=[{'id':'service','port':12345,'upstreams':['10.0.0.3:31001']}])
    caddy = edge_runtime.caddyfile(config)
    assert 'auto_https disable_redirects' in caddy and 'disable_http_challenge' in caddy
    assert 'trusted_proxies static 127.0.0.1/32' in caddy
    assert 'https://app.example.test' in caddy and 'http://other.example.test:18080' in caddy
    assert 'bind 127.0.0.1' in caddy and 'respond 404' in caddy
    assert 'bind 192.0.2.10:12345' in edge_runtime.haproxyfile(config)
    config.listeners[0].port = 8443
    with pytest.raises(HypervisorError, match='reserved'): edge_runtime.haproxyfile(config)
    with pytest.raises(ValidationError): edge_runtime.Configuration.model_validate({'address':'192.0.2.10','routes':[], 'command':'rm -rf /'})


def test_kubernetes_default_ip_mode_does_not_cause_repeated_status_writes(env):
    launch(env)
    env.manager.bind(ADMIN, IDENTITY, True); env.jobs[-1]()
    env.inventory['services'][0]['external'] = [{'ip':'192.168.8.235','ipMode':'VIP'}]
    env.requests.clear()
    env.manager.reconcile()
    assert not [r for r in env.requests if r[2]['action'] == 'edge-status']


def test_direct_vm_manager_routes_use_a_fixed_verified_https_origin(env):
    launch(env)
    route = edge.Route(hostname='manager.example.test', mode='letsencrypt', backend='vm-manager', email='admin@example.test')
    env.manager.route(ADMIN, route); env.jobs[-1]()
    body = [c[1]['body'] for c in env.calls if c[0] == 'edge_apply'][-1]
    added = body['routes'][-1]
    assert added['upstreams'] == ['192.168.8.235:8443']
    caddy = edge_runtime.caddyfile(edge_runtime.Configuration.model_validate(body))
    assert 'reverse_proxy https://192.168.8.235:8443' in caddy
    assert 'tls_trust_pool file /edge/manager.crt' in caddy
    with pytest.raises(ValidationError):
        edge.Route(hostname='manager.example.test',mode='manual',backend='vm-manager',upstreams=['10.0.0.1:443'])


def test_service_starts_load_balancer_and_connects_cluster_without_setup_calls(env):
    env.inventory['services'] = [service()]
    env.manager.tick()
    settings, status = env.manager.read()
    assert settings['enabled'] and settings['clusters'] == [IDENTITY]
    assert any(c[0] == 'edge_apply' for c in env.calls)
    assert not any(r[2]['action'] in ('edge-bootstrap','edge-claim') for r in env.requests)
    assert env.jobs == []  # No Launch or Connect API/task was required.
    env.manager.reconcile()
    assert env.manager.catalog()['status']['services'][0]['address'] == '192.168.8.235'
    assert any(r[2]['action'] == 'edge-status' for r in env.requests)


def test_empty_cluster_is_connected_but_runtime_starts_only_on_demand(env):
    env.inventory['services'] = []
    env.manager.reconcile()
    settings,status = env.manager.read()
    assert settings['clusters'] == [IDENTITY] and not settings['enabled']
    assert status['state'] == 'standby' and env.calls == []
    env.inventory['services'] = [service()]
    env.manager.reconcile()
    assert env.manager.read()[0]['enabled']


def test_new_ready_clusters_are_discovered_and_pause_survives_restart(env):
    env.inventory['services'] = []
    env.manager.reconcile()
    with env.store.db() as db:
        db.execute('UPDATE devops_clusters SET status=?', ('deploying',))
    settings,status = env.manager.read(); settings['clusters'] = []; env.manager.save(settings,status)
    env.manager.reconcile()
    assert env.manager.read()[0]['clusters'] == []
    with env.store.db() as db:
        db.execute('UPDATE devops_clusters SET status=?', ('ready',))
    env.manager.reconcile()
    assert env.manager.read()[0]['clusters'] == [IDENTITY]
    env.manager.bind(ADMIN, IDENTITY, False); env.jobs[-1]()
    another = edge.Manager(env.store, env.manager.manager, env.manager.submit)
    another.reconcile()
    assert another.read()[0]['clusters'] == []
    env.inventory['services'] = [service()]
    another.reconcile()
    assert not another.read()[0]['enabled']
    env.manager.bind(ADMIN, IDENTITY, True); env.jobs[-1]()
    assert env.manager.read()[0]['enabled']


def test_system_foreign_and_opted_out_services_are_not_claimed_or_autostarted(env):
    env.inventory['services'] = [service('system') | {'namespace':'kube-system','lb_class':''},
        service('foreign',uid='foreign') | {'lb_class':'someone-else/controller'},
        service('opt-out',uid='optout') | {'lb_class':'','automatic_lb':False}]
    env.manager.reconcile()
    assert not env.manager.read()[0]['enabled']
    assert env.calls == []
    assert not any(r[2]['action'] == 'edge-claim' for r in env.requests)


def test_status_waits_for_a_working_runtime_and_previous_provider_cleanup(env, monkeypatch):
    env.inventory['services'] = [service() | {'cleanup_pending':True}]
    original = edge.storage.broker
    monkeypatch.setattr(edge.storage,'broker',lambda action,**kw: original(action,**kw) if action=='edge_info' else (_ for _ in ()).throw(HypervisorError('Port occupied')))
    with pytest.raises(HypervisorError): env.manager.reconcile()
    assert not any(r[2]['action'] == 'edge-claim' for r in env.requests)
    monkeypatch.setattr(edge.storage,'broker',original)
    env.manager.reconcile(); env.requests.clear()
    env.manager.reconcile()
    assert not any(r[2]['action'] == 'edge-status' for r in env.requests)
    assert 'cleanup' in env.manager.catalog()['status']['services'][0]['message']
    env.inventory['services'][0]['cleanup_pending'] = False
    env.manager.reconcile()
    assert any(r[2]['action'] == 'edge-status' for r in env.requests)


def test_new_service_cannot_steal_an_existing_listener(env):
    env.inventory['services'] = [service('zzz-existing', uid='old')]
    env.manager.reconcile()
    env.inventory['services'].append(service('aaa-new', uid='new'))
    env.manager.reconcile()
    results = {s['uid']:s for s in env.manager.catalog()['status']['services']}
    assert results['old']['state'] == 'configured'
    assert results['new']['state'] == 'pending'
    configured = [c[1]['body'] for c in env.calls if c[0] == 'edge_apply'][-1]['listeners']
    assert len(configured) == 1 and configured[0]['id'].endswith('-old')


def test_missing_admission_is_installed_automatically_even_before_runtime_launch(env):
    env.inventory['services'] = []
    env.inventory['admission_version'] = ''
    env.manager.reconcile()
    calls = [r for r in env.requests if r[2]['action'] == 'edge-bootstrap']
    assert len(calls) == 1 and calls[0][2]['admission_publish']
    assert not env.manager.read()[0]['enabled'] and env.calls == []


def test_admission_is_prepared_on_every_master_before_publication(env):
    record = devops.row(env.store,'devops_clusters',IDENTITY)
    record['nodes'] = [{'id':'b'*32,'host_id':'','role':'master'}, {'id':'c'*32,'host_id':'d'*32,'role':'master'}, {'id':'e'*32,'host_id':'','role':'worker'}]
    env.manager.bootstrap_record(record,ADMIN)
    assert [r[0] for r in env.requests] == ['', 'd'*32]
    assert [r[2]['admission_publish'] for r in env.requests] == [False,True]


def test_automatic_enrollment_clears_stale_withdrawals_without_disabling_admission(env):
    env.inventory['services'] = []
    settings,status = env.manager.read()
    settings['withdrawals'] = [IDENTITY]
    env.manager.save(settings,status)
    env.manager.reconcile()
    assert env.manager.read()[0]['withdrawals'] == []
    assert not any(r[2]['action'] == 'edge-bootstrap' and not r[2]['admission_enabled'] for r in env.requests)
