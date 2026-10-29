import copy
import json
import xml.etree.ElementTree as ET

import pytest

from app.hypervisor import HypervisorError
from app.security import Store
from app.snapshots import Snapshots


class FakeIncus:
    def __init__(self):
        self.instance = {"type": "virtual-machine", "status": "Stopped",
                         "config": {"volatile.uuid": "identity"}, "devices": {
                             "root": {"type": "disk", "path": "/", "pool": "pool"},
                             "disk1": {"type": "disk", "pool": "pool", "source": "data"}}}
        self.volume = {"used_by": ["/1.0/instances/demo"], "content_type": "block", "config": {}}
        self.driver = "zfs"
        self.snapshots = {}
        self.changes = []
        self.fail = None

    def vm(self, vm_name):
        return copy.deepcopy(self.instance)

    def get(self, path):
        if path == "/1.0/storage-pools/pool":
            return {"driver": self.driver}
        if path == "/1.0/storage-pools/pool/volumes/custom/data":
            return copy.deepcopy(self.volume)
        if path.endswith("/snapshots?recursion=1"):
            prefix = path.split("?")[0] + "/"
            if "/virtual-machine/demo/" in prefix:
                prefix = "/1.0/instances/demo/snapshots/"
            return [value for key, value in self.snapshots.items() if key.startswith(prefix)]
        if path in self.snapshots:
            return self.snapshots[path]
        raise HypervisorError("Not found: " + path)

    def request(self, method, path, data=None):
        self.changes.append((method, path, data))
        if self.fail and self.fail(method, path):
            raise HypervisorError("Injected disk failure")
        if method == "POST":
            key = path + "/" + data["name"]
            self.snapshots[key] = {"name": data["name"], "created_at": f"2026-10-29T00:00:{len(self.snapshots):02d}Z"}
        elif method == "DELETE":
            del self.snapshots[path]


@pytest.fixture
def engine(tmp_path):
    return Snapshots(Store(tmp_path), FakeIncus(), None)


def create(engine, snapshot="one"):
    return engine.create("incus", "demo", "incus:demo", snapshot, "Before upgrade")


def test_snapshots_include_extra_disks_and_record_lineage(engine):
    create(engine)
    create(engine, "two")
    rows = engine.rows("incus:demo")
    assert [r["parent"] for r in rows] == [None, "one"]
    manifest = json.loads(rows[0]["manifest"])
    assert [d["device"] for d in manifest["disks"]] == ["root", "disk1"]
    assert len(engine.incus.snapshots) == 4
    assert engine.listing("incus", "demo", "incus:demo")["snapshots"][1]["current"]
    assert all(change[2]["expires_at"] == "0001-01-01T00:00:00Z" for change in engine.incus.changes)


def test_older_zfs_restore_and_parent_delete_are_rejected_before_mutation(engine):
    create(engine)
    create(engine, "two")
    engine.incus.changes.clear()
    with pytest.raises(HypervisorError, match="newer snapshots"):
        engine.restore("incus", "demo", "incus:demo", "one")
    with pytest.raises(HypervisorError, match="child snapshots"):
        engine.delete("incus", "demo", "incus:demo", "one")
    assert not engine.incus.changes
    engine.delete("incus", "demo", "incus:demo", "two")
    engine.restore("incus", "demo", "incus:demo", "one")
    restores = [c for c in engine.incus.changes if c[0] == "PUT"]
    assert len(restores) == 2
    assert restores[0][1].endswith("/custom/data")
    assert restores[1][2] == {"restore": "one", "stateful": False}


def test_btrfs_restore_can_branch_from_old_snapshot(engine):
    engine.incus.driver = "btrfs"
    create(engine)
    create(engine, "two")
    engine.restore("incus", "demo", "incus:demo", "one")
    create(engine, "branch")
    assert engine.row("incus:demo", "branch")["parent"] == "one"


@pytest.mark.parametrize("unsafe", ["running", "shared", "host-path", "full-copy"])
def test_unsupported_snapshot_fails_without_disk_mutations(engine, unsafe):
    if unsafe == "running":
        engine.incus.instance["status"] = "Running"
    elif unsafe == "shared":
        engine.incus.volume["used_by"].append("/1.0/instances/another")
    elif unsafe == "host-path":
        engine.incus.instance["devices"]["disk1"]["source"] = "/dev/sdb"
    else:
        engine.incus.driver = "dir"
    with pytest.raises(HypervisorError):
        create(engine)
    assert not engine.incus.changes
    assert not engine.rows("incus:demo")


def test_partial_create_survives_restart_and_can_be_deleted(engine):
    engine.incus.fail = lambda method, path: method == "POST" and "/instances/" in path
    with pytest.raises(HypervisorError, match="creation is incomplete"):
        create(engine)
    assert len(engine.incus.snapshots) == 1
    restarted = Snapshots(Store(engine.store.path.parent), engine.incus, None)
    assert restarted.row("incus:demo", "one")["status"] == "incomplete"
    with pytest.raises(HypervisorError, match="incomplete"):
        create(restarted, "two")
    engine.incus.fail = None
    restarted.delete("incus", "demo", "incus:demo", "one")
    assert not engine.incus.snapshots
    assert not restarted.rows("incus:demo")


def test_partial_restore_blocks_start_and_delete_until_retry(engine):
    create(engine)
    engine.incus.fail = lambda method, path: method == "PUT" and "/instances/" in path
    with pytest.raises(HypervisorError, match="restore is incomplete"):
        engine.restore("incus", "demo", "incus:demo", "one")
    with pytest.raises(HypervisorError, match="incomplete"):
        engine.guard_start("incus:demo")
    with pytest.raises(HypervisorError, match="incomplete restore"):
        engine.delete("incus", "demo", "incus:demo", "one")
    engine.incus.fail = None
    engine.restore("incus", "demo", "incus:demo", "one")
    engine.guard_start("incus:demo")


def test_restart_marks_inflight_snapshot_incomplete(engine):
    create(engine)
    engine.set_state("incus:demo", "one", "restoring", "restore")
    Store(engine.store.path.parent)
    row = engine.row("incus:demo", "one")
    assert row["status"] == "incomplete"
    assert "Service restarted" in row["error"]


def test_restore_preflight_checks_all_disks_and_current_attachments(engine):
    create(engine)
    engine.incus.changes.clear()
    engine.incus.instance["devices"]["disk1"]["source"] = "replacement"
    with pytest.raises(HypervisorError):
        engine.restore("incus", "demo", "incus:demo", "one")
    assert not engine.incus.changes
    engine.incus.instance["devices"]["disk1"]["source"] = "data"
    manifest = json.loads(engine.row("incus:demo", "one")["manifest"])
    del engine.incus.snapshots[manifest["volumes"][0]["url"] + "/snapshots/" + manifest["volume_snapshot"]]
    with pytest.raises(HypervisorError, match="Not found"):
        engine.restore("incus", "demo", "incus:demo", "one")
    assert not engine.incus.changes


def test_delete_retries_partial_disk_cleanup(engine):
    create(engine)
    engine.incus.fail = lambda method, path: method == "DELETE" and "/custom/" in path
    with pytest.raises(HypervisorError, match="deletion is incomplete"):
        engine.delete("incus", "demo", "incus:demo", "one")
    assert engine.row("incus:demo", "one")["status"] == "incomplete"
    engine.incus.fail = None
    engine.delete("incus", "demo", "incus:demo", "one")
    assert not engine.rows("incus:demo")
    assert not engine.incus.snapshots


def test_libvirt_plan_rejects_unsupported_state_and_shared_disks(engine):
    class Libvirt:
        xml_text = '<domain><uuid>id</uuid><devices><disk device="disk" type="file"><driver type="qcow2"/><source file="/disk.qcow2"/><target dev="vda"/></disk></devices></domain>'

        def xml(self, vm_name):
            return ET.fromstring(self.xml_text)

        def cli(self, *args):
            return "demo"

    engine.libvirt = Libvirt()
    assert engine.plan("libvirt", "demo")["disks"] == [{"device": "vda", "source": "/disk.qcow2"}]
    engine.libvirt.xml_text = engine.libvirt.xml_text.replace("qcow2", "raw")
    with pytest.raises(HypervisorError, match="qcow2"):
        engine.plan("libvirt", "demo")
    engine.libvirt.xml_text = '<domain><os><nvram>/vars.fd</nvram></os><devices/></domain>'
    with pytest.raises(HypervisorError, match="NVRAM"):
        engine.plan("libvirt", "demo")
