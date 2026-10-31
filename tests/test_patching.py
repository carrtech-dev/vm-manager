import json
import os
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault('VM_MANAGER_DATA', tempfile.mkdtemp(prefix='vmm-patch-tests-'))

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main, patching, guest_patch
from app.hypervisor import HypervisorError
from app.security import Store, hash_password

real_task = main.task


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path)
    jobs = []
    def submit(user, action, resource, fn, resources=None):
        jobs.append(SimpleNamespace(user=user, action=action, resource=resource, fn=fn, resources=resources))
        return {'task_id': str(len(jobs))}
    manager = patching.Manager(store, submit)
    calls = []
    def guest(vm, action, run_id=None):
        calls.append((vm, action, run_id))
        return {'ready': True, 'os': 'Ubuntu 24.04', 'manager': 'apt-get'} if action == 'probe' else {
            'status': 'completed', 'message': 'Updated 2 packages', 'changed_count': 2,
            'packages': ['curl', 'openssl'], 'reboot_required': False}
    monkeypatch.setattr(patching, 'guest', guest)
    return SimpleNamespace(store=store, manager=manager, jobs=jobs, calls=calls)


def rows(env):
    with env.store.db() as db:
        return [dict(r) for r in db.execute('SELECT * FROM patch_runs ORDER BY rowid')]


def test_batch_fifo_concurrency_stop_and_atomic_duplicate_guard(env):
    spec = patching.Request(vm_ids=['incus:one', 'libvirt:two'], concurrency=1)
    env.manager.create(spec, 'operator')
    with pytest.raises(HypervisorError, match='already has an active'):
        env.manager.create(patching.Request(vm_ids=['incus:new', 'incus:one']), 'operator')
    assert len(rows(env)) == 2  # Entire conflicting selection rolled back.
    env.manager.tick()
    assert len(env.jobs) == 1 and env.jobs[0].resources == {'incus:one'}
    env.manager.tick()
    assert len(env.jobs) == 1
    env.jobs[0].fn()
    env.manager.tick()
    assert len(env.jobs) == 2 and env.jobs[1].resources == {'libvirt:two'}
    env.manager.finish(rows(env)[1]['id'], 'failed', {'message': 'Repository unavailable'})
    env.manager.create(patching.Request(vm_ids=['incus:three', 'incus:four']), 'operator')
    env.manager.tick()
    env.manager.finish(rows(env)[2]['id'], 'failed', {'message': 'Failed'})
    env.manager.tick()
    assert rows(env)[3]['status'] == 'skipped'


def test_continue_after_failure_and_parallel_limit(env):
    env.manager.create(patching.Request(vm_ids=['incus:one', 'incus:two', 'incus:three'], concurrency=2, stop_on_failure=False), 'operator')
    env.manager.tick()
    assert len(env.jobs) == 2
    env.manager.finish(rows(env)[0]['id'], 'failed', {'message': 'Failed'})
    env.manager.tick()
    assert len(env.jobs) == 3


def test_recovery_reattaches_existing_transaction_and_does_not_launch_twice(env, monkeypatch):
    env.manager.create(patching.Request(vm_ids=['incus:one']), 'operator')
    env.manager.tick()
    rid = rows(env)[0]['id']
    with env.store.db() as db:
        db.execute('UPDATE patch_runs SET attempted=1 WHERE id=?', (rid,))
    patching.initialize(env.store)
    env.manager.tick()
    env.jobs[-1].fn()
    assert env.calls == [('incus:one', 'status', rid)]
    assert rows(env)[0]['status'] == 'completed'


def test_uncertain_disconnect_blocks_new_runs_and_retries_only_status(env, monkeypatch):
    def guest(vm, action, rid=None):
        env.calls.append(action)
        if action == 'probe':
            return {'ready': True}
        raise HypervisorError('Connection lost')
    monkeypatch.setattr(patching, 'guest', guest)
    env.manager.create(patching.Request(vm_ids=['incus:one']), 'operator')
    env.manager.tick()
    with pytest.raises(HypervisorError):
        env.jobs[0].fn()
    assert rows(env)[0]['status'] == 'reconnecting'
    with pytest.raises(HypervisorError):
        env.manager.create(patching.Request(vm_ids=['incus:one']), 'operator')
    env.manager.tick(time.time() + 61)
    with pytest.raises(HypervisorError):
        env.jobs[-1].fn()
    assert env.calls == ['probe', 'launch', 'status']


def test_missing_recovery_record_does_not_repeat_upgrade(env, monkeypatch):
    env.manager.create(patching.Request(vm_ids=['incus:one']), 'operator')
    with env.store.db() as db:
        db.execute("UPDATE patch_runs SET attempted=1,status='reconnecting'")
    monkeypatch.setattr(patching, 'guest', lambda *a: {'status': 'missing'})
    env.manager.tick()
    with pytest.raises(HypervisorError):
        env.jobs[-1].fn()
    assert rows(env)[0]['status'] == 'interrupted'


def test_stopped_unavailable_and_expired_window_never_launch(env, monkeypatch):
    monkeypatch.setattr(patching, 'guest', lambda *a: {'ready': False, 'message': 'VM stopped'})
    env.manager.create(patching.Request(vm_ids=['incus:one']), 'operator')
    env.manager.tick()
    assert 'Skipped' in env.jobs[-1].fn()
    assert rows(env)[0]['attempted'] == 0
    env.manager.create(patching.Request(vm_ids=['incus:two']), 'operator', deadline=time.time() - 1)
    env.manager.tick()
    assert rows(env)[1]['status'] == 'skipped'
    assert len(env.jobs) == 1


def test_schedule_slots_skip_missed_window_and_claim_once(env):
    env.manager.probe('incus:one')
    plan = patching.Schedule(name='Sunday Linux', vm_ids=['incus:one'], frequency='daily', timezone='Europe/London', time='02:00', window_minutes=60)
    sid = env.manager.save_schedule(plan, 'operator')['id']
    now = datetime(2026, 10, 31, 4, tzinfo=timezone.utc).timestamp()
    with env.store.db() as db:
        db.execute('UPDATE patch_schedules SET next_run=? WHERE id=?', (now - 7200, sid))
    env.manager.tick(now)
    assert rows(env) == []
    assert 'missed' in patching.catalog(env.store)['schedules'][0]['last_message']
    with env.store.db() as db:
        db.execute('UPDATE patch_schedules SET next_run=? WHERE id=?', (now - 60, sid))
    env.manager.tick(now)
    env.manager.tick(now)
    assert len(rows(env)) == 1 and len(env.jobs) == 1
    assert patching.catalog(env.store)['schedules'][0]['next_run'] > now


def test_dst_single_slot_and_schedule_validation():
    plan = patching.Schedule(name='Linux', vm_ids=['incus:one'], frequency='daily', time='01:30')
    first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()
    assert patching.backups.next_run(plan, first) == datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc).timestamp()
    for change in ({'timezone': '../bad'}, {'vm_ids': ['incus:../x']}, {'concurrency': 5}, {'reboot': 'always'}, {'time': '24:00'}, {'window_minutes': 0}):
        with pytest.raises(ValidationError):
            patching.Schedule.model_validate(dict(name='Linux', vm_ids=['incus:one'], **change)) if 'vm_ids' not in change else patching.Schedule(name='Linux', **change)


def test_reboot_verified_by_changed_boot_id_and_unknown_is_not_rebooted(env, monkeypatch):
    answers = iter([{'ready': True}, {'status': 'completed', 'reboot_required': True, 'boot_id': 'before'},
        {'status': 'completed', 'reboot_required': True, 'boot_id': 'before', 'boot_id_now': 'after', 'reboot_requested': True}])
    monkeypatch.setattr(patching, 'guest', lambda *a: next(answers))
    env.manager.create(patching.Request(vm_ids=['incus:one'], reboot='if-required'), 'operator')
    env.manager.tick()
    assert 'reboot verified' in env.jobs[-1].fn()
    result = json.loads(rows(env)[0]['result'])
    assert result['rebooted'] and not result['reboot_required']


def test_broker_rejects_arbitrary_commands_and_payloads(monkeypatch):
    for request in [dict(vm_id='incus:one', body={'action': 'shell'}), dict(vm_id='incus:one', body={'action': 'launch', 'run_id': '../x'}),
                    dict(vm_id='incus:one', body={'action': 'probe', 'source': 'malicious'}), dict(vm_id='libvirt:../x', body={'action': 'probe'})]:
        with pytest.raises((HypervisorError, ValidationError)):
            patching.dispatch(request)
    monkeypatch.setattr(patching, 'execute', lambda vm, script, body: body)
    assert 'Self-contained Linux patch RPC' in patching.dispatch(dict(vm_id='incus:one', body={'action': 'probe'}))['source']


def test_guest_launch_is_idempotent_and_reboot_intent_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(guest_patch, 'ROOT', tmp_path)
    monkeypatch.setattr(guest_patch, 'probe', lambda: {'ready': True, 'boot_id': 'before'})
    commands = []
    monkeypatch.setattr(guest_patch, 'command', lambda args: commands.append(args) or SimpleNamespace(returncode=0, stdout='active', stderr=''))
    # /proc is absent on macOS; replace only the boot-id read.
    original = guest_patch.Path.read_text
    monkeypatch.setattr(guest_patch.Path, 'read_text', lambda path, *a, **kw: 'before' if str(path) == '/proc/sys/kernel/random/boot_id' else original(path, *a, **kw))
    rid = 'a' * 32
    payload = {'action': 'launch', 'run_id': rid, 'source': '# trusted worker'}
    guest_patch.execute(payload)
    guest_patch.execute(payload)
    assert sum(c[0] == 'systemd-run' for c in commands) == 1
    state = tmp_path / rid / 'state.json'
    guest_patch.write(state, {'status': 'completed', 'boot_id': 'before'})
    guest_patch.execute(dict(action='reboot', run_id=rid))
    guest_patch.execute(dict(action='reboot', run_id=rid))
    assert sum('--on-active=5s' in c for c in commands) == 1
    assert json.loads(state.read_text())['reboot_requested']


@pytest.mark.parametrize('manager,failure', [('apt-get', False), ('apt-get', True), ('dnf', False)])
def test_real_guest_worker_selects_native_transaction_and_records_results(tmp_path, monkeypatch, manager, failure):
    monkeypatch.setattr(guest_patch, 'ROOT', tmp_path)
    monkeypatch.setattr(guest_patch, 'probe', lambda: {'ready': True, 'manager': manager, 'boot_id': 'boot'})
    versions = iter([{'curl': ['1']}, {'curl': ['2'], 'dependency': ['1']}])
    monkeypatch.setattr(guest_patch, 'packages', lambda _: next(versions))
    monkeypatch.setattr(guest_patch, 'reboot_required', lambda *a: True)
    commands = []
    def run(args, **kwargs):
        commands.append((args, kwargs))
        return SimpleNamespace(returncode=100 if failure else 0)
    monkeypatch.setattr(guest_patch.subprocess, 'run', run)
    rid = 'b' * 32
    (tmp_path / rid).mkdir()
    guest_patch.write(tmp_path / rid / 'state.json', {'status': 'starting', 'started': time.time()})
    guest_patch.worker(rid)
    result = json.loads((tmp_path / rid / 'state.json').read_text())
    if failure:
        assert result['status'] == 'failed' and len(commands) == 1
    else:
        assert result['status'] == 'completed' and result['changed_count'] == 2 and result['reboot_required']
        assert commands[-1][0][0] == manager
        if manager == 'apt-get':
            assert '--no-remove' in commands[-1][0]
            assert 'Dpkg::Options::=--force-confold' in commands[-1][0]
        else:
            assert '--allowerasing' not in commands[-1][0]
        assert 'timeout' not in commands[-1][1]  # Controller timeouts never kill dpkg/rpm.


def test_dnf_success_exit_with_failed_scriptlet_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(guest_patch, 'ROOT', tmp_path)
    monkeypatch.setattr(guest_patch, 'probe', lambda: {'manager': 'dnf'})
    monkeypatch.setattr(guest_patch, 'packages', lambda _: {})
    def run(args, **kwargs):
        kwargs['stdout'].write('warning: %post(pkg) scriptlet failed, exit status 127\nComplete!\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(guest_patch.subprocess, 'run', run)
    rid = 'c' * 32
    (tmp_path / rid).mkdir()
    guest_patch.write(tmp_path / rid / 'state.json', {'started': time.time()})
    guest_patch.worker(rid)
    result = json.loads((tmp_path / rid / 'state.json').read_text())
    assert result['status'] == 'failed' and 'scriptlet' in result['message']


def test_guest_completion_race_does_not_overwrite_final_result(tmp_path, monkeypatch):
    monkeypatch.setattr(guest_patch, 'ROOT', tmp_path)
    rid = 'd' * 32
    (tmp_path / rid).mkdir()
    state = tmp_path / rid / 'state.json'
    guest_patch.write(state, {'status': 'running', 'started': time.time() - 100})
    original = guest_patch.Path.read_text
    monkeypatch.setattr(guest_patch.Path, 'read_text', lambda path, *a, **kw: 'boot' if str(path) == '/proc/sys/kernel/random/boot_id' else original(path, *a, **kw))
    def command(args):
        guest_patch.write(state, {'status': 'completed', 'message': 'Updated 1 package'})
        return SimpleNamespace(stdout='inactive', returncode=3)
    monkeypatch.setattr(guest_patch, 'command', command)
    assert guest_patch.execute({'action': 'status', 'run_id': rid})['status'] == 'completed'


@pytest.fixture
def api_env(env, monkeypatch):
    monkeypatch.setattr(main, 'store', env.store)
    monkeypatch.setattr(main, 'patch_manager', env.manager)
    monkeypatch.setattr(main, 'task', env.manager.submit)
    monkeypatch.setattr(main, 'inventory', lambda: {'vms': [{'id': 'incus:one'}, {'id': 'libvirt:two'}]})
    with env.store.db() as db:
        for role in ('operator', 'viewer'):
            db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,0)', (role, hash_password('test-password'), role))
    env.client = TestClient(main.app)
    def login(role):
        return {'X-CSRF-Token': env.client.post('/api/login', json={'username': role, 'password': 'test-password'}).json()['csrf']}
    env.login = login
    return env


def test_api_permissions_csrf_selection_and_schedule_lifecycle(api_env):
    e = api_env
    root = '/api/operations/patching'
    assert e.client.get(root).status_code == 401
    viewer = e.login('viewer')
    assert e.client.get(root).status_code == 200
    for path in ('/check', '/runs', '/schedules', '/batches/b/cancel'):
        assert e.client.post(root + path, json={}, headers=viewer).status_code == 403
    headers = e.login('operator')
    assert e.client.post(root + '/check', json={'vm_ids': ['incus:one']}).status_code == 403
    assert e.client.post(root + '/runs', json={'vm_ids': ['incus:one']}, headers=headers).status_code == 409
    assert e.client.post(root + '/check', json={'vm_ids': ['incus:missing']}, headers=headers).status_code == 409
    assert e.client.post(root + '/check', json={'vm_ids': ['incus:one']}, headers=headers).status_code == 200
    e.jobs[-1].fn()
    plan = {'name': 'Linux maintenance', 'vm_ids': ['incus:one']}
    response = e.client.post(root + '/schedules', json=plan, headers=headers)
    assert response.status_code == 200, response.text
    sid = response.json()['id']
    assert e.client.put(root + '/schedules/' + sid, json=dict(plan, enabled=False), headers=headers).status_code == 200
    assert e.client.get(root).json()['schedules'][0]['next_run'] is None
    assert e.client.delete(root + '/schedules/' + sid, headers=headers).status_code == 200
    assert e.client.get(root).json()['schedules'] == []
    assert e.client.post(root + '/runs', json={'vm_ids': ['incus:one']}, headers=headers).status_code == 200
    assert e.client.post(root + '/runs', json={'vm_ids': ['incus:one']}, headers=headers).status_code == 409


def test_cancel_only_pending_runs(api_env):
    e = api_env
    bid = e.manager.create(patching.Request(vm_ids=['incus:one', 'libvirt:two']), 'operator')
    e.manager.tick()
    headers = e.login('operator')
    assert e.client.post('/api/operations/patching/batches/' + bid + '/cancel', json={}, headers=headers).status_code == 200
    assert [r['status'] for r in rows(e)] == ['dispatched', 'skipped']


def test_ordinary_vm_mutations_block_while_guest_transaction_reconnects(env, monkeypatch):
    monkeypatch.setattr(main, 'store', env.store)
    monkeypatch.setattr(main, 'executor', SimpleNamespace(submit=lambda fn, **kw: fn()))
    env.manager.create(patching.Request(vm_ids=['incus:one']), 'operator')
    with env.store.db() as db:
        db.execute("UPDATE patch_runs SET attempted=1,status='reconnecting'")
    mutations = []
    result = real_task({'username': 'operator'}, 'backup.create', 'incus:one', lambda: mutations.append('unsafe'), resources={'incus:one'})
    with env.store.db() as db:
        task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (result['task_id'],)).fetchone())
    assert task['status'] == 'failed' and 'reconnecting' in task['message']
    assert mutations == []
