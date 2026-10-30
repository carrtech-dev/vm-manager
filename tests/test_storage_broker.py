import json
from pathlib import Path

import pytest

from app import storage_broker as broker
from app.hypervisor import HypervisorError


@pytest.fixture
def registry(tmp_path, monkeypatch):
    block = dict(id="a" * 32, name="test", purpose="iso", parent="hdd-pool", size_gib=1,
                 dataset="tank/vmm-block-" + "a" * 32, path=str(tmp_path / "mount"), pool="", status="ready")
    Path(block["path"]).mkdir()
    path = tmp_path / "blocks.json"
    path.write_text(json.dumps([block]))
    monkeypatch.setattr(broker, "REGISTRY", path)
    monkeypatch.setattr(broker.os.path, "ismount", lambda p: True)
    calls = []
    def run(*args):
        calls.append(args)
        if args[:3] == ("zfs", "get", "-H"):
            return block["id"]
        if "used,available,quota" in args:
            return "100\n900\n1073741824"
        return str(20 * 1024**3)
    monkeypatch.setattr(broker, "run", run)
    return block, calls


def test_delete_requires_ownership_empty_and_mounted(registry, monkeypatch):
    block, calls = registry
    Path(block["path"], "keep.iso").write_text("data")
    with pytest.raises(HypervisorError, match="contains files"):
        broker.delete(block["id"])
    assert not any(c[:2] == ("zfs", "destroy") for c in calls)
    Path(block["path"], "keep.iso").unlink()
    monkeypatch.setattr(broker.os.path, "ismount", lambda p: False)
    with pytest.raises(HypervisorError, match="unmounted"):
        broker.delete(block["id"])
    monkeypatch.setattr(broker, "run", lambda *args: "another-owner")
    with pytest.raises(HypervisorError, match="ownership"):
        broker.delete(block["id"])


def test_grow_updates_hard_quota_and_reservation_and_rejects_shrink(registry):
    block, calls = registry
    with pytest.raises(HypervisorError, match="larger"):
        broker.grow(block["id"], dict(size_gib=1))
    broker.grow(block["id"], dict(size_gib=2))
    assert ("zfs", "set", "quota=2147483648", "reservation=2147483648", block["dataset"]) in calls
    assert broker.read_registry()[0]["size_gib"] == 2


def test_vm_pool_with_volumes_is_retained(registry, monkeypatch):
    block, calls = registry
    block.update(purpose="vm", pool="vmm-block-" + block["id"], path="")
    broker.save_registry([block])
    monkeypatch.setattr(broker.incus, "get", lambda path: [{"name": "disk"}])
    with pytest.raises(HypervisorError, match="contains volumes"):
        broker.delete(block["id"])
    assert len(broker.read_registry()) == 1


def test_no_arbitrary_commands_paths_or_parent_driver(registry, monkeypatch):
    monkeypatch.setattr(broker.incus, "pools", lambda: [{"name": "default", "driver": "btrfs"}])
    with pytest.raises(HypervisorError, match="ZFS"):
        broker.create(dict(name="new", parent="default", purpose="iso", size_gib=1))
    with pytest.raises(HypervisorError, match="Invalid"):
        broker.dispatch(dict(operation="delete", block_id="../../etc"))
    with pytest.raises(HypervisorError, match="Unsupported"):
        broker.dispatch(dict(operation="exec", command="anything"))


def test_live_export_enables_acl_support_only_on_owned_backup_block(registry, monkeypatch):
    from app import backup_libvirt
    block, calls = registry
    block['purpose'] = 'backup'
    broker.save_registry([block])
    previous = broker.run
    def run(*args, **kwargs):
        if 'acltype' in args:
            calls.append(args)
            return 'off'
        return previous(*args)
    monkeypatch.setattr(broker, 'run', run)
    exports = []
    monkeypatch.setattr(backup_libvirt, 'export', lambda *a, **k: exports.append((a, k)) or {})
    broker.dispatch(dict(operation='libvirt_export', block_id=block['id'], backup_id='b' * 32, vm_name='demo', live=True))
    assert ('zfs', 'set', 'acltype=posixacl', block['dataset']) in calls
    assert exports[0][1] == {'live': True}
