"""Acceptance test against a uniquely named disposable libvirt VM definition."""

import os
import time
import uuid
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app, DATA, libvirt, incus, invalidate, store
from app.security import hash_password
from app.hypervisor import run

vm_name = "vmm-libcheck-" + uuid.uuid4().hex[:6]
root_disk = DATA / (vm_name + ".qcow2")
run("qemu-img", "create", "-f", "qcow2", str(root_disk), "1G")
xml = f'''<domain type="kvm"><name>{vm_name}</name><memory unit="MiB">512</memory><vcpu>1</vcpu><os><type arch="x86_64" machine="q35">hvm</type><boot dev="hd"/></os><devices><emulator>/usr/bin/qemu-system-x86_64</emulator><disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file="{root_disk}"/><target dev="vda" bus="virtio"/></disk><graphics type="vnc" autoport="yes" listen="127.0.0.1"/></devices></domain>'''
path = DATA / (vm_name + ".xml")
path.write_text(xml)
with store.db() as db:
    db.execute(
        "UPDATE users SET password=? WHERE username='admin'",
        (hash_password("Live-check-password-123!"),),
    )
client = TestClient(app)
login = client.post(
    "/api/login", json={"username": "admin", "password": "Live-check-password-123!"}
).json()
headers = {"X-CSRF-Token": login["csrf"]}


def request(method, path, body):
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    tid = response.json()["task_id"]
    for _ in range(100):
        item = next(t for t in client.get("/api/tasks").json() if t["id"] == tid)
        if item["status"] in ("completed", "failed"):
            assert item["status"] == "completed", item
            print(path, item["status"], flush=True)
            return
        time.sleep(0.2)
    raise AssertionError("Timed out")


try:
    libvirt.cli("define", str(path), "--validate")
    request(
        "PUT",
        "/api/vms/libvirt:" + vm_name,
        {"cpu": 2, "memory_gib": 1, "description": "Disposable libvirt acceptance"},
    )
    request(
        "POST",
        "/api/vms/libvirt:" + vm_name + "/disks",
        {"pool": "hdd-pool", "size_gib": 1},
    )
    request("POST", "/api/vms/libvirt:" + vm_name + "/nics", {"network": "incusbr0"})
    invalidate()
    assert any(
        v["name"] == vm_name and v["cpu"] == "2"
        for v in client.get("/api/inventory").json()["vms"]
    )
    request("POST", "/api/vms/libvirt:" + vm_name + "/action", {"action": "start"})
    import struct

    with client.websocket_connect(
        "/api/console/libvirt:" + vm_name + "?csrf=" + headers["X-CSRF-Token"],
        headers={"Origin": "http://testserver"},
    ) as ws:
        data = ws.receive_bytes()
        assert data.startswith(b"RFB "), data
        print("VNC handshake verified", flush=True)
    request(
        "POST",
        "/api/vms/libvirt:" + vm_name + "/action",
        {"action": "stop", "confirm_name": vm_name},
    )
    request("DELETE", "/api/vms/libvirt:" + vm_name, {"confirm_name": vm_name})
    assert root_disk.exists(), "Existing libvirt root disk was unexpectedly deleted"
    print("LIBVIRT_ACCEPTANCE_PASSED", flush=True)
finally:
    try:
        libvirt.cli("destroy", vm_name)
    except Exception:
        pass
    try:
        libvirt.cli("undefine", vm_name, "--nvram")
    except Exception:
        pass
    for disk in (DATA / "disks").glob(vm_name + "-*"):
        try:
            disk.unlink()
        except PermissionError:
            print("Cleanup needs original disk owner:", disk, flush=True)
    try:
        root_disk.unlink(missing_ok=True)
    except PermissionError:
        print("Cleanup needs original disk owner:", root_disk, flush=True)
    path.unlink(missing_ok=True)
