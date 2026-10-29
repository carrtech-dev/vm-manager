"""Opt-in acceptance of deployed storage blocks; run as root on the host.

Uses uniquely named disposable resources and a temporary operator. Restores
previous defaults and removes only its own VM, ISO, files, blocks and account.
"""
import errno
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
import uuid

import httpx

from app.hypervisor import Incus
from app.security import hash_password, token_hash


def main():
    suffix = uuid.uuid4().hex[:10]
    prefix = "vmm-storage-check-" + suffix
    vm_name = prefix + "-vm"
    incus = Incus()
    db = sqlite3.connect("/var/lib/vm-manager/manager.sqlite3", timeout=30)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db:
        user_id = db.execute("INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)",
                             (prefix, hash_password(secrets.token_urlsafe(32)), "operator", time.time())).lastrowid
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), user_id, csrf, time.time() + 1800))
    client = httpx.Client(base_url="https://192.168.8.235:8443", verify=False, timeout=200,
                         cookies={"vmm_session": token}, headers={"X-CSRF-Token": csrf})
    blocks, own_tasks = [], []
    iso_id = None
    old_defaults = None

    def request(method, path, body=None, **kwargs):
        response = client.request(method, path, json=body, **kwargs)
        if response.is_error:
            raise AssertionError(f"{method} {path}: {response.status_code} {response.text}")
        return response.json()

    def task(method, path, body, expected="completed"):
        task_id = request(method, path, body)["task_id"]
        own_tasks.append(task_id)
        deadline = time.monotonic() + 190
        while time.monotonic() < deadline:
            row = next(t for t in request("GET", "/api/tasks") if t["id"] == task_id)
            if row["status"] in ("completed", "failed", "interrupted"):
                assert row["status"] == expected, row
                return row
            time.sleep(.2)
        raise AssertionError("Task timed out: " + task_id)

    try:
        inv = request("GET", "/api/inventory")
        assert not inv["storage_error"], inv["storage_error"]
        old_defaults = inv["storage_defaults"]
        parent = next(p["name"] for p in inv["pools"] if p["driver"] == "zfs" and not p["name"].startswith("vmm-block-"))
        network = next(n["name"] for n in inv["networks"] if n["provider"] == "incus")
        for purpose, capacity in (("vm", 8), ("iso", 1), ("backup", 1)):
            task("POST", "/api/storage/blocks", dict(name=prefix + "-" + purpose, parent=parent, purpose=purpose, size_gib=capacity))
            block = next(b for b in request("GET", "/api/inventory")["storage_blocks"] if b["name"] == prefix + "-" + purpose)
            blocks.append(block)
            assert block["status"] == "ready", block
            if purpose != "vm":
                assert os.path.ismount(block["path"]), "Storage mount is hidden from the host"
            values = subprocess.check_output(["zfs", "get", "-Hp", "-o", "value", "quota,reservation", block["dataset"]], text=True).splitlines()
            assert values == [str(capacity * 1024**3)] * 2, values
            request("PUT", "/api/storage/defaults/" + purpose, dict(block_id=block["id"]))
        vm_block, iso_block, backup_block = blocks
        # Write incompressible data: fallocate may succeed without allocating
        # physical blocks on ZFS, so it cannot establish quota enforcement.
        probe = Path(iso_block["path"]) / "quota-probe"
        try:
            try:
                with probe.open("wb", buffering=0) as stream:
                    chunk = secrets.token_bytes(4 * 1024**2)
                    for _ in range(320):
                        stream.write(chunk)
                    os.fsync(stream.fileno())
            except OSError as exc:
                assert exc.errno in (errno.ENOSPC, errno.EDQUOT), exc
            else:
                raise AssertionError("ZFS accepted writes beyond the block quota")
        finally:
            probe.unlink(missing_ok=True)
            # ZFS frees quota asynchronously. Wait for that transaction before
            # testing a new upload into the block we intentionally filled.
            subprocess.run(["zpool", "sync", iso_block["dataset"].split("/")[0]], check=True)
        content = b"\0" * 32768 + b"CD001" + b"\0" * 32768
        uploaded = request("POST", "/api/isos", files={"file": (prefix + ".iso", content)})
        iso_id = uploaded["id"]
        iso = next(i for i in request("GET", "/api/inventory")["isos"] if i["id"] == iso_id)
        assert Path(iso["path"]).parent == Path(iso_block["path"])
        assert Path(iso["path"]).read_bytes() == content
        response = client.put("/api/storage/defaults/iso", json=dict(block_id=vm_block["id"]))
        assert response.status_code == 400
        task("POST", "/api/vms", dict(name=vm_name, cpu=1, memory_gib=1, disk_gib=4,
              pool=vm_block["pool"], network=network, iso_id=iso_id, extra_disks=[1]))
        vm = incus.vm(vm_name)
        assert vm["devices"]["root"]["pool"] == vm_block["pool"]
        edit = task("PUT", "/api/vms/incus:" + vm_name, dict(cpu=2, memory_gib=1))
        backup = Path(backup_block["path"]) / (edit["id"] + ".json")
        assert json.loads(backup.read_text())["name"] == vm_name
        # Defaults, references, and nonempty storage all prevent deletion.
        row = task("DELETE", "/api/storage/blocks/" + iso_block["id"], dict(confirm_name=iso_block["name"]), "failed")
        assert "default" in row["message"]
        request("PUT", "/api/storage/defaults/vm", dict(block_id=""))
        row = task("DELETE", "/api/storage/blocks/" + vm_block["id"], dict(confirm_name=vm_block["name"]), "failed")
        assert "volumes" in row["message"]
        task("PUT", "/api/storage/blocks/" + vm_block["id"], dict(size_gib=9))
        grown = next(b for b in request("GET", "/api/inventory")["storage_blocks"] if b["id"] == vm_block["id"])
        assert grown["capacity"] == 9 * 1024**3
        print("PASS: real quotas/reservations, full-block rejection, VM and extra disk allocation, ISO routing, configuration backup routing, defaults, expansion and deletion guards.", flush=True)
    finally:
        try:
            try:
                if any(v["name"] == vm_name for v in incus.vms()):
                    task("DELETE", "/api/vms/incus:" + vm_name, dict(confirm_name=vm_name))
                if iso_id:
                    task("DELETE", "/api/isos/" + iso_id, dict(confirm_name=prefix + ".iso"))
            finally:
                if old_defaults is not None:
                    for purpose in ("vm", "iso", "backup"):
                        request("PUT", "/api/storage/defaults/" + purpose, dict(block_id=old_defaults.get(purpose, "")))
            for block in request("GET", "/api/inventory")["storage_blocks"]:
                if not block["name"].startswith(prefix + "-"):
                    continue
                if block["purpose"] == "backup":
                    for task_id in own_tasks:
                        (Path(block["path"]) / (task_id + ".json")).unlink(missing_ok=True)
                task("DELETE", "/api/storage/blocks/" + block["id"], dict(confirm_name=block["name"]))
            print("Removed disposable storage blocks, VM, ISO and test configuration backups; restored defaults.", flush=True)
        finally:
            client.close()
            with db:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                db.execute("DELETE FROM users WHERE id=?", (user_id,))
            db.close()


if __name__ == "__main__":
    main()
