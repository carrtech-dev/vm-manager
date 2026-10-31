import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault('VM_MANAGER_DATA', tempfile.mkdtemp(prefix='vmm-template-tests-'))
import pytest
from fastapi.testclient import TestClient

from app import backups, main, templates
from app.hypervisor import HypervisorError
from app.power import PowerRequests
from app.security import Store, hash_password

BLOCK = 'b' * 32


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state')
    backups.initialize(store)
    dest = tmp_path / 'archives'
    dest.mkdir()
    monkeypatch.setattr(backups, 'destination', lambda *args: dest)
    incus = SimpleNamespace(vm=lambda _: {'type': 'virtual-machine', 'status': 'Stopped'},
                            delete=lambda _: None)
    manager = backups.Manager(store, tmp_path / 'state', incus,
                              SimpleNamespace(xml=lambda _: None), PowerRequests(), lambda *a: None)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'backup_manager', manager)
    monkeypatch.setattr(main, 'incus', incus)
    monkeypatch.setattr(main, 'libvirt', manager.libvirt)
    monkeypatch.setattr(main.snapshots, 'guard_start', lambda _: None)
    queued = []
    def task(user, action, resource, fn, resources=None):
        queued.append(dict(action=action, resource=resource, fn=fn, resources=resources))
        return {'task_id': str(len(queued))}
    monkeypatch.setattr(main, 'task', task)
    def export(vm, path, manifest):
        (path / 'instance.tar.gz').write_bytes(b'independent captured guest')
    monkeypatch.setattr(manager, 'export_incus', export)
    with store.db() as db:
        for role in ('viewer', 'operator'):
            db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,0)',
                       (role, hash_password('test-password'), role))
    client = TestClient(main.app)
    def login(role='operator'):
        return {'X-CSRF-Token': client.post('/api/login', json={
            'username': role, 'password': 'test-password'}).json()['csrf']}
    headers = login()
    return SimpleNamespace(store=store, manager=manager, client=client, headers=headers,
                           queued=queued, login=login, dest=dest)


def capture(env, **overrides):
    body = dict(vm_id='incus:source', name='Ubuntu base', description='Configured OS', block_id=BLOCK)
    return env.client.post('/api/templates', json=dict(body, **overrides), headers=env.headers)


def test_capture_reuse_survives_source_deletion_and_template_delete_retains_copies(env, monkeypatch):
    response = capture(env)
    assert response.status_code == 200, response.text
    tid = response.json()['template_id']
    assert env.queued[0]['resources'] == {'incus:source', 'template:' + tid}
    assert env.queued[0]['action'] == 'template.capture'
    env.queued[0]['fn']()
    row = templates.record(env.store, tid)
    assert row['name'] == 'Ubuntu base' and row['status'] == 'completed'
    assert backups.catalog(env.store) == []
    # Bulk cleanup removes ordinary backups, retaining this independent template.
    monkeypatch.setattr(main, 'ensure_stopped', lambda *a: None)
    monkeypatch.setattr(main.snapshots, 'delete_all', lambda *a: None)
    main.remove_vm_contents('incus:source', cleanup=True, preserve_backups=False, actor='operator')
    archive = backups.archive_dir(env.store, env.manager.data, row)
    before = (archive / 'instance.tar.gz').read_bytes()
    created = []
    monkeypatch.setattr(env.manager, 'restore_incus', lambda path, manifest, spec: created.append(spec.name))
    for vm_name in ('copy-one', 'copy-two'):
        response = env.client.post('/api/templates/' + tid + '/vms',
                                   json={'name': vm_name, 'pool': 'pool'}, headers=env.headers)
        assert response.status_code == 200
        job = env.queued[-1]
        assert job['resources'] == {'template:' + tid, 'incus:' + vm_name}
        assert 'stopped' in job['fn']()
        assert (archive / 'instance.tar.gz').read_bytes() == before
    assert created == ['copy-one', 'copy-two']
    # Shared backing records cannot be accessed through the backup API.
    for method, suffix, body in [('post', '/restore', {'name': 'bad'}),
                                 ('delete', '', {'confirm_name': 'source'})]:
        response = env.client.request(method, '/api/backups/' + tid + suffix,
                                      json=body, headers=env.headers)
        assert response.status_code == 409 and 'Templates' in response.text
    response = env.client.request('DELETE', '/api/templates/' + tid,
                                  json={'confirm_name': 'Ubuntu base'}, headers=env.headers)
    assert response.status_code == 200
    assert env.queued[-1]['resources'] == {'template:' + tid}
    env.queued[-1]['fn']()
    assert not archive.exists() and templates.catalog(env.store) == []
    assert created == ['copy-one', 'copy-two']


def test_permissions_csrf_and_validation(env):
    assert env.client.post('/api/templates', json={}).status_code == 403
    viewer = env.login('viewer')
    assert env.client.get('/api/templates').status_code == 200
    assert env.client.post('/api/templates', json={}, headers=viewer).status_code == 403
    assert env.client.post('/api/templates/no/vms', json={'name': 'new'}, headers=viewer).status_code == 403
    assert env.client.request('DELETE', '/api/templates/no', json={'confirm_name': 'x'}, headers=viewer).status_code == 403
    env.headers = env.login()
    for body in ({'name': '   '}, {'name': 'a\nb'}, {'mode': 'invalid'}, {'block_id': '../bad'}, {'vm_id': 'bad'}):
        assert capture(env, **body).status_code in (400, 422)
    assert env.queued == []


def test_capture_atomic_conflicts_and_incomplete_template_guards(env):
    tid = capture(env).json()['template_id']
    assert capture(env, name='Other name').status_code == 409
    assert env.client.post('/api/templates/' + tid + '/vms', json={'name': 'copy'}, headers=env.headers).status_code == 409
    assert env.client.request('DELETE', '/api/templates/' + tid, json={'confirm_name': 'Ubuntu base'}, headers=env.headers).status_code == 409
    env.queued[0]['fn']()
    # Name uniqueness is case insensitive and failed inserts leave no archive row.
    assert capture(env, name='ubuntu BASE', vm_id='incus:another').status_code == 409
    assert len(templates.catalog(env.store)) == 1
    with env.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM vm_backups').fetchone()[0] == 1
    assert env.client.request('DELETE', '/api/templates/' + tid, json={'confirm_name': 'source'}, headers=env.headers).status_code == 400
    for name in ('../bad', '1bad'):
        assert env.client.post('/api/templates/' + tid + '/vms', json={'name': name}, headers=env.headers).status_code == 422


def test_failed_and_interrupted_capture_remain_visible_and_deletable(env, monkeypatch):
    def fail(*a):
        raise OSError('No space')
    monkeypatch.setattr(env.manager, 'export_incus', fail)
    tid = capture(env).json()['template_id']
    with pytest.raises(OSError, match='No space'):
        env.queued[-1]['fn']()
    assert templates.record(env.store, tid)['status'] == 'failed'
    assert env.client.post('/api/templates/' + tid + '/vms', json={'name': 'copy'}, headers=env.headers).status_code == 409
    second = capture(env, name='Interrupted').json()['template_id']
    backups.initialize(env.store)
    assert templates.record(env.store, second)['status'] == 'interrupted'
    assert env.client.request('DELETE', '/api/templates/' + second, json={'confirm_name': 'Interrupted'}, headers=env.headers).status_code == 200
    env.queued[-1]['fn']()
    assert [r['id'] for r in templates.catalog(env.store)] == [tid]


def test_template_checksum_validation_and_libvirt_destination_queue(env, monkeypatch):
    tid = capture(env).json()['template_id']
    env.queued[-1]['fn']()
    archive = backups.archive_dir(env.store, env.manager.data, env.manager.record(tid))
    (archive / 'instance.tar.gz').write_bytes(b'corrupt')
    env.client.post('/api/templates/' + tid + '/vms', json={'name': 'copy'}, headers=env.headers)
    with pytest.raises(HypervisorError, match='checksum.*template is retained'):
        env.queued[-1]['fn']()
    with env.store.db() as db:
        db.execute("UPDATE vm_backups SET vm_id='libvirt:source' WHERE id=?", (tid,))
    response = env.client.post('/api/templates/' + tid + '/vms', json={'name': 'lv-copy'}, headers=env.headers)
    assert response.status_code == 200
    assert env.queued[-1]['resources'] == {'template:' + tid, 'libvirt:lv-copy'}
