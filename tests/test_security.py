import os
import tempfile
from pathlib import Path

os.environ["VM_MANAGER_DATA"] = tempfile.mkdtemp(prefix="vmm-tests-")
from fastapi.testclient import TestClient
from app.main import (
    app,
    store,
    incus,
    libvirt,
    inventory,
    invalidate,
    network_config,
    Network,
)
from app.security import hash_password, verify_password
from app.hypervisor import HypervisorError, name
import pytest


@pytest.fixture
def client(monkeypatch):
    with store.db() as db:
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM attempts")
        db.execute("DELETE FROM users")
        for username, role in [
            ("admin", "administrator"),
            ("operator", "operator"),
            ("viewer", "viewer"),
        ]:
            db.execute(
                "INSERT INTO users(username,password,role,created) VALUES(?,?,?,0)",
                (username, hash_password("Test-password-123!"), role),
            )
    monkeypatch.setattr(incus, "vms", lambda: [])
    monkeypatch.setattr(libvirt, "vms", lambda: [])
    monkeypatch.setattr(incus, "networks", lambda: [])
    monkeypatch.setattr(libvirt, "networks", lambda: [])
    monkeypatch.setattr(incus, "pools", lambda: [])
    monkeypatch.setattr(incus, "isos", lambda: [])
    invalidate()
    return TestClient(app)


def login(client, username="admin"):
    r = client.post(
        "/api/login", json={"username": username, "password": "Test-password-123!"}
    )
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf"]}


def test_password_hashes():
    hashed = hash_password("example-password")
    assert verify_password("example-password", hashed)
    assert not verify_password("wrong", hashed)
    assert hashed != hash_password("example-password")


def test_unauthenticated_inventory(client):
    assert client.get("/api/inventory").status_code == 401
    assert client.get("/api/users").status_code == 401


def test_csrf_and_origin(client):
    headers = login(client)
    assert client.post("/api/logout").status_code == 403
    assert (
        client.post(
            "/api/logout", headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_viewer_forbidden_to_mutate(client):
    headers = login(client, "viewer")
    assert client.get("/api/inventory").status_code == 200
    assert client.get("/api/users").status_code == 403
    assert (
        client.post("/api/networks", json={"name": "demo"}, headers=headers).status_code
        == 403
    )
    assert (
        client.post(
            "/api/users",
            json={
                "username": "new",
                "password": "longpassword123",
                "role": "administrator",
            },
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/vms/incus:demo", json={"cpu": 2, "memory_gib": 2}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/isos", files={"file": ("a.iso", b"bad")}, headers=headers
        ).status_code
        == 403
    )


def test_operator_cannot_administer_users(client):
    headers = login(client, "operator")
    assert client.get("/api/audit").status_code == 403
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/inventory").status_code == 200


def test_self_lockout_prevented(client):
    headers = login(client)
    user_id = client.get("/api/me").json()["id"]
    assert (
        client.put(
            f"/api/users/{user_id}",
            json={"role": "viewer", "enabled": True},
            headers=headers,
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/users/{user_id}",
            json={"role": "administrator", "enabled": False},
            headers=headers,
        ).status_code
        == 400
    )


def test_user_disable_revokes_sessions(client):
    admin = login(client)
    with store.db() as db:
        target = db.execute("SELECT id FROM users WHERE username='viewer'").fetchone()[
            0
        ]
    viewer = TestClient(app)
    login(viewer, "viewer")
    assert (
        client.put(
            f"/api/users/{target}",
            json={"role": "viewer", "enabled": False},
            headers=admin,
        ).status_code
        == 200
    )
    assert viewer.get("/api/me").status_code == 401


def test_upload_rejects_fake_iso_and_paths(client):
    headers = login(client)
    assert (
        client.post(
            "/api/isos", files={"file": ("bad.iso", b"not an ISO")}, headers=headers
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/isos", files={"file": ("payload.sh", b"#!/bin/sh")}, headers=headers
        ).status_code
        == 400
    )
    data = bytearray(40000)
    data[32769:32774] = b"CD001"
    r = client.post(
        "/api/isos", files={"file": ("../../valid.iso", bytes(data))}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["name"] == "valid.iso"
    assert (
        client.post(
            "/api/isos", files={"file": ("valid.iso", bytes(data))}, headers=headers
        ).status_code
        == 409
    )


def test_deletion_requires_exact_confirmation(client):
    headers = login(client)
    assert client.delete("/api/vms/incus:test", headers=headers).status_code == 422
    assert (
        client.request(
            "DELETE",
            "/api/vms/incus:test",
            json={"confirm_name": "wrong"},
            headers=headers,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/vms/incus:test/action", json={"action": "stop"}, headers=headers
        ).status_code
        == 400
    )
    assert (
        client.request(
            "DELETE",
            "/api/networks/test",
            json={"confirm_name": "wrong"},
            headers=headers,
        ).status_code
        == 400
    )


def test_invalid_vm_names_and_limits(client):
    headers = login(client)
    for bad in ["../etc", "vm; rm -rf /", "--help", "vm:remote"]:
        assert (
            client.post(
                "/api/vms",
                json={"name": bad, "pool": "default", "network": "incusbr0"},
                headers=headers,
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/api/vms",
            json={"name": "valid", "pool": "default", "network": "incusbr0", "cpu": 0},
            headers=headers,
        ).status_code
        == 422
    )


def test_hypervisor_failure_is_visible(client, monkeypatch):
    login(client)

    def fail():
        raise HypervisorError("connection refused")

    monkeypatch.setattr(incus, "vms", fail)
    invalidate()
    result = client.get("/api/inventory")
    assert result.status_code == 200
    assert any("Incus VMs: connection refused" in x for x in result.json()["warnings"])


def test_network_overlap_and_public_rejected(client, monkeypatch):
    monkeypatch.setattr("app.main.psutil.net_if_addrs", lambda: {})
    monkeypatch.setattr(
        incus,
        "networks",
        lambda: [
            {
                "id": "incus:existing",
                "provider": "incus",
                "config": {"ipv4.address": "10.200.1.1/24"},
            }
        ],
    )
    invalidate()
    assert network_config(Network(name="new"))["ipv4.address"] == "10.200.2.1/24"
    with pytest.raises(HypervisorError, match="overlaps"):
        network_config(Network(name="new", cidr="10.200.1.1/24"))
    for cidr in [
        "8.8.8.1/24",
        "127.0.0.1/24",
        "10.2.0.0/24",
        "10.2.0.255/24",
        "not-a-cidr",
    ]:
        with pytest.raises(HypervisorError):
            network_config(Network(name="new", cidr=cidr))


def test_console_requires_auth_and_origin(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/console/incus:demo"):
            pass
    login(client, "viewer")
    with pytest.raises(Exception):
        with client.websocket_connect("/api/console/incus:demo"):
            pass


def test_login_failures_are_rate_limited_and_audited(client):
    for _ in range(12):
        assert (
            client.post(
                "/api/login", json={"username": "admin", "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/login", json={"username": "admin", "password": "Test-password-123!"}
        ).status_code
        == 429
    )
    with store.db() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM audit WHERE action='login.failed'"
            ).fetchone()[0]
            >= 12
        )


def test_upload_rejected_before_body_parsing_when_unauthenticated(client):
    response = client.post(
        "/api/isos",
        content=b"bad multipart",
        headers={"Content-Type": "multipart/form-data; boundary=bad"},
    )
    assert response.status_code == 401


def test_oversize_request_rejected(client):
    assert (
        client.post(
            "/api/login", content=b"{}", headers={"Content-Length": str(2 * 1024**2)}
        ).status_code
        == 413
    )


def test_queued_job_reports_failure_and_audit(client, monkeypatch):
    import time

    headers = login(client)

    def fail(*args, **kwargs):
        raise HypervisorError("Intentional test error")

    monkeypatch.setattr(incus, "request", fail)
    result = client.post("/api/networks", json={"name": "jobtest"}, headers=headers)
    assert result.status_code == 200
    task_id = result.json()["task_id"]
    for _ in range(50):
        rows = client.get("/api/tasks").json()
        job = next(t for t in rows if t["id"] == task_id)
        if job["status"] == "failed":
            break
        time.sleep(0.01)
    assert job["status"] == "failed"
    assert job["message"] == "Intentional test error"
    assert any(
        a["action"] == "network.create" and "failed" in a["detail"]
        for a in client.get("/api/audit").json()
    )


def test_incus_async_wait_preserves_query_and_errors(monkeypatch):
    import httpx
    from app.hypervisor import Incus

    paths = []

    def handler(request):
        paths.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "type": "async",
                    "operation": "/1.0/operations/test?project=default",
                    "metadata": {},
                },
            )
        return httpx.Response(
            200, json={"type": "sync", "metadata": {"status_code": 200}}
        )

    monkeypatch.setattr(
        httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handler)
    )
    Incus().request("POST", "/1.0/instances", {})
    assert (
        paths[-1] == "http://incus/1.0/operations/test/wait?timeout=180&project=default"
    )


def test_incus_update_uses_optimistic_lock(monkeypatch):
    import httpx
    from app.hypervisor import Incus

    headers = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"etag": '"version1"'},
                json={
                    "type": "sync",
                    "metadata": {"config": {}, "devices": {}, "profiles": []},
                },
            )
        headers.append(request.headers.get("if-match"))
        return httpx.Response(200, json={"type": "sync", "metadata": {}})

    monkeypatch.setattr(
        httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handler)
    )
    Incus().update("testvm", lambda v: v["config"].update({"limits.cpu": "2"}))
    assert headers == ['"version1"']


def test_mounted_iso_boots_before_existing_high_priority_disk(client, monkeypatch):
    headers = login(client)
    vm = {
        "status": "Stopped",
        "config": {},
        "devices": {
            "root": {
                "type": "disk",
                "path": "/",
                "pool": "hdd-pool",
                "boot.priority": "20",
            }
        },
        "expanded_devices": {
            "root": {
                "type": "disk",
                "path": "/",
                "pool": "hdd-pool",
                "boot.priority": "20",
            }
        },
    }
    monkeypatch.setattr(incus, "vm", lambda _: vm)
    monkeypatch.setattr(
        "app.main.get_iso",
        lambda _: {"name": "install.iso", "path": "/tmp/install.iso"},
    )
    monkeypatch.setattr(incus, "update", lambda _, transform: transform(vm))
    monkeypatch.setattr(
        "app.main.task", lambda user, action, resource, fn: fn() or {"task_id": "test"}
    )
    result = client.put(
        "/api/vms/incus:testvm/cdrom",
        json={"iso_id": "iso", "boot": True},
        headers=headers,
    )
    assert result.status_code == 200
    assert int(vm["devices"]["cdrom"]["boot.priority"]) > 20
    assert vm["devices"]["root"]["boot.priority"] == "20"


@pytest.fixture
def hardware_vm(client, monkeypatch):
    import copy

    vm = {
        "status": "Running",
        "config": {"limits.cpu": "2", "limits.memory": "2GiB"},
        "devices": {
            "root": {"type": "disk", "path": "/", "pool": "hdd-pool", "size": "4GiB"}
        },
    }
    with store.db() as db:
        db.execute("DELETE FROM pending_hardware")
    monkeypatch.setattr(incus, "vm", lambda _: copy.deepcopy(vm))
    monkeypatch.setattr(incus, "update", lambda _, transform: transform(vm))
    monkeypatch.setattr(
        "app.main.task", lambda user, action, resource, fn: fn() or {"task_id": "test"}
    )
    return vm


def test_running_hardware_is_persisted_and_replaced(client, hardware_vm):
    from app.main import pending_hardware
    from app.security import Store

    headers = login(client)
    for cores in (3, 4):
        assert (
            client.put(
                "/api/vms/incus:demo",
                headers=headers,
                json={"cpu": cores, "memory_gib": 3},
            ).status_code
            == 200
        )
    assert hardware_vm["config"]["limits.cpu"] == "2"
    rows = pending_hardware("incus:demo")
    assert len(rows) == 1
    import json

    assert json.loads(rows[0]["body"])["cpu"] == 4
    # Opening the database again (service restart) preserves the pending plan.
    reopened = Store(store.path.parent)
    with reopened.db() as db:
        assert db.execute("SELECT count(*) FROM pending_hardware").fetchone()[0] == 1


def test_restart_applies_pending_only_after_clean_shutdown(
    client, hardware_vm, monkeypatch
):
    from app.main import pending_hardware

    headers = login(client)
    client.put("/api/vms/incus:demo", headers=headers, json={"cpu": 4, "memory_gib": 3})
    calls = []

    def action(_, action):
        calls.append((action, hardware_vm["config"]["limits.cpu"]))
        hardware_vm["status"] = "Stopped" if action == "shutdown" else "Running"

    monkeypatch.setattr(incus, "action", action)
    assert (
        client.post(
            "/api/vms/incus:demo/action", headers=headers, json={"action": "restart"}
        ).status_code
        == 200
    )
    assert calls == [("shutdown", "2"), ("start", "4")]
    assert hardware_vm["config"]["limits.memory"] == str(3 * 1024**3)
    assert not pending_hardware("incus:demo")


def test_failed_pending_change_keeps_remaining_and_does_not_start(
    client, hardware_vm, monkeypatch
):
    from app.main import pending_hardware, power_action

    headers = login(client)
    client.put("/api/vms/incus:demo", headers=headers, json={"cpu": 4, "memory_gib": 3})
    hardware_vm["status"] = "Stopped"
    calls = []
    monkeypatch.setattr(incus, "action", lambda *args: calls.append(args))

    def fail(*args):
        raise HypervisorError("No capacity")

    monkeypatch.setattr(incus, "update", fail)
    with pytest.raises(HypervisorError, match="No capacity"):
        power_action("incus:demo", "start")
    assert not calls
    assert len(pending_hardware("incus:demo")) == 1
    assert (
        client.request(
            "DELETE", "/api/vms/incus:demo/pending", headers=headers
        ).status_code
        == 200
    )
    assert not pending_hardware("incus:demo")


def test_all_hardware_kinds_queue_without_mutating_guest(
    client, hardware_vm, monkeypatch
):
    from app.main import pending_hardware, pending_reference

    headers = login(client)
    monkeypatch.setattr(
        "app.main.get_iso", lambda _: {"name": "test.iso", "path": "/tmp/test.iso"}
    )
    for method, path, body in [
        ("POST", "/disks", {"pool": "hdd-pool", "size_gib": 10}),
        ("PUT", "/disks/root", {"size_gib": 8}),
        ("POST", "/nics", {"network": "testnet"}),
        ("PUT", "/cdrom", {"iso_id": "testiso", "boot": True}),
        ("DELETE", "/devices/nic0", {"confirm_name": "nic0"}),
    ]:
        assert (
            client.request(
                method, "/api/vms/incus:demo" + path, headers=headers, json=body
            ).status_code
            == 200
        )
    assert [r["kind"] for r in pending_hardware("incus:demo")] == [
        "disk.add",
        "disk.resize",
        "nic",
        "cdrom",
        "device.detach",
    ]
    assert pending_reference("network", "testnet")
    assert pending_reference("iso_id", "testiso")
    assert list(hardware_vm["devices"]) == ["root"]


def test_libvirt_pending_resources_use_inactive_xml_on_start(
    client, hardware_vm, monkeypatch
):
    import xml.etree.ElementTree as ET
    from app.main import power_action, pending_hardware

    headers = login(client)
    status = ["running"]
    monkeypatch.setattr(libvirt, "cli", lambda *args: status[0])
    root = ET.fromstring(
        '<domain><vcpu>2</vcpu><memory unit="KiB">2097152</memory><currentMemory unit="KiB">2097152</currentMemory><cpu><topology sockets="1" cores="2" threads="1"/></cpu></domain>'
    )
    monkeypatch.setattr(libvirt, "xml", lambda _: root)
    defined = []
    monkeypatch.setattr(libvirt, "define", lambda r: defined.append(r))
    client.put(
        "/api/vms/libvirt:demo", headers=headers, json={"cpu": 4, "memory_gib": 3}
    )
    assert not defined
    status[0] = "shut off"
    actions = []
    monkeypatch.setattr(libvirt, "action", lambda _, action: actions.append(action))
    power_action("libvirt:demo", "start")
    assert root.findtext("vcpu") == "4"
    assert root.findtext("memory") == str(3 * 1024**2)
    assert actions == ["start"]
    assert not pending_hardware("libvirt:demo")


def test_cpu_rates_normalize_all_allocated_cores_and_reset():
    from app.main import cpu_percentage, metric_samples

    metric_samples.clear()
    assert cpu_percentage("incus:test", 1, "boot1", 2, 10) is None
    assert cpu_percentage("incus:test", 2, "boot1", 2, 12) == 25
    assert cpu_percentage("incus:test", 0, "boot2", 2, 13) is None
    assert cpu_percentage("incus:test", 100, "boot2", 2, 14) == 100


def test_metrics_use_labelled_fallback_and_viewers_can_read(
    client, hardware_vm, monkeypatch
):
    from types import SimpleNamespace

    headers = login(client, "viewer")
    monkeypatch.setattr(
        incus,
        "get",
        lambda _: {"status": "Running", "pid": 123, "memory": {"total": 0}},
    )
    process = SimpleNamespace(
        cpu_times=lambda: SimpleNamespace(user=1, system=2),
        create_time=lambda: 1234,
        memory_info=lambda: SimpleNamespace(rss=1024**3),
    )
    monkeypatch.setattr("app.main.psutil.Process", lambda _: process)
    result = client.get("/api/vms/incus:demo/metrics")
    assert result.status_code == 200
    data = result.json()
    assert data["memory_used"] == 1024**3
    assert data["memory_source"] == "Host VM resident memory"
    assert data["cpu_source"] == "Host VM process"
    assert (
        client.request(
            "DELETE", "/api/vms/incus:demo/pending", headers=headers
        ).status_code
        == 403
    )


def test_libvirt_memory_metrics_and_stopped_reset(client, monkeypatch):
    login(client)
    monkeypatch.setattr(
        libvirt,
        "cli",
        lambda *args: "state.state=1\ncpu.time=2000000000\nvcpu.current=2\nballoon.current=2097152\nballoon.available=2097152\nballoon.unused=1048576",
    )
    result = client.get("/api/vms/libvirt:demo/metrics").json()
    assert result["memory_used"] == 1024**3
    assert result["memory_source"] == "Guest memory used"
    monkeypatch.setattr(libvirt, "cli", lambda *args: "state.state=5")
    result = client.get("/api/vms/libvirt:demo/metrics").json()
    assert result["running"] is False
    assert result["memory_used"] is None
    assert result["cpu_percent"] is None


def test_idle_console_stays_writable_and_revocation_closes_it(client, monkeypatch):
    import asyncio
    import time
    from types import SimpleNamespace

    headers = login(client)
    queue = asyncio.Queue()

    class Writer:
        def write(self, data):
            queue.put_nowait(b"echo:" + data)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def open_connection(*args):
        return SimpleNamespace(read=lambda _: queue.get()), Writer()

    monkeypatch.setattr(libvirt, "cli", lambda *args: "vnc://127.0.0.1:0")
    monkeypatch.setattr("app.main.asyncio.open_connection", open_connection)
    monkeypatch.setattr("app.main.CONSOLE_AUTH_INTERVAL", 0.02)
    with client.websocket_connect(
        "/api/console/libvirt:demo?csrf=" + headers["X-CSRF-Token"],
        headers={"Origin": "http://testserver"},
    ) as ws:
        time.sleep(0.08)
        ws.send_bytes(b"key")
        assert ws.receive_bytes() == b"echo:key"
        with store.db() as db:
            db.execute("UPDATE users SET role='viewer' WHERE username='admin'")
        with pytest.raises(Exception):
            ws.receive_bytes()
