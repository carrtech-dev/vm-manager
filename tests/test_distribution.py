"""Security and lifecycle checks for the package bootstrap."""
import importlib.util
import os
from pathlib import Path
import stat

import pytest
from app.security import Store, verify_password

ROOT = Path(__file__).resolve().parents[1]


def test_initial_credentials_private_before_write(tmp_path, monkeypatch):
    original = os.fdopen
    observed = []
    def inspect(descriptor, *args, **kwargs):
        observed.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return original(descriptor, *args, **kwargs)
    monkeypatch.setattr(os, 'fdopen', inspect)
    store = Store(tmp_path)
    previous = os.umask(0)
    try:
        store.bootstrap(tmp_path)
    finally:
        os.umask(previous)
    assert observed == [0o600]
    credentials = (tmp_path / 'initial-admin.txt').read_text()
    password = credentials.split('Password: ')[1].splitlines()[0]
    with store.db() as db:
        user = db.execute('SELECT * FROM users').fetchone()
        assert user['role'] == 'administrator'
        assert verify_password(password, user['password'])
    store.bootstrap(tmp_path)
    assert (tmp_path / 'initial-admin.txt').read_text() == credentials
    (tmp_path / 'initial-admin.txt').unlink()
    store.bootstrap(tmp_path)
    assert not (tmp_path / 'initial-admin.txt').exists()


def test_bootstrap_refuses_symlink_and_rolls_back(tmp_path):
    victim = tmp_path / 'other'
    victim.write_text('retain me')
    (tmp_path / 'initial-admin.txt').symlink_to(victim)
    store = Store(tmp_path)
    with pytest.raises(FileExistsError):
        store.bootstrap(tmp_path)
    assert victim.read_text() == 'retain me'
    with store.db() as db:
        assert not db.execute('SELECT 1 FROM users').fetchone()


def test_upgrade_backup_keeps_data_and_private_archive(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('distribution_maintain', ROOT / 'packaging/maintain.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / 'state'
    state.mkdir()
    store = Store(state)
    store.bootstrap(state)
    monkeypatch.setattr(module, 'STATE', state)
    monkeypatch.setattr(module, 'RUNTIME', tmp_path / 'runtime')
    monkeypatch.setattr(module, 'CONFIG', tmp_path / 'config')
    monkeypatch.setattr(module, 'systemd', lambda: False)
    module.pre_upgrade()
    archive = next((tmp_path / 'runtime/backups').glob('*.tar.gz'))
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
    assert (state / 'initial-admin.txt').exists()
    import tarfile
    with tarfile.open(archive) as contents:
        assert 'manager.sqlite3' in contents.getnames()
