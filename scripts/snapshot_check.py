"""Opt-in snapshot acceptance against the deployed service; run as root on host.

Creates disposable Incus/ZFS and libvirt/qcow2 VMs. Writes known disk markers,
verifies incremental storage, restores both root/data disks and configuration,
checks chain guards and roles, and removes only its own resources and account.
"""
import json
import grp
import os
from pathlib import Path
import pwd
import secrets
import sqlite3
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import httpx

from app.hypervisor import Incus, Libvirt
from app.security import hash_password, token_hash


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    prefix = "vmm-snapshot-check-" + uuid.uuid4().hex[:10]
    incus, libvirt = Incus(), Libvirt()
    db = sqlite3.connect("/var/lib/vm-manager/manager.sqlite3", timeout=30)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db:
        user_id = db.execute("INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)",
                             (prefix, hash_password(secrets.token_urlsafe(32)), "operator", time.time())).lastrowid
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), user_id, csrf, time.time() + 1800))
    client = httpx.Client(base_url="https://192.168.8.235:8443", verify=False, timeout=200,
                         cookies={"vmm_session": token}, headers={"X-CSRF-Token": csrf})
    vm_ids, own_tasks = [], []
    scratch = Path(tempfile.mkdtemp(prefix=prefix + "-", dir="/var/lib/vm-manager"))
    scratch.chmod(0o755)

    def request(method, path, body=None):
        response = client.request(method, path, json=body)
        assert not response.is_error, (method, path, response.status_code, response.text)
        return response.json()

    def task(method, path, body, expected="completed"):
        task_id = request(method, path, body)["task_id"]
        own_tasks.append(task_id)
        deadline = time.monotonic() + 200
        while time.monotonic() < deadline:
            row = next(t for t in request("GET", "/api/tasks") if t["id"] == task_id)
            if row["status"] in ("completed", "failed", "interrupted"):
                assert row["status"] == expected, row
                return row
            time.sleep(.15)
        raise AssertionError("Task timed out: " + task_id)

    def exercise(vm_id, write, verify, configure, config, incremental):
        vm_name = vm_id.split(":", 1)[1]
        path = "/api/vms/" + vm_id
        snap = path + "/snapshots"
        data = request("GET", snap)
        assert data["supported"], data
        assert len(data["disks"]) == 2, data
        write(0x11)
        configure("first")
        task("POST", snap, dict(name="first", description="First known disk state"))
        write(0x22)
        configure("second")
        task("POST", snap, dict(name="second", description="Second known disk state"))
        snapshots = request("GET", snap)["snapshots"]
        assert [(s["name"], s["parent"]) for s in snapshots] == [("first", None), ("second", "first")], snapshots
        assert snapshots[1]["current"] and len(snapshots[1]["disks"]) == 2
        incremental()
        # Wrong confirmations must not enqueue a task.
        assert client.post(snap + "/second/restore", json=dict(confirm_name="wrong")).status_code == 400
        assert client.request("DELETE", snap + "/second", json=dict(confirm_name="wrong")).status_code == 400
        row = task("DELETE", snap + "/first", dict(confirm_name="first"), "failed")
        assert "child snapshots" in row["message"], row
        row = task("DELETE", path, dict(confirm_name=vm_name), "failed")
        assert "snapshots first" in row["message"], row
        write(0x33)
        configure("unsaved")
        if vm_id.startswith("incus:"):
            row = task("POST", snap + "/first/restore", dict(confirm_name=vm_name), "failed")
            assert "newer snapshots" in row["message"], row
            verify(0x33)
        task("POST", snap + "/second/restore", dict(confirm_name=vm_name))
        verify(0x22)
        assert config() == "second", config()
        task("DELETE", snap + "/second", dict(confirm_name="second"))
        task("POST", snap + "/first/restore", dict(confirm_name=vm_name))
        verify(0x11)
        assert config() == "first", config()
        task("DELETE", snap + "/first", dict(confirm_name="first"))
        assert request("GET", snap)["snapshots"] == []
        print("Verified " + vm_id + ": disk contents, configuration, parent chain, incremental storage, restore and deletion", flush=True)

    try:
        inv = request("GET", "/api/inventory")
        pool = next(p["name"] for p in inv["pools"] if p["driver"] == "zfs" and not p["name"].startswith("vmm-block-"))
        network = next(n["name"] for n in inv["networks"] if n["provider"] == "incus")
        vm_name = prefix + "-incus"
        vm_id = "incus:" + vm_name
        vm_ids.append(vm_id)
        task("POST", "/api/vms", dict(name=vm_name, cpu=1, memory_gib=1, disk_gib=4,
             pool=pool, network=network, extra_disks=[1], secure_boot=False))
        datasets = run("zfs", "list", "-H", "-t", "volume", "-o", "name").splitlines()
        extra = "vmm-" + vm_name + "-disk1"
        disks = [next(d for d in datasets if d.rsplit("/", 1)[-1] == vm_name + ".block"),
                 next(d for d in datasets if d.rsplit("/", 1)[-1] in (extra, "default_" + extra))]

        def expose_zfs(dataset):
            # Incus hides stopped VM zvols. Expose only our disposable test
            # volumes to write/read markers without booting a guest filesystem.
            run("zfs", "set", "volmode=dev", dataset)
            run("udevadm", "settle", "--timeout=10")
            return Path("/dev/zvol/" + dataset)

        def write_zfs(pattern):
            for dataset in disks:
                with expose_zfs(dataset).open("r+b", buffering=0) as stream:
                    stream.seek(4 * 1024**2)
                    stream.write(bytes([pattern]) * 4096)
                    os.fsync(stream.fileno())

        def verify_zfs(pattern):
            for dataset in disks:
                with expose_zfs(dataset).open("rb", buffering=0) as stream:
                    stream.seek(4 * 1024**2)
                    assert stream.read(4096) == bytes([pattern]) * 4096, dataset

        def incremental_zfs():
            run("zpool", "sync", disks[0].split("/")[0])
            for dataset in disks:
                values = run("zfs", "list", "-H", "-p", "-t", "snapshot", "-o", "used", "-r", dataset).splitlines()
                assert len(values) == 2, (dataset, values)
                assert sum(int(v) for v in values) < 64 * 1024**2, values

        exercise(vm_id, write_zfs, verify_zfs,
                 lambda description: incus.update(vm_name, lambda vm: vm.update(description=description)),
                 lambda: incus.vm(vm_name)["description"], incremental_zfs)
        # Capture while a disposable blank VM is at its boot menu. Disk markers
        # survive live capture and restore; require-stopped remains available.
        task("POST", "/api/vms/" + vm_id + "/action", dict(action="start"))
        task("POST", "/api/vms/" + vm_id + "/snapshots", dict(name="running", mode="live"))
        assert incus.vm(vm_name)['status'].lower() == 'running'
        row = task("POST", "/api/vms/" + vm_id + "/snapshots", dict(name="blocked", mode="stopped"), "failed")
        assert "must be stopped" in row["message"], row
        task("POST", "/api/vms/" + vm_id + "/action", dict(action="stop", confirm_name=vm_name))
        write_zfs(0x44)
        task("POST", "/api/vms/" + vm_id + "/snapshots/running/restore", dict(confirm_name=vm_name))
        verify_zfs(0x11)
        task("DELETE", "/api/vms/" + vm_id + "/snapshots/running", dict(confirm_name="running"))
        task("DELETE", "/api/vms/" + vm_id, dict(confirm_name=vm_name))

        vm_name = prefix + "-libvirt"
        vm_id = "libvirt:" + vm_name
        vm_ids.append(vm_id)
        paths = [scratch / "root.qcow2", scratch / "data.qcow2"]
        for path in paths:
            run("qemu-img", "create", "-f", "qcow2", str(path), "32M")
            os.chown(path, pwd.getpwnam("libvirt-qemu").pw_uid, grp.getgrnam("libvirt").gr_gid)
            path.chmod(0o660)
        root = ET.fromstring(f'<domain type="kvm"><name>{vm_name}</name><memory unit="MiB">256</memory><vcpu>1</vcpu><os><type arch="x86_64">hvm</type></os><devices/></domain>')
        for path, target in zip(paths, ("vda", "vdb")):
            disk = ET.SubElement(root.find("devices"), "disk", type="file", device="disk")
            ET.SubElement(disk, "driver", name="qemu", type="qcow2")
            ET.SubElement(disk, "source", file=str(path))
            ET.SubElement(disk, "target", dev=target, bus="virtio")
        libvirt.define(root)

        def write_qcow(pattern):
            for path in paths:
                run("qemu-io", "-f", "qcow2", "-c", f"write -P {pattern} 1M 4k", str(path))

        def verify_qcow(pattern):
            for path in paths:
                run("qemu-io", "-f", "qcow2", "-c", f"read -P {pattern} 1M 4k", str(path))

        def configure_qcow(description):
            root = libvirt.xml(vm_name)
            node = root.find("description")
            if node is None:
                node = ET.SubElement(root, "description")
            node.text = description
            libvirt.define(root)

        def incremental_qcow():
            for path in paths:
                info = json.loads(run("qemu-img", "info", "--output=json", str(path)))
                assert len(info["snapshots"]) == 2, info
                assert info["actual-size"] < info["virtual-size"], info

        exercise(vm_id, write_qcow, verify_qcow, configure_qcow,
                 lambda: libvirt.xml(vm_name).findtext("description"), incremental_qcow)
        task('POST', '/api/vms/' + vm_id + '/action', dict(action='start'))
        task('POST', '/api/vms/' + vm_id + '/snapshots', dict(name='running', mode='live'))
        assert libvirt.cli('domstate', vm_name).strip() == 'running'
        task('POST', '/api/vms/' + vm_id + '/action', dict(action='stop', confirm_name=vm_name))
        write_qcow(0x44)
        task('POST', '/api/vms/' + vm_id + '/snapshots/running/restore', dict(confirm_name=vm_name))
        assert libvirt.cli('domstate', vm_name).strip() == 'shut off'
        verify_qcow(0x11)
        task('DELETE', '/api/vms/' + vm_id + '/snapshots/running', dict(confirm_name='running'))
        # Server-side role checks apply equally to existing VM snapshots.
        with db:
            db.execute("UPDATE users SET role='viewer' WHERE id=?", (user_id,))
        path = "/api/vms/" + vm_id + "/snapshots"
        assert client.get(path).status_code == 200
        assert client.post(path, json=dict(name="forbidden")).status_code == 403
        with db:
            db.execute("UPDATE users SET role='operator' WHERE id=?", (user_id,))
        print("Snapshot live acceptance passed", flush=True)
    finally:
        # Only disposable names constructed by this invocation are touched.
        for vm_id in reversed(vm_ids):
            provider, vm_name = vm_id.split(":", 1)
            try:
                if provider == "incus":
                    if any(v["name"] == vm_name for v in incus.vms()):
                        if incus.vm(vm_name)["status"].lower() != "stopped":
                            incus.action(vm_name, "stop")
                        incus.delete(vm_name)
                elif vm_name in libvirt.cli("list", "--all", "--name").splitlines():
                    if libvirt.cli('domstate', vm_name).strip() != 'shut off':
                        libvirt.action(vm_name, 'stop')
                    libvirt.cli("undefine", vm_name, "--snapshots-metadata")
            except Exception as exc:
                print("Cleanup needs inspection for " + vm_id + ": " + str(exc), flush=True)
                raise
        for path in scratch.iterdir():
            path.unlink()
        scratch.rmdir()
        with db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
            for vm_id in vm_ids:
                db.execute("DELETE FROM vm_snapshots WHERE vm_id=?", (vm_id,))
                db.execute("DELETE FROM snapshot_heads WHERE vm_id=?", (vm_id,))
                db.execute("DELETE FROM pending_hardware WHERE vm_id=?", (vm_id,))
            # Audit and task entries are retained as evidence of the checks.
        client.close()
        db.close()


if __name__ == "__main__":
    main()
