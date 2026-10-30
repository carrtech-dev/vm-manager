import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import external_storage as ext, storage_broker, guest_storage, backups
from app.hypervisor import HypervisorError


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(ext, 'REGISTRY', tmp_path / 'external.json')
    monkeypatch.setattr(ext, 'CONFIG', tmp_path / 'credentials')
    monkeypatch.setattr(ext, 'STAGING', tmp_path / 'external-staging')
    monkeypatch.setattr(ext, 'HOST_MOUNTS', tmp_path / 'mounts')
    return tmp_path


def smb(**extra):
    return dict(name='Shared documents', kind='smb', server='nas.local', share='documents', username='backup', password='not-a-real-password') | extra


def s3(**extra):
    return dict(name='Cloud backups', kind='s3', endpoint='https://s3.example.test', bucket='vm-backups', access_key='fake-key', secret_key='fake-secret') | extra


@pytest.mark.parametrize('body', [smb(server='host,guest'), smb(share='../etc'), smb(password='secret\nusername=root'),
    smb(prefix='../private'), smb(prefix='folder,,ro'), s3(endpoint='https://user:password@host'),
    s3(endpoint='https://host/?token=secret'), s3(bucket='../private'), s3(region='us-east-1\nfoo=bar')])
def test_sources_reject_path_and_configuration_injection(body):
    with pytest.raises(ValidationError):
        ext.Source(**body)


@pytest.mark.parametrize('path', ['/', '/etc/passwd', '/mnt', '/mnt/../etc', '/mnt//files', '/mnt/files/', '/mnt/files\nfoo', '/mnt/foo;bar'])
def test_guest_mount_paths_are_restricted(path):
    with pytest.raises(ValidationError):
        ext.Mount(source_id='a' * 32, path=path)


def test_registry_keeps_secrets_private_and_multiple_sources_survive(registry):
    first, second = ext.create(smb()), ext.create(s3())
    public = json.dumps(ext.listing())
    assert 'not-a-real-password' not in public
    assert 'fake-secret' not in public
    assert 'fake-key' not in public
    assert first['has_password'] and second['has_secret_key']
    assert ext.REGISTRY.stat().st_mode & 0o777 == 0o600
    assert len(ext.listing()['sources']) == 2
    with pytest.raises(HypervisorError, match='already exists'):
        ext.create(smb(name='SHARED DOCUMENTS'))
    ext.create(smb(password='', name='Renamed'), first['id'])
    assert ext.source_record(first['id'])['password'] == 'not-a-real-password'


def test_credential_files_and_utility_errors_do_not_expose_secrets(registry, monkeypatch):
    source = ext.source_record(ext.create(s3())['id'])
    path = ext.config(source)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    monkeypatch.setattr(ext.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stderr='fake-secret', stdout='fake-key'))
    with pytest.raises(HypervisorError) as error:
        ext.test(source['id'])
    assert 'fake-secret' not in str(error.value)
    assert 'fake-key' not in json.dumps(ext.listing())
    assert ext.listing()['sources'][0]['status'] == 'unavailable'


def test_shared_source_mounts_preserve_references_and_retry_ids(registry, monkeypatch):
    source = ext.create(smb())
    calls = []
    monkeypatch.setattr(ext, 'guest', lambda vm, body: calls.append((vm, body)) or 'Mounted')
    body = dict(source_id=source['id'], path='/mnt/shared')
    ext.attach('incus:one', body)
    ext.attach('libvirt:two', body)
    assert len(ext.listing()['mounts']) == 2
    with pytest.raises(HypervisorError, match='Detach'):
        ext.delete(source['id'], source['name'])
    with pytest.raises(HypervisorError, match='Detach'):
        ext.create(smb(password='new'), source['id'])
    with pytest.raises(HypervisorError, match='overlap'):
        ext.attach('incus:one', body | dict(path='/mnt/shared/nested'))
    mount_id = ext.listing()['mounts'][0]['id']
    ext.mount_operation('incus:one', mount_id, 'detach')
    assert len(ext.listing()['mounts']) == 1
    assert calls[0][1]['source']['password'] == 'not-a-real-password'
    assert calls[0][1]['runner']


def test_failed_setup_remains_recoverable_and_retry_keeps_identity(registry, monkeypatch):
    source = ext.create(s3(readonly=True))
    body = dict(source_id=source['id'], path='/mnt/cloud')
    def fail(*args):
        raise HypervisorError('Agent unavailable')
    monkeypatch.setattr(ext, 'guest', fail)
    with pytest.raises(HypervisorError, match='Agent'):
        ext.attach('incus:one', body)
    mount = ext.listing()['mounts'][0]
    assert mount['status'] == 'incomplete' and mount['readonly']
    monkeypatch.setattr(ext, 'guest', lambda *a: 'Mounted')
    ext.attach('incus:one', body)
    assert ext.listing()['mounts'][0]['id'] == mount['id']
    assert ext.listing()['mounts'][0]['status'] == 'mounted'


def test_guest_setup_does_not_block_inventory_or_lose_other_attachments(registry, monkeypatch):
    source = ext.create(smb())
    barrier = threading.Barrier(2)
    monkeypatch.setattr(ext, 'guest', lambda *a: barrier.wait(timeout=2) or 'Mounted')
    errors = []
    def mount(vm):
        try:
            ext.attach(vm, dict(source_id=source['id'], path='/mnt/shared'))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=mount, args=('incus:' + vm,)) for vm in ('one', 'two')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not errors
    assert {m['vm_id'] for m in ext.listing()['mounts']} == {'incus:one', 'incus:two'}


def test_incident_payload_uses_stdin_and_never_argv(registry, monkeypatch):
    vm = SimpleNamespace(vm=lambda _: dict(type='virtual-machine', status='Running'))
    monkeypatch.setattr(ext, 'Incus', lambda: vm)
    calls = []
    def execute(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='Mounted')
    monkeypatch.setattr(ext.subprocess, 'run', execute)
    ext.guest('incus:one', {'source': {'password': 'SECRET-CREDENTIAL'}})
    args, kwargs = calls[0]
    assert 'SECRET-CREDENTIAL' not in repr(args)
    assert 'SECRET-CREDENTIAL' in kwargs['input']


def test_external_backup_upload_is_verified_and_manifest_is_last(registry, monkeypatch):
    source = ext.create(s3())
    bid = 'b' * 32
    stage = ext.STAGING / source['id'] / 'vm-backups' / bid
    stage.mkdir(parents=True)
    (stage / 'disk').write_bytes(b'disk')
    (stage / 'manifest.json').write_text('{}')
    calls = []
    monkeypatch.setattr(ext, 'rclone', lambda source, *args: calls.append(args))
    ext.backup_transfer(source['id'], bid, 'push')
    assert [c[0] for c in calls] == ['copy', 'check', 'copyto', 'check']
    assert '--download' in calls[1] and '--download' in calls[-1]
    assert calls[2][2].endswith('/vm-manager-backups/' + bid + '/manifest.json')
    assert backups.external_destination(stage)
    assert not backups.external_destination(Path('/var/lib/vm-manager-storage/mounts/xyz/vm-backups/id'))
    (stage / 'symlink').symlink_to('/etc/passwd')
    with pytest.raises(HypervisorError, match='symbolic'):
        ext.backup_transfer(source['id'], bid, 'push')


def test_failed_upload_never_publishes_manifest(registry, monkeypatch):
    source = ext.create(s3())
    bid = 'b' * 32
    stage = ext.STAGING / source['id'] / 'vm-backups' / bid
    stage.mkdir(parents=True)
    (stage / 'manifest.json').write_text('{}')
    calls = []
    def transfer(source, *args):
        calls.append(args)
        if args[0] == 'check':
            raise HypervisorError('Verification failed')
    monkeypatch.setattr(ext, 'rclone', transfer)
    with pytest.raises(HypervisorError, match='Verification'):
        ext.backup_transfer(source['id'], bid, 'push')
    assert not any(c[0] == 'copyto' for c in calls)


def test_readonly_source_cannot_be_backup_destination(registry):
    source = ext.create(s3(readonly=True))
    with pytest.raises(HypervisorError, match='Read-only'):
        ext.backup_directory(source['id'])


def test_s3_detach_refuses_pending_or_unverifiable_uploads(monkeypatch):
    config = dict(source=dict(kind='s3'), rc_port=1234, rc_password='secret')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({'diskCache': {'uploadsQueued': 1}}).encode()
    monkeypatch.setattr(guest_storage.urllib.request, 'urlopen', lambda *a, **k: Response())
    with pytest.raises(RuntimeError, match='pending'):
        guest_storage.pending(config)
    def fail(*args, **kwargs): raise OSError('Connection refused')
    monkeypatch.setattr(guest_storage.urllib.request, 'urlopen', fail)
    with pytest.raises(RuntimeError, match='Cannot verify'):
        guest_storage.pending(config)


def test_s3_mount_reference_count_is_not_an_open_file(monkeypatch):
    # Rclone's inUse counts VFS mount references, not open files. A healthy
    # mounted VFS normally reports one and must still be detachable.
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"inUse":1,"diskCache":{"uploadsQueued":0,"uploadsInProgress":0,"erroredFiles":0}}'
    monkeypatch.setattr(guest_storage.urllib.request, 'urlopen', lambda *a, **k: Response())
    guest_storage.pending(dict(source=dict(kind='s3'), rc_port=1234, rc_password='secret'))


def test_broker_rejects_malformed_external_request_without_echoing_secret(registry):
    from app.storage_broker import dispatch
    with pytest.raises(ValueError):
        dispatch(dict(operation='external_create', body=s3(region='secret\ninjected')))
