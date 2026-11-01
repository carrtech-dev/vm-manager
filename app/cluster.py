"""Federated managers: explicit trust links, host-local resources and streamed proxies."""

import asyncio
import re
import secrets
import socket
import ssl
import time
import uuid
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .security import ROLES, token_hash

PROTOCOL = 1
ROOTS = {'inventory', 'vms', 'networks', 'isos', 'storage', 'collections',
         'organization', 'monitoring', 'operations', 'tasks', 'audit', 'backups', 'templates',
         'console', 'terminal', 'devops'}


def allowed_path(path):
    # Never permit credentials, trust administration, another proxy or URL traversal.
    return (bool(path) and path.split('/')[0] in ROOTS and
            not any(c in path for c in ('\\', '%', '?', '#')) and
            all(p not in ('', '.', '..') for p in path.split('/')))


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r'^[\w .-]+$')


class HostCreate(KeyCreate):
    url: str = Field(max_length=2048)
    token: str = Field(min_length=32, max_length=256, pattern=r'^[A-Za-z0-9_-]+$')
    certificate: str = Field(default='', max_length=32768)

    @field_validator('url')
    @classmethod
    def https_origin(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Use an HTTPS origin, for example https://192.168.8.236:8443')
        try:
            parsed.port
        except ValueError:
            raise ValueError('Invalid HTTPS port')
        return value.rstrip('/')

    @field_validator('certificate')
    @classmethod
    def valid_certificate(cls, value):
        if value:
            try:
                ssl.create_default_context(cadata=value)
            except (ssl.SSLError, ValueError):
                raise ValueError('Provide the host public PEM certificate or CA certificate')
        return value


class Cluster:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cluster_identity(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS cluster_keys(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    digest TEXT UNIQUE NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cluster_hosts(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    url TEXT UNIQUE NOT NULL, token TEXT NOT NULL, certificate TEXT NOT NULL,
                    node_id TEXT UNIQUE NOT NULL, created REAL NOT NULL);
            ''')
            db.execute('INSERT INTO cluster_identity SELECT ? WHERE NOT EXISTS '
                       '(SELECT 1 FROM cluster_identity)', (uuid.uuid4().hex,))
            self.node_id = db.execute('SELECT id FROM cluster_identity').fetchone()['id']

    def identity(self):
        return {'node_id': self.node_id, 'name': socket.gethostname(), 'protocol': PROTOCOL}

    def hosts(self, private=False):
        with self.store.db() as db:
            columns = '*' if private else 'id,name,url,node_id,created'
            return [dict(r) for r in db.execute(f'SELECT {columns} FROM cluster_hosts ORDER BY name,id')]

    def host(self, host_id):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM cluster_hosts WHERE id=?', (host_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Linked host not found')
        return dict(row)

    def peer_user(self, request):
        auth = request.headers.get('authorization', '')
        if not auth:
            return None
        if not auth.startswith('Bearer '):
            raise HTTPException(401, 'Invalid cluster credential')
        digest = token_hash(auth[7:])
        with self.store.db() as db:
            key = db.execute('SELECT id,name FROM cluster_keys WHERE digest=?', (digest,)).fetchone()
        if not key:
            raise HTTPException(401, 'Cluster credential expired or revoked')
        path = request.url.path.removeprefix('/api/')
        if path != 'cluster/identity' and not allowed_path(path):
            raise HTTPException(403, 'Cluster credentials cannot administer accounts or trust links')
        role = request.headers.get('x-vmm-role', '')
        actor = request.headers.get('x-vmm-actor', '')
        if role not in ROLES or not re.fullmatch(r'[A-Za-z0-9_.:@ -]{1,128}', actor):
            raise HTTPException(403, 'Invalid cluster delegation')
        return {'id': 0, 'username': f'cluster:{key["name"]}/{actor}', 'role': role,
                'csrf': digest, 'token': digest, 'peer': True}

    @staticmethod
    def headers(host, user):
        # Never forward browser cookies, CSRF credentials, Origin or arbitrary headers.
        return {'Authorization': 'Bearer ' + host['token'], 'X-VMM-Role': user['role'],
                'X-VMM-Actor': user['username']}

    @staticmethod
    def tls(host):
        return ssl.create_default_context(cadata=host['certificate'] or None)

    def client(self, host, timeout=10):
        return httpx.AsyncClient(verify=self.tls(host), trust_env=False,
                                 follow_redirects=False, timeout=httpx.Timeout(timeout, connect=4))

    async def get(self, host, path, user):
        try:
            async with self.client(host, 5) as client:
                response = await client.get(host['url'] + '/api/' + path, headers=self.headers(host, user))
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError, ssl.SSLError):
            raise HTTPException(502, 'Host unavailable or trust verification failed. Check its URL, certificate and link key.')


def install(app, cluster, session, require, inventory):
    @app.get('/api/cluster/identity')
    def identity(user=Depends(require())):
        return cluster.identity()

    @app.get('/api/cluster/hosts')
    def hosts(user=Depends(require())):
        return {'local': cluster.identity(), 'hosts': cluster.hosts()}

    @app.get('/api/cluster/keys')
    def keys(user=Depends(require('users'))):
        with cluster.store.db() as db:
            return [dict(r) for r in db.execute('SELECT id,name,created FROM cluster_keys ORDER BY created')]

    @app.post('/api/cluster/keys')
    def create_key(body: KeyCreate, user=Depends(require('users'))):
        token, key_id = secrets.token_urlsafe(48), uuid.uuid4().hex
        with cluster.store.db() as db:
            db.execute('INSERT INTO cluster_keys VALUES(?,?,?,?)',
                       (key_id, body.name, token_hash(token), time.time()))
            cluster.store.audit(user['username'], 'cluster.key.create', key_id, body.name, db=db)
        return {'id': key_id, 'token': token}

    @app.delete('/api/cluster/keys/{key_id}')
    def revoke_key(key_id: str, user=Depends(require('users'))):
        with cluster.store.db() as db:
            db.execute('DELETE FROM cluster_keys WHERE id=?', (key_id,))
            cluster.store.audit(user['username'], 'cluster.key.revoke', key_id, db=db)
        return {'ok': True}

    @app.post('/api/cluster/hosts')
    async def add_host(body: HostCreate, user=Depends(require('users'))):
        host = body.model_dump()
        peer = await cluster.get(host, 'cluster/identity', user)
        if (peer.get('protocol') != PROTOCOL or not re.fullmatch(r'[a-f0-9]{32}', peer.get('node_id', ''))):
            raise HTTPException(409, 'Incompatible VM Manager cluster protocol')
        if peer['node_id'] == cluster.node_id:
            raise HTTPException(409, 'This is the local manager; link a different host')
        host_id = uuid.uuid4().hex
        import sqlite3
        try:
            with cluster.store.db() as db:
                if db.execute('SELECT COUNT(*) FROM cluster_hosts').fetchone()[0] >= 32:
                    raise HTTPException(409, 'A manager supports up to 32 linked hosts')
                db.execute('INSERT INTO cluster_hosts VALUES(?,?,?,?,?,?,?)',
                           (host_id, body.name, body.url, body.token, body.certificate,
                            peer['node_id'], time.time()))
                cluster.store.audit(user['username'], 'cluster.host.link', host_id, body.name, db=db)
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'This host is already linked')
        return {'id': host_id, 'name': body.name, 'node_id': peer['node_id']}

    @app.delete('/api/cluster/hosts/{host_id}')
    def unlink_host(host_id: str, user=Depends(require('users'))):
        host = cluster.host(host_id)
        with cluster.store.db() as db:
            db.execute('DELETE FROM cluster_hosts WHERE id=?', (host_id,))
            cluster.store.audit(user['username'], 'cluster.host.unlink', host_id, host['name'], db=db)
        return {'ok': True}

    @app.get('/api/cluster/inventory')
    async def cluster_inventory(user=Depends(require())):
        semaphore = asyncio.Semaphore(4)

        async def collect(host):
            async with semaphore:
                try:
                    inv = await cluster.get(host, 'inventory', user)
                    return {'id': host['id'], 'name': host['name'], 'status': 'online',
                            'host': inv['host'], 'vms': inv['vms'], 'warnings': inv['warnings']}
                except (HTTPException, KeyError, TypeError):
                    return {'id': host['id'], 'name': host['name'], 'status': 'offline',
                            'vms': [], 'warnings': ['Host unavailable or trust verification failed']}

        local = await asyncio.to_thread(inventory)
        return {'hosts': [{'id': '', 'name': local['host']['name'], 'status': 'online',
                           'host': local['host'], 'vms': local['vms'], 'warnings': local['warnings']}]
                + list(await asyncio.gather(*(collect(h) for h in cluster.hosts(private=True))))}

    def authorize(request, path):
        if path.split('/')[0] == 'devops':
            from .features import require_devops
            require_devops()
        user = session(request)
        if not allowed_path(path):
            raise HTTPException(403, 'This resource cannot be forwarded')
        permission = 'users' if path.split('/')[0] == 'audit' else (
            'read' if request.scope['type'] == 'http' and request.method in ('GET', 'HEAD') else 'operate')
        if permission not in ROLES.get(user['role'], set()):
            raise HTTPException(403, 'Your role does not permit this action')
        return user

    @app.api_route('/api/cluster/hosts/{host_id}/proxy/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'HEAD'])
    async def proxy(host_id: str, path: str, request: Request):
        user = authorize(request, path)
        host = cluster.host(host_id)
        client = cluster.client(host, 120)
        headers = cluster.headers(host, user)
        for key in ('content-type', 'last-event-id'):
            if key in request.headers:
                headers[key] = request.headers[key]
        url = host['url'] + '/api/' + quote(path, safe='/:')
        if request.url.query:
            url += '?' + request.url.query
        try:
            remote_request = client.build_request(request.method, url, headers=headers,
                                                  content=request.stream())
            response = await client.send(remote_request, stream=True)
        except BaseException as exc:
            await client.aclose()
            if isinstance(exc, (httpx.HTTPError, ssl.SSLError)):
                raise HTTPException(502, 'Linked host unavailable; inspect its task history before retrying an operation')
            raise

        async def body():
            iterator = response.aiter_bytes().__aiter__()
            pending = None
            try:
                while True:
                    if pending is None:
                        pending = asyncio.create_task(anext(iterator))
                    done, _ = await asyncio.wait({pending}, timeout=20)
                    current = session(request)
                    cluster.host(host_id)
                    if current['role'] != user['role']:
                        break
                    if done:
                        try:
                            chunk = pending.result()
                        except StopAsyncIteration:
                            break
                        pending = None
                        yield chunk
            except (HTTPException, httpx.HTTPError):
                return
            finally:
                if pending:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await response.aclose()
                await client.aclose()

        return StreamingResponse(body(), status_code=response.status_code,
                                 headers={k: v for k, v in response.headers.items()
                                          if k in ('content-type', 'x-accel-buffering')})

    @app.websocket('/api/cluster/hosts/{host_id}/proxy/{path:path}')
    async def websocket_proxy(ws: WebSocket, host_id: str, path: str):
        workers = []
        close_code = 4403
        try:
            if path.split('/')[0] not in ('console', 'terminal') and not re.fullmatch(r'devops/agent/[0-9a-f]{32}/terminal', path):
                raise HTTPException(403, 'Only console and terminal streams are supported')
            user = authorize(ws, path)
            expected = ('https' if ws.url.scheme == 'wss' else 'http') + '://' + ws.headers.get('host', '')
            if (ws.headers.get('origin') != expected or
                    not secrets.compare_digest(ws.query_params.get('csrf', ''), user['csrf'])):
                raise HTTPException(403, 'Session validation failed')
            if ws.query_params.get('mode') == 'root' and user['role'] != 'administrator':
                raise HTTPException(403, 'Root terminal requires an administrator')
            host = cluster.host(host_id)
            query = [(k, v) for k, v in ws.query_params.multi_items() if k != 'csrf']
            query.append(('csrf', token_hash(host['token'])))
            uri = 'wss://' + urlsplit(host['url']).netloc + '/api/' + quote(path, safe='/:') + '?' + urlencode(query)
            protocols = ['binary'] if 'binary' in ws.scope.get('subprotocols', []) else None
            async with connect(uri, ssl=cluster.tls(host), additional_headers=cluster.headers(host, user),
                               origin=host['url'], subprotocols=protocols, proxy=None,
                               max_size=16 * 1024**2, open_timeout=5) as remote:
                await ws.accept(subprotocol=remote.subprotocol)

                async def upstream():
                    while True:
                        message = await ws.receive()
                        if message['type'] == 'websocket.disconnect':
                            return
                        current = session(ws)
                        authorize(ws, path)
                        if current['role'] != user['role']:
                            return
                        await remote.send(message.get('bytes') if message.get('bytes') is not None else message['text'])

                async def downstream():
                    async for message in remote:
                        await (ws.send_bytes(message) if isinstance(message, bytes) else ws.send_text(message))

                async def recheck():
                    while True:
                        await asyncio.sleep(20)
                        current = session(ws)
                        authorize(ws, path)
                        cluster.host(host_id)
                        if current['role'] != user['role']:
                            return

                workers = [asyncio.create_task(fn()) for fn in (upstream, downstream, recheck)]
                done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                if workers[1] in done and remote.close_code is not None:
                    # Preserve clean guest logout so the viewer can return to login.
                    close_code = remote.close_code if remote.close_code in (1000, 1001, 1011, 4400, 4403) else 1011
        except (HTTPException, OSError, WebSocketException, WebSocketDisconnect, ssl.SSLError):
            pass
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            try:
                await ws.close(code=close_code)
            except RuntimeError:
                pass
