"""Destructive only to uniquely named disposable resources created by this check.
Run on the hypervisor with VM_MANAGER_DATA pointing to disposable test state.
"""

import asyncio
import io
import os
import time
import uuid
from fastapi.testclient import TestClient
from app.main import app, store, incus, libvirt, invalidate
from app.security import hash_password

suffix = uuid.uuid4().hex[:6]
network = "vmmtest-" + suffix
network2 = network + "b"
vm_name = "vmm-check-" + suffix
with store.db() as db:
    db.execute(
        "UPDATE users SET password=? WHERE username='admin'",
        (hash_password("Live-check-password-123!"),),
    )
client = TestClient(app)
r = client.post(
    "/api/login", json={"username": "admin", "password": "Live-check-password-123!"}
)
assert r.status_code == 200, r.text
headers = {"X-CSRF-Token": r.json()["csrf"]}


def request(method, path, payload=None, **kwargs):
    r = client.request(method, path, json=payload, headers=headers, **kwargs)
    assert r.status_code == 200, (path, r.status_code, r.text)
    data = r.json()
    if "task_id" in data:
        for _ in range(200):
            tasks = client.get("/api/tasks").json()
            task = next(t for t in tasks if t["id"] == data["task_id"])
            if task["status"] in ("completed", "failed", "interrupted"):
                assert task["status"] == "completed", (path, task)
                print(path, task["status"], flush=True)
                return task
            time.sleep(0.5)
        raise AssertionError("task did not finish")
    return data


try:
    inventory = request("GET", "/api/inventory")
    assert not inventory["warnings"], inventory["warnings"]
    assert any(v["provider"] == "libvirt" for v in inventory["vms"])
    request(
        "POST",
        "/api/networks",
        {
            "name": network,
            "cidr": "auto",
            "nat": True,
            "dhcp": True,
            "description": "Disposable VM Manager acceptance test",
        },
    )
    request(
        "POST",
        "/api/networks",
        {
            "name": network2,
            "cidr": "auto",
            "nat": False,
            "dhcp": True,
            "description": "Disposable isolated network",
        },
    )
    iso_data = bytearray(40000)
    iso_data[32769:32774] = b"CD001"
    uploaded = client.post(
        "/api/isos",
        files={"file": ("acceptance-" + suffix + ".iso", bytes(iso_data))},
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    iso_id = uploaded.json()["id"]
    request(
        "POST",
        "/api/vms",
        {
            "name": vm_name,
            "cpu": 1,
            "memory_gib": 2,
            "disk_gib": 4,
            "pool": "hdd-pool",
            "network": network,
            "iso_id": iso_id,
            "extra_disks": [1],
        },
    )
    request(
        "PUT",
        "/api/vms/incus:" + vm_name,
        {
            "cpu": 2,
            "memory_gib": 2,
            "description": "Acceptance hardware edit",
            "secure_boot": False,
            "autostart": False,
        },
    )
    request(
        "POST",
        "/api/vms/incus:" + vm_name + "/disks",
        {"pool": "hdd-pool", "size_gib": 1},
    )
    request("PUT", "/api/vms/incus:" + vm_name + "/disks/root", {"size_gib": 5})
    request("POST", "/api/vms/incus:" + vm_name + "/nics", {"network": network2})
    request(
        "POST",
        "/api/vms/incus:" + vm_name + "/nics",
        {"network": network, "device": "eth0"},
    )
    # Use an existing genuine OS installer to prove CD-ROM boot and console connection.
    invalidate()
    isos = request("GET", "/api/inventory")["isos"]
    actual_iso = next((i for i in isos if i["name"] == "win2022-ctops.iso"), None)
    if actual_iso:
        request(
            "PUT",
            "/api/vms/incus:" + vm_name + "/cdrom",
            {"iso_id": actual_iso["id"], "boot": True},
        )
    request("POST", "/api/vms/incus:" + vm_name + "/action", {"action": "start"})
    invalidate()
    vm = next(
        v for v in request("GET", "/api/inventory")["vms"] if v["name"] == vm_name
    )
    assert vm["status"] == "running", vm
    with client.websocket_connect(
        "/api/console/incus:" + vm_name + "?csrf=" + headers["X-CSRF-Token"],
        headers={"Origin": "http://testserver"},
    ) as ws:
        # SPICE protocol version 2.2 handshake header, server must respond with REDQ magic.
        import struct

        ws.send_bytes(
            struct.pack("<IIII", 0x51444552, 2, 2, 18)
            + struct.pack("<IBBII", 0, 1, 0, 0, 0)
            + struct.pack("<I", 18)
        )
        data = ws.receive_bytes()
        assert data[:4] == b"REDQ", data[:20]
        print("SPICE console handshake verified", flush=True)
    request(
        "POST",
        "/api/vms/incus:" + vm_name + "/action",
        {"action": "stop", "confirm_name": vm_name},
    )
    request(
        "PUT", "/api/vms/incus:" + vm_name + "/cdrom", {"iso_id": "", "boot": False}
    )
    request("DELETE", "/api/vms/incus:" + vm_name, {"confirm_name": vm_name})
    request(
        "DELETE",
        "/api/isos/" + iso_id,
        {"confirm_name": "acceptance-" + suffix + ".iso"},
    )
    request("DELETE", "/api/networks/" + network, {"confirm_name": network})
    request("DELETE", "/api/networks/" + network2, {"confirm_name": network2})
    print("LIVE_ACCEPTANCE_PASSED", flush=True)
finally:
    # Only clean the uniquely named resources belonging to this run.
    try:
        vm = incus.vm(vm_name)
        if vm["status"].lower() != "stopped":
            incus.action(vm_name, "stop")
        incus.delete(vm_name)
    except Exception as exc:
        print("VM cleanup:", str(exc)[:160], flush=True)
    try:
        volumes = incus.get("/1.0/storage-pools/hdd-pool/volumes/custom?recursion=1")
        for volume in volumes:
            if volume["name"].startswith("vmm-" + vm_name + "-") and not volume.get(
                "used_by"
            ):
                incus.request(
                    "DELETE",
                    "/1.0/storage-pools/hdd-pool/volumes/custom/" + volume["name"],
                )
        for net in (network, network2):
            try:
                incus.request("DELETE", "/1.0/networks/" + net)
            except Exception:
                pass
    except Exception as exc:
        print("Network cleanup:", str(exc)[:160], flush=True)
