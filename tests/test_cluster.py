"""Cluster trust boundaries and host routing use the real application policy."""
import json
import secrets
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from app import main
from app.cluster import HostCreate, allowed_path
from app.security import token_hash


@pytest.fixture
def client(monkeypatch):
    with main.store.db() as db:
        db.execute('DELETE FROM cluster_hosts')
        db.execute('DELETE FROM cluster_keys')
    monkeypatch.setattr(main.incus, 'vms', lambda: [])
    monkeypatch.setattr(main.libvirt, 'vms', lambda: [])
    main.invalidate()
    client = TestClient(main.app)
    yield client
    with main.store.db() as db:
        db.execute('DELETE FROM cluster_hosts')
        db.execute('DELETE FROM cluster_keys')
        db.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'cluster-test-%')")
        db.execute("DELETE FROM users WHERE username LIKE 'cluster-test-%'")


def signin(client, role='administrator'):
    cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with main.store.db() as db:
        db.execute('INSERT OR IGNORE INTO users(username,password,role,created) VALUES(?,?,?,?)',
                   ('cluster-test-' + role, 'unused', role, time.time()))
        uid = db.execute('SELECT id FROM users WHERE username=?', ('cluster-test-' + role,)).fetchone()[0]
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(cookie), uid, csrf, time.time() + 3600))
    client.cookies.set('vmm_session', cookie)
    return {'X-CSRF-Token': csrf}


def network(monkeypatch, handler):
    monkeypatch.setattr(main.cluster, 'client', lambda host, timeout=10:
                        httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))


def enroll(client, monkeypatch, handler=None):
    def default(req):
        return httpx.Response(200, json={'node_id': 'a' * 32, 'protocol': 1})
    network(monkeypatch, handler or default)
    headers = signin(client)
    body = {'name': 'Second host', 'url': 'https://second.example:8443', 'token': 'T' * 48}
    response = client.post('/api/cluster/hosts', json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['id'], headers, body


def test_link_keys_are_admin_only_once_and_revocable(client):
    assert client.get('/api/cluster/hosts').status_code == 401
    for role in ('viewer', 'operator'):
        headers = signin(client, role)
        assert client.post('/api/cluster/keys', json={'name': 'Main'}, headers=headers).status_code == 403
    headers = signin(client)
    assert client.post('/api/cluster/keys', json={'name': 'Main'}).status_code == 403
    key = client.post('/api/cluster/keys', json={'name': 'Main'}, headers=headers).json()
    listed = client.get('/api/cluster/keys').json()
    assert listed[0]['id'] == key['id'] and 'token' not in listed[0] and 'digest' not in listed[0]
    with main.store.db() as db:
        assert db.execute('SELECT digest FROM cluster_keys').fetchone()[0] == token_hash(key['token'])
    peer = {'Authorization': 'Bearer ' + key['token'], 'X-VMM-Role': 'viewer', 'X-VMM-Actor': 'alice'}
    assert client.get('/api/cluster/identity', headers=peer).status_code == 200
    assert client.get('/api/me', headers=peer).status_code == 403
    assert client.get('/api/cluster/hosts', headers=peer).status_code == 403
    assert client.post('/api/vms/incus:demo/action', headers=peer, json={'action': 'start'}).status_code == 403
    assert client.delete('/api/cluster/keys/' + key['id'], headers=headers).status_code == 200
    assert client.get('/api/cluster/identity', headers=peer).status_code == 401


def test_peers_delegate_roles_and_actor_to_actual_handlers(client, monkeypatch):
    headers = signin(client)
    key = client.post('/api/cluster/keys', json={'name': 'Trusted'}, headers=headers).json()
    calls = []
    monkeypatch.setattr(main, 'vm_ref', lambda vm: ('incus', 'demo'))
    monkeypatch.setattr(main, 'task', lambda user, *a, **k: calls.append(user) or {'task_id': 'remote-task'})
    peer = {'Authorization': 'Bearer ' + key['token'], 'X-VMM-Role': 'operator', 'X-VMM-Actor': 'alice'}
    response = client.post('/api/vms/incus:demo/action', headers=peer, json={'action': 'start'})
    assert response.status_code == 200, response.text
    assert calls[0]['username'] == 'cluster:Trusted/alice' and calls[0]['role'] == 'operator'
    assert client.get('/api/inventory', headers=dict(peer, **{'X-VMM-Role': 'invalid'})).status_code == 403


def test_enrollment_checks_identity_duplicates_and_hides_secrets(client, monkeypatch):
    host_id, headers, body = enroll(client, monkeypatch)
    listing = client.get('/api/cluster/hosts').json()
    assert listing['hosts'][0]['id'] == host_id
    assert 'token' not in listing['hosts'][0] and 'certificate' not in listing['hosts'][0]
    assert client.post('/api/cluster/hosts', json=body, headers=headers).status_code == 409
    network(monkeypatch, lambda req: httpx.Response(200, json=main.cluster.identity()))
    assert client.post('/api/cluster/hosts', json=body, headers=headers).status_code == 409
    network(monkeypatch, lambda req: httpx.Response(200, json={'node_id': 'b'*32, 'protocol': 999}))
    assert client.post('/api/cluster/hosts', json=body, headers=headers).status_code == 409
    assert client.delete('/api/cluster/hosts/' + host_id, headers=headers).status_code == 200
    assert client.get('/api/cluster/hosts/' + host_id + '/proxy/inventory').status_code == 404


def test_proxy_preserves_host_scope_role_body_and_task_stream(client, monkeypatch):
    host_id, headers, body = enroll(client, monkeypatch)
    seen = []
    def remote(req):
        seen.append(req)
        assert req.url.host == 'second.example'
        assert req.headers['authorization'] == 'Bearer ' + body['token']
        assert req.headers['x-vmm-role'] == 'operator'
        assert req.headers['x-vmm-actor'] == 'cluster-test-operator'
        assert 'cookie' not in req.headers and 'x-csrf-token' not in req.headers and 'origin' not in req.headers
        if req.url.path.endswith('/output'):
            assert req.url.query == b'after=4'
            return httpx.Response(200, content='event: output\ndata: {"entries":[]}\n\nevent: done\ndata: {}\n\n', headers={'content-type': 'text/event-stream'})
        assert json.loads(req.content) == {'action': 'start'}
        return httpx.Response(200, json={'task_id': 'owned-by-second-host'})
    network(monkeypatch, remote)
    headers = signin(client, 'operator')
    prefix = '/api/cluster/hosts/' + host_id + '/proxy/'
    response = client.post(prefix + 'vms/incus:same-name/action', json={'action': 'start'}, headers=headers)
    assert response.json()['task_id'] == 'owned-by-second-host'
    assert seen[0].url.path == '/api/vms/incus:same-name/action'
    response = client.get(prefix + 'tasks/owned-by-second-host/output?after=4')
    assert response.status_code == 200 and 'event: done' in response.text
    assert response.headers['content-type'].startswith('text/event-stream')
    assert client.get(prefix + 'users').status_code == 403
    assert client.get(prefix + 'cluster/hosts').status_code == 403
    assert client.post(prefix + 'vms/incus:demo/action', json={'action': 'start'}).status_code == 403
    signin(client, 'viewer')
    assert client.post(prefix + 'vms/incus:demo/action', json={'action': 'start'}, headers=signin(client, 'viewer')).status_code == 403


def test_offline_host_does_not_hide_healthy_inventory(client, monkeypatch):
    host_id, _, _ = enroll(client, monkeypatch)
    def unavailable(req):
        raise httpx.ConnectError('offline', request=req)
    network(monkeypatch, unavailable)
    response = client.get('/api/cluster/inventory')
    assert response.status_code == 200
    hosts = response.json()['hosts']
    assert hosts[0]['status'] == 'online'
    assert hosts[1]['id'] == host_id and hosts[1]['status'] == 'offline'
    response = client.get('/api/cluster/hosts/' + host_id + '/proxy/inventory')
    assert response.status_code == 502


def test_tls_and_traversal_validation():
    import ssl
    for url in ('http://host', 'https://u:p@host', 'https://host/api', 'https://host?foo=bar'):
        with pytest.raises(ValueError):
            HostCreate(name='Test', url=url, token='T'*48)
    with pytest.raises(ValueError):
        HostCreate(name='Test', url='https://host', token='T'*48, certificate='invalid')
    for path in ('users', 'cluster/hosts', 'vms/../users', 'vms/%2e%2e/users', 'vms//demo', 'vms/\\demo'):
        assert not allowed_path(path)
    assert allowed_path('vms/incus:demo/snapshots/one/restore')
    context = main.cluster.tls({'certificate': ''})
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


def test_websocket_proxy_rejects_viewer_bad_origin_and_root_operator(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    host_id, _, _ = enroll(client, monkeypatch)
    for role, origin, mode in [('viewer', 'http://testserver', 'login'),
                               ('operator', 'https://evil.test', 'login'),
                               ('operator', 'http://testserver', 'root')]:
        headers = signin(client, role)
        path = f'/api/cluster/hosts/{host_id}/proxy/terminal/incus:demo?csrf={headers["X-CSRF-Token"]}&mode={mode}'
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path, headers={'origin': origin}):
                pass
        assert exc.value.code == 4403


def test_console_proxy_routes_frames_and_preserves_clean_logout(client, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from starlette.websockets import WebSocketDisconnect
    from app import cluster as federation
    host_id, _, body = enroll(client, monkeypatch)
    headers = signin(client, 'operator')
    seen = []

    class Remote:
        subprotocol = None
        close_code = None
        def __init__(self):
            self.frames = asyncio.Queue()
        def __aiter__(self):
            return self
        async def __anext__(self):
            frame = await self.frames.get()
            if frame is None:
                self.close_code = 1000
                raise StopAsyncIteration
            return frame
        async def send(self, frame):
            if frame == 'logout':
                await self.frames.put('{"type":"exit"}')
                await self.frames.put(None)
            else:
                await self.frames.put(frame)

    @asynccontextmanager
    async def connection(uri, **kwargs):
        seen.append((uri, kwargs))
        yield Remote()

    monkeypatch.setattr(federation, 'connect', connection)
    path = f'/api/cluster/hosts/{host_id}/proxy/terminal/incus:demo?csrf={headers["X-CSRF-Token"]}&mode=login'
    with client.websocket_connect(path, headers={'origin': 'http://testserver'}) as ws:
        ws.send_text('hello')
        assert ws.receive_text() == 'hello'
        ws.send_bytes(b'frame')
        assert ws.receive_bytes() == b'frame'
        ws.send_text('logout')
        assert ws.receive_json() == {'type': 'exit'}
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1000
    assert seen[0][0].startswith('wss://second.example:8443/api/terminal/incus:demo?')
    assert 'csrf=' + token_hash(body['token']) in seen[0][0]
    assert headers['X-CSRF-Token'] not in seen[0][0]
    assert seen[0][1]['origin'] == body['url']
    assert seen[0][1]['additional_headers']['X-VMM-Role'] == 'operator'


def test_forwarded_iso_upload_is_streamed_and_authorized_before_parsing(client, monkeypatch):
    host_id, _, _ = enroll(client, monkeypatch)
    seen = []
    def remote(req):
        seen.append(req)
        assert req.url.path == '/api/isos'
        assert req.headers['content-type'].startswith('multipart/form-data; boundary=')
        assert b'disposable.iso' in req.content and b'example-iso-content' in req.content
        return httpx.Response(200, json={'id': 'on-second-host'})
    network(monkeypatch, remote)
    path = f'/api/cluster/hosts/{host_id}/proxy/isos'
    headers = signin(client, 'operator')
    response = client.post(path, headers=headers, files={'file': ('disposable.iso', b'example-iso-content')})
    assert response.status_code == 200 and response.json()['id'] == 'on-second-host'
    assert len(seen) == 1
    headers = signin(client, 'viewer')
    assert client.post(path, headers=headers, content=b'invalid multipart').status_code == 403
    assert len(seen) == 1
    headers = signin(client)
    assert client.post(path, headers=dict(headers, **{'content-length': str(33 * 1024**3)})).status_code == 413
