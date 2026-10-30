import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault('VM_MANAGER_DATA', tempfile.mkdtemp(prefix='vmm-backup-tests-'))
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import backups, main, storage
from app.backup_libvirt import validate
from app.hypervisor import HypervisorError
from app.power import PowerInterrupted, PowerRequests
from app.security import Store, hash_password
import xml.etree.ElementTree as ET

BLOCK = 'a' * 32


@pytest.fixture
def environment(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state')
    backups.initialize(store)
    destination = tmp_path / 'backup'
    destination.mkdir()
    monkeypatch.setattr(backups, 'destination', lambda *args: destination)
    incus = SimpleNamespace(vm=lambda _: {'type': 'virtual-machine', 'status': 'Stopped'}, action=lambda *args: None)
    libvirt = SimpleNamespace(cli=lambda *args: 'shut off')
    manager = backups.Manager(store, tmp_path / 'state', incus, libvirt, PowerRequests(), lambda *args: None)
    return store, manager, destination


def add(store, vm='incus:demo', status='queued', trigger='manual', backup_id=None):
    import uuid
    bid = backup_id or uuid.uuid4().hex
    with store.db() as db:
        db.execute('INSERT INTO vm_backups(id,vm_id,block_id,status,created,trigger) VALUES(?,?,?,?,?,?)', (bid, vm, BLOCK, status, 1, trigger))
    return bid


def test_calendar_schedules_and_dst():
    def stamp(value):
        return datetime.fromisoformat(value).timestamp()
    plan = backups.Plan(block_id=BLOCK, enabled=True, time='01:30')
    # Fall clock change: do not run twice in the repeated hour.
    first = backups.next_run(plan, stamp('2026-10-25T00:00:00+00:00'))
    assert first == stamp('2026-10-25T00:30:00+00:00')
    assert backups.next_run(plan, first) == stamp('2026-10-26T01:30:00+00:00')
    assert backups.next_run(plan, stamp('2026-03-29T00:00:00+00:00')) == stamp('2026-03-29T01:30:00+00:00')
    weekly = plan.model_copy(update={'frequency': 'weekly', 'weekday': 0, 'timezone': 'UTC', 'time': '02:00'})
    assert backups.next_run(weekly, stamp('2026-10-27T00:00:00+00:00')) == stamp('2026-11-02T02:00:00+00:00')
    with pytest.raises(ValidationError):
        backups.Plan(block_id=BLOCK, timezone='../../etc/passwd')
    with pytest.raises(ValidationError):
        backups.Plan(block_id=BLOCK, time='25:60')


def test_backup_requires_explicit_purpose_and_mounted_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'broker', lambda *a: [{'id': BLOCK, 'purpose': 'vm', 'status': 'ready', 'path': str(tmp_path)}])
    with pytest.raises(HypervisorError, match='wrong purpose'):
        backups.destination(None, tmp_path, BLOCK)
    with pytest.raises(HypervisorError, match='Choose'):
        backups.destination(None, tmp_path, '')
    monkeypatch.setattr(storage, 'broker', lambda *a: [{'id': BLOCK, 'purpose': 'backup', 'status': 'ready', 'path': str(tmp_path)}])
    with pytest.raises(HypervisorError, match='not mounted'):
        backups.destination(None, tmp_path, BLOCK)


def test_completed_only_after_export_and_checksums(environment, monkeypatch):
    store, manager, destination = environment
    bid = add(store)
    def export(vm, path, manifest):
        (path / 'instance.tar.gz').write_bytes(b'guest disk contents')
    monkeypatch.setattr(manager, 'export_incus', export)
    manager.create(bid, False, None)
    row = manager.record(bid)
    assert row['status'] == 'completed'
    assert row['size'] == len(b'guest disk contents')
    path = backups.archive_dir(store, manager.data, row)
    assert manager.verify(path)['files']['instance.tar.gz']['sha256']
    (path / 'instance.tar.gz').write_bytes(b'corruption')
    with pytest.raises(HypervisorError, match='checksum'):
        manager.restore(bid, backups.Restore(name='restored', pool='pool'))


def test_running_guest_requires_opt_in_and_failed_exports_are_not_restorable(environment, monkeypatch):
    store, manager, _ = environment
    bid = add(store)
    monkeypatch.setattr(manager, 'status', lambda *args: 'running')
    with pytest.raises(HypervisorError, match='must be stopped'):
        manager.create(bid, False, None)
    assert manager.record(bid)['status'] == 'failed'
    with pytest.raises(HypervisorError, match='completed'):
        manager.restore(bid, backups.Restore(name='new', pool='pool'))


def test_restart_after_failed_backup_and_force_stop_cancels_restart(environment, monkeypatch):
    store, manager, _ = environment
    state = {'status': 'running', 'started': 0}
    monkeypatch.setattr(manager, 'status', lambda *args: state['status'])
    manager.power_action = lambda *args: state.update(status='stopped')
    manager.incus.action = lambda *args: state.update(started=state['started'] + 1)
    def fail(*args):
        raise OSError('Storage full')
    monkeypatch.setattr(manager, 'export_incus', fail)
    bid = add(store)
    with pytest.raises(OSError, match='Storage full'):
        manager.create(bid, True, None)
    assert state['started'] == 1
    state['status'] = 'running'
    bid = add(store)
    cancelled = manager.power_requests.register('incus:demo', 'backup')
    def emergency_stop(*args):
        manager.power_requests.register('incus:demo', 'stop')
    monkeypatch.setattr(manager, 'export_incus', emergency_stop)
    with pytest.raises(HypervisorError, match='Force stop'):
        manager.create(bid, True, cancelled)
    assert state['started'] == 1
    assert manager.record(bid)['status'] == 'interrupted'


def test_interrupted_records_recovered_and_retention_only_scheduled(environment, monkeypatch):
    store, manager, _ = environment
    bid = add(store)
    backups.initialize(store)
    assert manager.record(bid)['status'] == 'interrupted'
    manual = add(store, status='completed')
    older = add(store, status='completed', trigger='scheduled')
    newer = add(store, status='completed', trigger='scheduled')
    with store.db() as db:
        db.execute('UPDATE vm_backups SET created=2 WHERE id=?', (newer,))
    manager.retain('incus:demo', BLOCK, 1)
    assert {r['id'] for r in backups.catalog(store)} == {bid, manual, newer}


@pytest.mark.parametrize('restore_name', ['clone', 'c' * 63])
def test_restore_uses_independent_incus_volumes_and_clears_identity(environment, monkeypatch, tmp_path, restore_name):
    _, manager, _ = environment
    vm = {'devices': {'root': {'type': 'disk', 'path': '/', 'pool': 'old'}, 'data': {'type': 'disk', 'pool': 'old', 'source': 'original'}, 'eth0': {'type': 'nic', 'network': 'net', 'hwaddr': 'old'}, 'cdrom': {'type': 'disk', 'readonly': 'true', 'source': '/old.iso'}}, 'config': {'volatile.uuid': 'old', 'limits.cpu': '2', 'boot.autostart': 'true'}}
    manifest = {'config': vm, 'volumes': [{'device': 'data', 'file': 'volume-0.tar.gz'}], 'media': ['cdrom']}
    imported, updates = [], []
    manager.incus.vms = lambda: []
    def update(vm_name, transform):
        state = json.loads(json.dumps(vm))
        transform(state)
        updates.append(state)
    manager.incus.update = update
    monkeypatch.setattr(manager, 'cli', lambda *args: imported.append(args))
    monkeypatch.setattr(storage, 'check_vm_pool', lambda _: None)
    manager.restore_incus(tmp_path, manifest, backups.Restore(name=restore_name, pool='new'))
    assert set(updates[0]['devices']) == {'root'}
    restored = updates[-1]
    assert restored['devices']['data']['source'].startswith('vmm-' + restore_name + '-restore-')
    assert restored['devices']['data']['pool'] == 'new'
    assert restored['config']['boot.autostart'] == 'false'
    import uuid
    assert str(uuid.UUID(restored['config']['volatile.uuid'])) == restored['config']['volatile.uuid']
    assert restored['config']['volatile.uuid'] == restored['config']['volatile.uuid.generation']
    assert restored['config']['volatile.uuid'] != 'old'
    assert 'hwaddr' not in restored['devices']['eth0']
    assert 'cdrom' not in restored['devices']
    assert imported[1][:4] == ('storage', 'volume', 'import', 'new')


def test_libvirt_rejects_incomplete_backups():
    for xml in ('<domain><devices><hostdev/></devices></domain>', '<domain><devices><disk device="disk" type="block"><source dev="/dev/sda"/></disk></devices></domain>'):
        with pytest.raises(HypervisorError):
            validate(ET.fromstring(xml))


@pytest.fixture
def api_environment(environment, monkeypatch):
    store, manager, destination = environment
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'backup_manager', manager)
    monkeypatch.setattr(main, 'incus', manager.incus)
    with store.db() as db:
        for user, role in [('operator', 'operator'), ('viewer', 'viewer')]:
            db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,0)', (user, hash_password('test-password'), role))
    return TestClient(main.app), store, manager


def auth(client, user):
    return {'X-CSRF-Token': client.post('/api/login', json={'username': user, 'password': 'test-password'}).json()['csrf']}


def test_permissions_and_bulk_plans(api_environment):
    client, store, manager = api_environment
    viewer = auth(client, 'viewer')
    body = {'vm_ids': ['incus:one', 'incus:two'], 'block_id': BLOCK, 'enabled': True}
    assert client.get('/api/backups').status_code == 200
    assert client.put('/api/backups/plans', json=body, headers=viewer).status_code == 403
    assert client.post('/api/backups', json=body, headers=viewer).status_code == 403
    assert client.post('/api/backups/x/restore', json={'name': 'clone'}, headers=viewer).status_code == 403
    operator = auth(client, 'operator')
    assert client.put('/api/backups/plans', json=body, headers=operator).status_code == 200
    assert {p['vm_id'] for p in backups.plans(store)} == {'incus:one', 'incus:two'}
    assert client.delete('/api/backups/plans/incus:one', headers=operator).status_code == 200
    assert len(backups.plans(store)) == 1
    assert client.post('/api/backups', json={**body, 'vm_ids': ['incus:../../etc']}, headers=operator).status_code == 409


def test_bulk_queue_conflict_is_atomic_and_scheduler_does_not_duplicate(api_environment, monkeypatch):
    client, store, manager = api_environment
    headers = auth(client, 'operator')
    tasks = []
    def queue(user, action, resource, fn):
        tasks.append(fn)
        return {'task_id': str(len(tasks))}
    monkeypatch.setattr(main, 'task', queue)
    add(store, vm='incus:one')
    body = {'vm_ids': ['incus:one', 'incus:two'], 'block_id': BLOCK}
    assert client.post('/api/backups', json=body, headers=headers).status_code == 409
    assert not any(r['vm_id'] == 'incus:two' for r in backups.catalog(store))
    assert not tasks
    body.update(vm_ids=['incus:two'], enabled=True)
    assert client.put('/api/backups/plans', json=body, headers=headers).status_code == 200
    with store.db() as db:
        db.execute('UPDATE backup_plans SET next_run=0')
    main.backup_tick()
    main.backup_tick()
    assert len(tasks) == 1
    assert backups.plans(store)[0]['last_task'] == '1'
    assert backups.catalog(store)[-1]['status'] == 'queued'


def test_portable_incus_archive_removes_original_disk_and_network_dependencies():
    import io
    import tarfile
    import yaml
    root = {'path': '/', 'type': 'disk', 'pool': 'old'}
    vm = {'created_at': '2026-10-29T22:00:00Z', 'devices': {'root': root, 'data': {'type': 'disk', 'source': 'original'}, 'nic': {'type': 'nic', 'network': 'original'}}, 'profiles': ['original'], 'config': {'boot.autostart': 'true', 'volatile.uuid': 'original', 'limits.cpu': '2'}}
    source, target = io.BytesIO(), io.BytesIO()
    with tarfile.open(fileobj=source, mode='w') as archive:
        for filename, value in [('backup/index.yaml', {'config': {'container': vm}}), ('backup/virtual-machine/backup.yaml', {'container': vm})]:
            content = yaml.safe_dump(value).encode().replace(b"'2026-10-29T22:00:00Z'", b'2026-10-29T22:00:00Z')
            info = tarfile.TarInfo(filename)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        info = tarfile.TarInfo('backup/virtual-machine.img')
        info.size = 6
        archive.addfile(info, io.BytesIO(b'guest!'))
    source.seek(0)
    backups.portable_instance(source, target)
    target.seek(0)
    with tarfile.open(fileobj=target, mode='r:gz') as archive:
        doc = yaml.safe_load(archive.extractfile('backup/index.yaml').read())
        restored = doc['config']['container']
        assert restored['created_at'] == '2026-10-29T22:00:00Z'
        assert set(restored['devices']) == {'root'}
        assert restored['profiles'] == []
        assert restored['config'] == {'boot.autostart': 'false', 'limits.cpu': '2'}
        assert archive.extractfile('backup/virtual-machine.img').read() == b'guest!'


def test_settings_can_be_saved_during_long_export(api_environment, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    client, store, manager = api_environment
    headers = auth(client, 'operator')
    started, release = threading.Event(), threading.Event()
    def export(vm_name, path, manifest):
        started.set()
        assert release.wait(5)
        (path / 'instance.tar.gz').write_bytes(b'guest')
    monkeypatch.setattr(manager, 'export_incus', export)
    bid = add(store)
    with ThreadPoolExecutor(max_workers=2) as workers:
        job = workers.submit(manager.create, bid, False, None)
        assert started.wait(2)
        try:
            update = workers.submit(lambda: client.put('/api/backups/plans', json={'vm_ids': ['incus:demo'], 'block_id': BLOCK}, headers=headers))
            assert update.result(timeout=2).status_code == 200
        finally:
            release.set()
        job.result(timeout=2)


@pytest.mark.parametrize('legacy_shutdown', [False, True])
def test_live_policy_uses_immutable_export_and_records_consistency(environment, monkeypatch, legacy_shutdown):
    store, manager, _ = environment
    manager.incus.vm = lambda _: {'status': 'Running'}
    bid = add(store)
    def live_export(vm, path, manifest, cancelled):
        assert manager.status('incus', vm) == 'running'
        (path / 'instance.tar.gz').write_bytes(b'point in time root')
        (path / 'volume-0.tar.gz').write_bytes(b'point in time data')
    monkeypatch.setattr(manager, 'export_incus_live', live_export)
    def fail(*args):
        pytest.fail('Live capture must not request a shutdown or use mutable offline export')
    monkeypatch.setattr(manager, 'export_incus', fail)
    manager.power_action = fail
    manager.create(bid, legacy_shutdown, None, 'live')
    row = manager.record(bid)
    assert row['status'] == 'completed' and 'live crash-consistent' in row['message']
    manifest = manager.verify(backups.archive_dir(store, manager.data, row))
    assert manifest['consistency'] == 'crash-consistent'
    assert manifest['mode'] == 'live' and len(manifest['files']) == 2


def test_backup_plans_preserve_legacy_policy_and_accept_live(api_environment):
    client, store, manager = api_environment
    headers = auth(client, 'operator')
    body = {'vm_ids': ['incus:demo'], 'block_id': BLOCK, 'enabled': True}
    assert client.put('/api/backups/plans', json=body, headers=headers).status_code == 200
    assert backups.plans(store)[0]['mode'] is None
    body['mode'] = 'live'
    assert client.put('/api/backups/plans', json=body, headers=headers).status_code == 200
    assert backups.plans(store)[0]['mode'] == 'live'
    body['mode'] = 'invalid'
    assert client.put('/api/backups/plans', json=body, headers=headers).status_code == 422
