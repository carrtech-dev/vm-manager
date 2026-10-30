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


def test_snapshot_api_permissions_confirmations_and_validation(client, monkeypatch):
    from app.main import snapshots
    path = "/api/vms/incus:demo/snapshots"
    assert client.get(path).status_code == 401
    monkeypatch.setattr(snapshots, "listing", lambda *args: {"snapshots": [], "supported": True})
    headers = login(client, "viewer")
    assert client.get(path).status_code == 200
    assert client.post(path, headers=headers, json={"name": "one"}).status_code == 403
    assert client.post(path + "/one/restore", headers=headers, json={"confirm_name": "demo"}).status_code == 403
    assert client.request("DELETE", path + "/one", headers=headers, json={"confirm_name": "one"}).status_code == 403
    headers = login(client, "operator")
    assert client.post(path, json={"name": "one"}).status_code == 403
    assert client.post(path, headers=headers, json={"name": "../bad"}).status_code == 422
    assert client.post(path + "/one/restore", headers=headers, json={"confirm_name": "wrong"}).status_code == 400
    assert client.request("DELETE", path + "/one", headers=headers, json={"confirm_name": "wrong"}).status_code == 400


def test_snapshot_tasks_are_audited_and_pending_hardware_blocks_restore(client, monkeypatch):
    import time
    import app.main as main
    headers = login(client, "operator")
    vm_id = "incus:snapshot-api-check"
    path = "/api/vms/" + vm_id + "/snapshots"
    calls = []
    monkeypatch.setattr(incus, "vm", lambda name: {"name": name, "status": "Stopped"})
    monkeypatch.setattr(main.snapshots, "create", lambda *args: calls.append(args) or "Created snapshot one")

    def wait(response):
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = next(t for t in client.get("/api/tasks").json() if t["id"] == task_id)
            if row["status"] not in ("running", "queued"):
                return row
            time.sleep(.01)
        pytest.fail("Snapshot task did not finish")

    row = wait(client.post(path, headers=headers, json={"name": "one", "description": "Before upgrade"}))
    assert row["status"] == "completed"
    assert len(calls) == 1
    assert calls[0][:6] == ("incus", "snapshot-api-check", vm_id, "one", "Before upgrade", False)
    with store.db() as db:
        assert db.execute("SELECT 1 FROM audit WHERE action='vm.snapshot.create' AND resource=?", (vm_id,)).fetchone()
    monkeypatch.setattr(main, "pending_hardware", lambda vm: [{"id": "pending"}])
    row = wait(client.post(path + "/one/restore", headers=headers, json={"confirm_name": "snapshot-api-check"}))
    assert row["status"] == "failed"
    assert "pending hardware" in row["message"]


def test_password_hashes():
    hashed = hash_password("example-password")
    assert verify_password("example-password", hashed)
    assert not verify_password("wrong", hashed)
    assert hashed != hash_password("example-password")


def test_frontend_uses_versioned_modules(client):
    from app.main import ASSET_VERSION
    response = client.get("/")
    assert response.headers["cache-control"] == "no-store"
    prefix = "/assets/" + ASSET_VERSION
    assert prefix + "/app.js" in response.text
    assert client.get(prefix + "/app.js").status_code == 200
    assert client.get(prefix + "/core.js").status_code == 200


def test_unauthenticated_inventory(client):
    assert client.get("/api/inventory").status_code == 401
    assert client.get("/api/users").status_code == 401


def test_public_tunnel_upgrades_http_and_keeps_https_origin_checks(client):
    response = client.get(
        "https://vmm.carrtech.dev/health?check=tunnel",
        headers={"X-Forwarded-Proto": "http"}, follow_redirects=False,
    )
    assert response.status_code == 308
    assert response.headers["location"] == "https://vmm.carrtech.dev/health?check=tunnel"
    response = client.post(
        "https://vmm.carrtech.dev/api/login", json={},
        headers={"X-Forwarded-Proto": "http"}, follow_redirects=False,
    )
    assert response.status_code == 308
    response = client.get(
        "https://vmm.carrtech.dev/health", headers={"X-Forwarded-Proto": "https"}
    )
    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    response = client.post(
        "https://vmm.carrtech.dev/api/login", json={},
        headers={"Origin": "https://evil.example", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 403
    response = client.get("/health", headers={"X-Forwarded-Proto": "http"})
    assert response.status_code == 200
    assert "strict-transport-security" not in response.headers


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


@pytest.mark.parametrize("with_iso", [False, True])
def test_new_vm_boots_root_before_pxe_and_data_disks(monkeypatch, with_iso):
    from app.hypervisor import Incus

    driver = Incus()
    requests = []
    monkeypatch.setattr(driver, "request", lambda *args: requests.append(args))
    driver.create({
        "name": "boot-order-check", "pool": "test-pool", "disk_gib": 8,
        "network": "test-net", "cpu": 1, "memory_gib": 1,
        "secure_boot": False, "extra_disks": [4],
    }, {"name": "install.iso", "path": "/tmp/install.iso"} if with_iso else None)
    devices = next(args[2]["devices"] for args in requests if args[1] == "/1.0/instances")
    root_priority = int(devices["root"]["boot.priority"])
    # Incus sorts higher priorities first. NICs and data disks default to 0;
    # equal priorities put the NIC ahead of root on the affected host.
    for key in ("eth0", "disk1"):
        assert root_priority > int(devices[key].get("boot.priority", "0"))
    if with_iso:
        assert int(devices["cdrom"]["boot.priority"]) > root_priority
        assert int(driver.cdrom({"name": "other.iso"}, False)["boot.priority"]) < root_priority


@pytest.mark.parametrize("status,managed,priority,repair", [
    ("Stopped", "true", "0", True),
    ("Running", "true", "0", False),
    ("Stopped", "false", "0", False),
    ("Stopped", "true", "20", False),
    ("Stopped", "true", None, False),
])
def test_start_repairs_only_stopped_legacy_managed_vms(monkeypatch, status, managed, priority, repair):
    from app.hypervisor import Incus
    driver = Incus()
    root = {"type": "disk", "path": "/"}
    if priority is not None:
        root["boot.priority"] = priority
    vm = {"status": status, "config": {"user.vm-manager": managed}, "devices": {"root": root}}
    calls = []
    monkeypatch.setattr(driver, "vm", lambda _: vm)
    def update(_, transform):
        assert vm["status"] == "Stopped"
        calls.append("repair")
        transform(vm)
    monkeypatch.setattr(driver, "update", update)
    monkeypatch.setattr(driver, "request", lambda *args: calls.append(args[2]["action"]))
    driver.action("legacy-vm", "start")
    assert calls == (["repair", "start"] if repair else ["start"])
    assert root.get("boot.priority") == ("1" if repair else priority)


def test_boot_order_repair_failure_prevents_start(monkeypatch):
    from app.hypervisor import Incus
    driver = Incus()
    vm = {"status": "Stopped", "config": {"user.vm-manager": "true"},
          "devices": {"root": {"path": "/", "boot.priority": "0"}}}
    monkeypatch.setattr(driver, "vm", lambda _: vm)
    def update(_, transform):
        vm["status"] = "Running"
        transform(vm)
    monkeypatch.setattr(driver, "update", update)
    requests = []
    monkeypatch.setattr(driver, "request", lambda *args: requests.append(args))
    with pytest.raises(HypervisorError, match="must be stopped"):
        driver.action("legacy-vm", "start")
    assert not requests


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


@pytest.fixture
def organized_inventory(client, monkeypatch):
    with store.db() as db:
        db.execute("DELETE FROM resource_projects")
        db.execute("DELETE FROM resource_tags")
        db.execute("DELETE FROM projects")
        db.execute("DELETE FROM tags")
    monkeypatch.setattr(
        incus,
        "vms",
        lambda: [
            {
                "id": "incus:shared",
                "name": "shared",
                "provider": "incus",
                "devices": {},
            },
            {"id": "incus:other", "name": "other", "provider": "incus", "devices": {}},
        ],
    )
    monkeypatch.setattr(
        libvirt,
        "vms",
        lambda: [
            {
                "id": "libvirt:shared",
                "name": "shared",
                "provider": "libvirt",
                "devices": {},
            },
        ],
    )
    monkeypatch.setattr(
        incus,
        "networks",
        lambda: [
            {"id": "incus:shared", "name": "shared", "provider": "incus", "config": {}},
        ],
    )
    monkeypatch.setattr(
        libvirt,
        "networks",
        lambda: [
            {
                "id": "libvirt:shared",
                "name": "shared",
                "provider": "libvirt",
                "config": {},
            },
        ],
    )
    invalidate()
    return client


def create_label(client, headers, kind, name):
    response = client.post(
        f"/api/collections/{kind}", json={"name": name}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.parametrize("kind", ["projects", "tags"])
def test_collection_validation_and_rename(organized_inventory, kind):
    client = organized_inventory
    headers = login(client, "operator")
    item_id = create_label(client, headers, kind, "  Production  ")
    assert client.get("/api/collections").json()[kind] == [
        {"id": item_id, "name": "Production"}
    ]
    for duplicate in ["production", " PRODUCTION "]:
        assert (
            client.post(
                f"/api/collections/{kind}", json={"name": duplicate}, headers=headers
            ).status_code
            == 409
        )
    for invalid in ["", "   ", "x" * 65, "bad\nname", "bad\x00name"]:
        assert (
            client.post(
                f"/api/collections/{kind}", json={"name": invalid}, headers=headers
            ).status_code
            == 422
        )
    other = create_label(client, headers, kind, "Other")
    assert (
        client.put(
            f"/api/collections/{kind}/{other}",
            json={"name": "production"},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/api/collections/{kind}/{item_id}", json={"name": "Live"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/collections/{kind}/missing",
            json={"name": "Missing"},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/collections/{kind}/{item_id}", headers=headers).status_code
        == 200
    )
    assert (
        client.delete(f"/api/collections/{kind}/{item_id}", headers=headers).status_code
        == 404
    )


def test_assignments_are_distinct_persistent_and_replaceable(organized_inventory):
    from app import organization
    from app.security import Store

    client = organized_inventory
    headers = login(client)
    project = create_label(client, headers, "projects", "App")
    tag_a = create_label(client, headers, "tags", "Production")
    tag_b = create_label(client, headers, "tags", "Database")
    body = {"project_id": project, "tag_ids": [tag_a, tag_b, tag_a]}
    # Warm the hypervisor cache; manager metadata must appear immediately.
    client.get("/api/inventory")
    for kind, resource_id in [
        ("vm", "incus:shared"),
        ("vm", "libvirt:shared"),
        ("network", "incus:shared"),
        ("network", "libvirt:shared"),
    ]:
        assert (
            client.put(
                f"/api/organization/{kind}/{resource_id}", json=body, headers=headers
            ).status_code
            == 200
        )
    inventory_data = client.get("/api/inventory").json()
    for resource in inventory_data["vms"] + inventory_data["networks"]:
        if resource["name"] == "shared":
            assert resource["project_id"] == project
            assert set(resource["tag_ids"]) == {tag_a, tag_b}
        else:
            assert resource["project_id"] is None and resource["tag_ids"] == []
    reopened = Store(store.path.parent)
    assert organization.decorate(reopened, inventory())["vms"] == inventory_data["vms"]
    assert (
        client.put(
            "/api/organization/vm/incus:shared",
            json={"project_id": None, "tag_ids": [tag_b]},
            headers=headers,
        ).status_code
        == 200
    )
    updated = client.get("/api/inventory").json()
    incus_vm = next(v for v in updated["vms"] if v["id"] == "incus:shared")
    assert incus_vm["project_id"] is None and incus_vm["tag_ids"] == [tag_b]
    assert updated["networks"][0]["project_id"] == project
    assert (
        next(v for v in updated["vms"] if v["id"] == "libvirt:shared")["project_id"]
        == project
    )
    assert any(
        a["action"] == "organization.assign" for a in client.get("/api/audit").json()
    )


def test_invalid_assignment_is_atomic(organized_inventory):
    client = organized_inventory
    headers = login(client)
    project = create_label(client, headers, "projects", "App")
    tag = create_label(client, headers, "tags", "Test")
    route = "/api/organization/vm/incus:shared"
    assert (
        client.put(
            route, json={"project_id": project, "tag_ids": [tag]}, headers=headers
        ).status_code
        == 200
    )
    for invalid in [
        {"project_id": "missing"},
        {"tag_ids": ["missing"]},
        {"tag_ids": [project]},
    ]:
        assert client.put(route, json=invalid, headers=headers).status_code == 404
        vm = client.get("/api/inventory").json()["vms"][0]
        assert vm["project_id"] == project and vm["tag_ids"] == [tag]
    assert (
        client.put(
            "/api/organization/vm/incus:missing", json={}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/organization/disk/incus:shared", json={}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/collections/users", json={"name": "Bad"}, headers=headers
        ).status_code
        == 422
    )


def test_deleting_labels_retains_resources_and_clears_only_assignments(
    organized_inventory,
):
    client = organized_inventory
    headers = login(client)
    project = create_label(client, headers, "projects", "App")
    tag = create_label(client, headers, "tags", "Test")
    other_tag = create_label(client, headers, "tags", "Keep")
    for kind in ["vm", "network"]:
        client.put(
            f"/api/organization/{kind}/incus:shared",
            json={"project_id": project, "tag_ids": [tag, other_tag]},
            headers=headers,
        )
    client.delete(f"/api/collections/tags/{tag}", headers=headers)
    client.delete(f"/api/collections/projects/{project}", headers=headers)
    result = client.get("/api/inventory").json()
    assert len(result["vms"]) == 3 and len(result["networks"]) == 2
    for resources in [result["vms"], result["networks"]]:
        resource = next(r for r in resources if r["id"] == "incus:shared")
        assert resource["project_id"] is None and resource["tag_ids"] == [other_tag]


def test_organization_enforces_auth_csrf_and_viewer_role(organized_inventory):
    client = organized_inventory
    assert client.get("/api/collections").status_code == 401
    assert client.post("/api/collections/tags", json={"name": "No"}).status_code == 401
    headers = login(client, "viewer")
    assert client.get("/api/collections").status_code == 200
    for method, path, body in [
        ("POST", "/api/collections/tags", {"name": "No"}),
        ("PUT", "/api/collections/projects/missing", {"name": "No"}),
        ("DELETE", "/api/collections/tags/missing", None),
        ("PUT", "/api/organization/network/incus:shared", {}),
    ]:
        assert (
            client.request(method, path, json=body, headers=headers).status_code == 403
        )
    login(client)
    assert client.post("/api/collections/tags", json={"name": "No"}).status_code == 403


@pytest.mark.parametrize("kind", ["vm", "network"])
def test_resource_deletion_clears_organization_after_success(
    organized_inventory, monkeypatch, kind
):
    client = organized_inventory
    headers = login(client)
    project = create_label(client, headers, "projects", "App")
    tag = create_label(client, headers, "tags", "Test")
    client.put(
        f"/api/organization/{kind}/incus:shared",
        json={"project_id": project, "tag_ids": [tag]},
        headers=headers,
    )
    monkeypatch.setattr(
        "app.main.task", lambda user, action, resource, fn: fn() or {"task_id": "test"}
    )
    monkeypatch.setattr("app.main.ensure_stopped", lambda *args: None)
    monkeypatch.setattr(incus, "get", lambda *args: {})
    monkeypatch.setattr(incus, "delete", lambda *args: None)
    monkeypatch.setattr(incus, "request", lambda *args: None)
    route = "/api/vms/incus:shared" if kind == "vm" else "/api/networks/shared"
    assert (
        client.request(
            "DELETE", route, json={"confirm_name": "shared"}, headers=headers
        ).status_code
        == 200
    )
    with store.db() as db:
        assert not db.execute(
            "SELECT 1 FROM resource_projects WHERE kind=?", (kind,)
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM resource_tags WHERE kind=?", (kind,)
        ).fetchone()


def test_organization_schema_upgrades_existing_store_without_losing_data(tmp_path):
    import sqlite3
    from app.security import Store

    with sqlite3.connect(tmp_path / "manager.sqlite3") as db:
        db.execute(
            "CREATE TABLE isos(id TEXT PRIMARY KEY, name TEXT UNIQUE, path TEXT, size INTEGER, sha256 TEXT, created REAL)"
        )
        db.execute(
            "INSERT INTO isos VALUES('retained','retained.iso','/data/retained.iso',40000,'hash',0)"
        )
    upgraded = Store(tmp_path)
    with upgraded.db() as db:
        assert (
            db.execute("SELECT name FROM isos WHERE id='retained'").fetchone()[0]
            == "retained.iso"
        )
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tags").fetchone()[0] == 0


def test_failed_resource_delete_preserves_assignments(organized_inventory, monkeypatch):
    client = organized_inventory
    headers = login(client)
    tag = create_label(client, headers, "tags", "Keep")
    client.put(
        "/api/organization/vm/incus:shared", json={"tag_ids": [tag]}, headers=headers
    )
    monkeypatch.setattr(
        "app.main.task", lambda user, action, resource, fn: fn() or {"task_id": "test"}
    )

    def fail(*args):
        raise HypervisorError("VM is running")

    monkeypatch.setattr("app.main.ensure_stopped", fail)
    assert (
        client.request(
            "DELETE",
            "/api/vms/incus:shared",
            json={"confirm_name": "shared"},
            headers=headers,
        ).status_code
        == 409
    )
    assert client.get("/api/inventory").json()["vms"][0]["tag_ids"] == [tag]


@pytest.mark.parametrize("provider", ["incus", "libvirt"])
@pytest.mark.parametrize("action", ["shutdown", "restart"])
def test_force_stop_bypasses_wait_and_cancels_earlier_power_requests(
    client, monkeypatch, provider, action
):
    import threading
    from app.task_queue import ResourceExecutor
    import app.main as main

    normal = ResourceExecutor(max_workers=1)
    emergency = ResourceExecutor(max_workers=1)
    monkeypatch.setattr(main, "executor", normal)
    monkeypatch.setattr(main, "stop_executor", emergency)
    monkeypatch.setattr(main, "power_requests", main.PowerRequests())
    vm_id = provider + ":power-test"
    vm = {"status": "Running"}
    shutdown_waiting = threading.Event()
    stopped = threading.Event()
    release_shutdown = threading.Event()
    calls = []
    monkeypatch.setattr(incus, "vm", lambda _: dict(vm))
    monkeypatch.setattr(libvirt, "cli", lambda command, *args: (
        "shut off" if vm["status"] == "Stopped" else "running"
    ) if command == "domstate" else "<domain/>")
    monkeypatch.setattr(main, "pending_hardware", lambda _: [{"id": "saved-change"}])
    applied = []
    monkeypatch.setattr(main, "apply_pending", lambda _: applied.append(True))

    def driver_action(_, requested):
        calls.append(requested)
        if requested == "shutdown":
            shutdown_waiting.set()
            assert release_shutdown.wait(5), "test did not release graceful shutdown"
        elif requested == "stop":
            vm["status"] = "Stopped"
            stopped.set()

    monkeypatch.setattr(incus if provider == "incus" else libvirt, "action", driver_action)
    headers = login(client)

    def submit(requested, **extra):
        response = client.post(
            f"/api/vms/{vm_id}/action", headers=headers,
            json={"action": requested, **extra},
        )
        assert response.status_code == 200, response.text
        return response.json()["task_id"]

    try:
        original = submit(action)
        assert shutdown_waiting.wait(2)
        queued_start = submit("start")
        queued_shutdown = submit("shutdown")
        # A rejected stop must not cancel the existing requests.
        assert client.post(
            f"/api/vms/{vm_id}/action", headers=headers,
            json={"action": "stop", "confirm_name": "wrong"},
        ).status_code == 400
        force = submit("stop", confirm_name="power-test")
        assert stopped.wait(2), "Force stop waited behind graceful shutdown"
        assert not release_shutdown.is_set()
    finally:
        release_shutdown.set()
        normal.shutdown(wait=True)
        emergency.shutdown(wait=True)

    with store.db() as db:
        rows = {r["id"]: dict(r) for r in db.execute(
            "SELECT * FROM tasks WHERE id IN (?,?,?,?)",
            (original, queued_start, queued_shutdown, force),
        )}
        audits = db.execute(
            "SELECT action,detail FROM audit WHERE resource=?", (vm_id,)
        ).fetchall()
    assert rows[force]["status"] == "completed"
    for task_id in (original, queued_start, queued_shutdown):
        assert rows[task_id]["status"] == "interrupted"
        assert "Superseded by Force stop" in rows[task_id]["message"]
    assert calls == ["shutdown", "stop"]
    assert not applied
    assert vm["status"] == "Stopped"
    assert len(audits) >= 4


def test_shutdown_failure_explains_boot_menu_and_never_forces(client, monkeypatch):
    import app.main as main

    calls = []
    def fail(_, action):
        calls.append(action)
        raise HypervisorError("context deadline exceeded")

    monkeypatch.setattr(incus, "action", fail)
    with pytest.raises(HypervisorError, match="Boot menus.*Force stop"):
        main.power_action("incus:boot-menu", "shutdown")
    assert calls == ["shutdown"]


def test_libvirt_shutdown_waits_for_actual_power_off(client, monkeypatch):
    import app.main as main

    states = iter(["running", "shut off"])
    checked = []
    monkeypatch.setattr(libvirt, "action", lambda *args: None)
    def state(*args):
        checked.append(True)
        return next(states)
    monkeypatch.setattr(libvirt, "cli", state)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)
    main.power_action("libvirt:boot-menu", "shutdown")
    assert len(checked) == 2


def test_incus_force_stop_has_no_guest_shutdown_timeout(monkeypatch):
    from app.hypervisor import Incus

    driver = Incus()
    requests = []
    monkeypatch.setattr(driver, "request", lambda *args: requests.append(args))
    driver.action("boot-menu", "stop")
    driver.action("boot-menu", "shutdown")
    assert requests[0][2] == {
        "action": "stop", "timeout": 0, "force": True, "stateful": False,
    }
    assert requests[1][2] == {
        "action": "stop", "timeout": 60, "force": False, "stateful": False,
    }


@pytest.fixture
def storage_blocks(monkeypatch, tmp_path):
    from app import storage
    with store.db() as db:
        db.execute("DELETE FROM storage_defaults")
    blocks = [dict(id="a" * 32, name="media", parent="hdd-pool", purpose="iso",
                   size_gib=1, path=str(tmp_path / "media"), pool="", status="ready"),
              dict(id="b" * 32, name="disks", parent="hdd-pool", purpose="vm",
                   size_gib=8, path="", pool="vmm-block-" + "b" * 32, status="ready")]
    Path(blocks[0]["path"]).mkdir()
    calls = []
    def broker(operation, **kwargs):
        calls.append((operation, kwargs))
        return blocks if operation == "list" else {"ok": True}
    monkeypatch.setattr(storage, "broker", broker)
    monkeypatch.setattr(storage.os.path, "ismount", lambda p: str(p) == blocks[0]["path"])
    yield blocks, calls
    with store.db() as db:
        db.execute("DELETE FROM storage_defaults")


def run_storage_task(client, response):
    import time
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    for _ in range(100):
        row = next(t for t in client.get("/api/tasks").json() if t["id"] == task_id)
        if row["status"] in ("completed", "failed"):
            return row
        time.sleep(.01)
    pytest.fail("Task did not finish")


def test_storage_permissions_validation_and_assignment(client, storage_blocks):
    blocks, calls = storage_blocks
    headers = login(client, "viewer")
    assert len(client.get("/api/inventory").json()["storage_blocks"]) == 2
    for method, url, body in [
        ("POST", "/api/storage/blocks", dict(name="new", parent="hdd-pool", purpose="iso", size_gib=1)),
        ("PUT", "/api/storage/blocks/" + "a" * 32, dict(size_gib=2)),
        ("PUT", "/api/storage/defaults/iso", dict(block_id="a" * 32)),
        ("DELETE", "/api/storage/blocks/" + "a" * 32, dict(confirm_name="media")),
    ]:
        assert client.request(method, url, json=body, headers=headers).status_code == 403
    headers = login(client, "operator")
    assert client.post("/api/storage/blocks", headers=headers,
                       json=dict(name="../../bad", parent="hdd-pool", purpose="iso", size_gib=1)).status_code == 422
    assert client.put("/api/storage/defaults/iso", headers=headers, json=dict(block_id="b" * 32)).status_code == 400
    assert client.put("/api/storage/defaults/iso", headers=headers, json=dict(block_id="a" * 32)).status_code == 200
    row = run_storage_task(client, client.request("DELETE", "/api/storage/blocks/" + "a" * 32,
                            headers=headers, json=dict(confirm_name="media")))
    assert row["status"] == "failed" and "default" in row["message"]
    assert not any(op == "delete" for op, _ in calls)


def test_iso_upload_routes_to_block_and_rejects_wrong_or_missing_mount(client, storage_blocks, monkeypatch):
    from app import storage
    blocks, _ = storage_blocks
    headers = login(client, "operator")
    content = b"\0" * 32768 + b"CD001" + b"\0" * 32768
    client.put("/api/storage/defaults/iso", headers=headers, json=dict(block_id="a" * 32))
    response = client.post("/api/isos", headers=headers, files={"file": ("block-test.iso", content)})
    assert response.status_code == 200, response.text
    iso_id = response.json()["id"]
    with store.db() as db:
        path = db.execute("SELECT path FROM isos WHERE id=?", (iso_id,)).fetchone()[0]
        assert Path(path).parent == Path(blocks[0]["path"])
        db.execute("DELETE FROM isos WHERE id=?", (iso_id,))
    Path(path).unlink()
    response = client.post("/api/isos", headers=headers, data=dict(storage_id="b" * 32),
                           files={"file": ("wrong.iso", content)})
    assert response.status_code == 409
    # An explicit application destination must override the configured default.
    response = client.post("/api/isos", headers=headers, data=dict(storage_id="application"),
                           files={"file": ("original-storage.iso", content)})
    assert response.status_code == 200
    from app.main import ISOS
    with store.db() as db:
        original = db.execute("SELECT path FROM isos WHERE id=?", (response.json()["id"],)).fetchone()[0]
        assert Path(original).parent == ISOS
        db.execute("DELETE FROM isos WHERE id=?", (response.json()["id"],))
    Path(original).unlink()
    monkeypatch.setattr(storage.os.path, "ismount", lambda p: False)
    response = client.post("/api/isos", headers=headers, files={"file": ("unmounted.iso", content)})
    assert response.status_code == 409 and "not mounted" in response.text


def test_storage_pending_disk_reference_blocks_delete(client, storage_blocks):
    blocks, calls = storage_blocks
    headers = login(client, "operator")
    with store.db() as db:
        db.execute("INSERT INTO pending_hardware VALUES(?,?,?,?,?,?)",
                   ("storage-pending", "incus:test", "disk.add", '{"pool":"' + blocks[1]["pool"] + '"}', "", 0))
    try:
        row = run_storage_task(client, client.request("DELETE", "/api/storage/blocks/" + "b" * 32,
                                headers=headers, json=dict(confirm_name="disks")))
        assert row["status"] == "failed" and "pending" in row["message"]
        assert not any(op == "delete" for op, _ in calls)
    finally:
        with store.db() as db:
            db.execute("DELETE FROM pending_hardware WHERE id='storage-pending'")


def test_bulk_vm_api_requires_permission_confirmation_and_valid_selection(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'inventory', lambda: {'vms': [{'id': 'incus:one'}]})
    body = {'vm_ids': ['incus:one'], 'action': 'stop', 'confirmed': True}
    path = '/api/vms/bulk/action'
    assert client.post(path, json=body).status_code == 401
    headers = login(client, 'viewer')
    assert client.post(path, headers=headers, json=body).status_code == 403
    headers = login(client, 'operator')
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, headers=headers, json={**body, 'confirmed': False}).status_code == 400
    assert client.post(path, headers=headers, json={**body, 'vm_ids': []}).status_code == 422
    assert client.post(path, headers=headers, json={**body, 'vm_ids': ['incus:missing']}).status_code == 404
    assert client.post(path, headers=headers, json={**body, 'vm_ids': ['other:one']}).status_code == 400
    assert client.post(path, headers=headers, json={**body, 'preserve_backups': ['incus:one']}).status_code == 400
    assert client.post(path, headers=headers, json={**body, 'action': 'delete', 'preserve_backups': ['incus:other']}).status_code == 400
    with store.db() as db:
        before = db.execute('SELECT count(*) FROM tasks').fetchone()[0]
    assert client.post(path, headers=headers, json={**body, 'vm_ids': ['incus:one', 'incus:missing']}).status_code == 404
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


@pytest.mark.parametrize('action', ['shutdown', 'restart', 'stop'])
def test_bulk_power_runs_concurrently_once_per_vm_and_isolates_failures(client, monkeypatch, action):
    import threading
    import time
    import app.main as main
    ids = ['incus:bulk-one', 'libvirt:bulk-two']
    monkeypatch.setattr(main, 'inventory', lambda: {'vms': [{'id': vm} for vm in ids]})
    monkeypatch.setattr(incus, 'vm', lambda _: {'status': 'Stopped'})
    monkeypatch.setattr(libvirt, 'cli', lambda *args: '<domain/>')
    barrier = threading.Barrier(2)
    calls = []
    def operate(vm_id, operation, cancelled):
        calls.append((vm_id, operation))
        barrier.wait(timeout=5)  # Serial execution fails this test.
        if vm_id == ids[0]:
            raise HypervisorError('Injected guest failure')
    monkeypatch.setattr(main, 'power_action', operate)
    headers = login(client, 'operator')
    response = client.post('/api/vms/bulk/action', headers=headers, json={'vm_ids': ids + ids, 'action': action, 'confirmed': True})
    assert response.status_code == 200, response.text
    tasks = response.json()['tasks']
    assert len(tasks) == 2
    main.stop_executor.submit(lambda: None).result(timeout=10)
    main.executor.submit(lambda: None).result(timeout=10)
    with store.db() as db:
        rows = [dict(db.execute('SELECT * FROM tasks WHERE id=?', (t['task_id'],)).fetchone()) for t in tasks]
        for vm in ids:
            assert db.execute('SELECT 1 FROM audit WHERE action=? AND resource=?', ('vm.' + action, vm)).fetchone()
    assert [r['status'] for r in rows] == ['failed', 'completed']
    assert sorted(calls) == sorted((vm, action) for vm in ids)


def test_bulk_delete_defaults_to_cleanup_and_preserves_only_checked_vm(client, monkeypatch):
    import app.main as main
    ids = ['incus:delete-one', 'libvirt:delete-two']
    monkeypatch.setattr(main, 'inventory', lambda: {'vms': [{'id': vm} for vm in ids]})
    monkeypatch.setattr(incus, 'vm', lambda _: {'status': 'Stopped'})
    monkeypatch.setattr(libvirt, 'cli', lambda *args: '<domain/>')
    calls = []
    monkeypatch.setattr(main, 'remove_vm', lambda vm, **options: calls.append((vm, options)))
    headers = login(client, 'operator')
    response = client.post('/api/vms/bulk/action', headers=headers, json={'vm_ids': ids, 'action': 'delete', 'confirmed': True, 'preserve_backups': [ids[1]]})
    assert response.status_code == 200
    main.executor.submit(lambda: None).result(timeout=10)
    assert sorted(calls) == [(ids[0], {'cleanup': True, 'preserve_backups': False, 'actor': 'operator'}), (ids[1], {'cleanup': True, 'preserve_backups': True, 'actor': 'operator'})]


def test_bulk_delete_cleanup_stopped_guards_backup_retention_and_failure(client, monkeypatch):
    import app.main as main
    vm = 'incus:delete-content-check'
    events = []
    monkeypatch.setattr(main, 'ensure_stopped', lambda *args: events.append('stopped'))
    monkeypatch.setattr(main.snapshots, 'guard_start', lambda *args: events.append('guard'))
    monkeypatch.setattr(main.snapshots, 'delete_all', lambda *args: events.append('snapshots'))
    monkeypatch.setattr(incus, 'delete', lambda *args: events.append('vm'))
    with store.db() as db:
        db.execute("INSERT INTO vm_backups(id,vm_id,block_id,status,created,trigger) VALUES('bulk-check',?,'block','completed',0,'manual')", (vm,))
        db.execute("INSERT INTO backup_plans(vm_id,settings) VALUES(?,'{}')", (vm,))
    def delete_backup(backup_id):
        events.append('backup')
        with store.db() as db:
            db.execute('DELETE FROM vm_backups WHERE id=?', (backup_id,))
    monkeypatch.setattr(main.backup_manager, 'delete', delete_backup)
    main.remove_vm(vm, cleanup=True, preserve_backups=True)
    assert events == ['stopped', 'guard', 'snapshots', 'vm']
    with store.db() as db:
        assert db.execute('SELECT 1 FROM vm_backups WHERE vm_id=?', (vm,)).fetchone()
        assert not db.execute('SELECT 1 FROM backup_plans WHERE vm_id=?', (vm,)).fetchone()
    events.clear()
    main.remove_vm(vm, cleanup=True, preserve_backups=False)
    assert events == ['stopped', 'guard', 'snapshots', 'backup', 'vm']
    events.clear()
    def fail(*args):
        raise HypervisorError('Snapshot storage unavailable')
    monkeypatch.setattr(main.snapshots, 'delete_all', fail)
    with pytest.raises(HypervisorError, match='unavailable'):
        main.remove_vm(vm, cleanup=True, preserve_backups=False)
    assert events == ['stopped', 'guard']
    assert vm not in main.deleting_vms
    with store.db() as db:
        db.execute("INSERT INTO vm_backups(id,vm_id,block_id,status,created,trigger) VALUES('bulk-active',?,'block','queued',0,'manual')", (vm,))
    events.clear()
    try:
        with pytest.raises(HypervisorError, match='active backup'):
            main.remove_vm(vm, cleanup=True, preserve_backups=False)
        assert not events
    finally:
        with store.db() as db:
            db.execute("DELETE FROM vm_backups WHERE id='bulk-active'")


def test_backup_cannot_queue_during_vm_deletion(client):
    import app.main as main
    vm = 'incus:deleting-check'
    main.deleting_vms.add(vm)
    try:
        with pytest.raises(HypervisorError, match='being deleted'):
            main.queue_backups({'username': 'operator'}, [vm], 'block')
    finally:
        main.deleting_vms.discard(vm)


def test_bulk_tasks_wait_behind_same_vm_backup_but_other_vm_can_run(client, monkeypatch):
    import threading
    import app.main as main
    from app.task_queue import ResourceExecutor
    normal = ResourceExecutor(max_workers=2)
    monkeypatch.setattr(main, 'executor', normal)
    ids = ['incus:busy-backup', 'incus:other-vm']
    monkeypatch.setattr(main, 'inventory', lambda: {'vms': [{'id': vm} for vm in ids]})
    monkeypatch.setattr(incus, 'vm', lambda _: {'status': 'Stopped'})
    release, entered, other_ran = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def backup():
        entered.set()
        assert release.wait(5)
        calls.append('backup')
    def operate(vm_id, *args):
        calls.append(vm_id)
        if vm_id == ids[1]:
            other_ran.set()
    monkeypatch.setattr(main, 'power_action', operate)
    try:
        main.task({'username': 'operator'}, 'backup.create', ids[0], backup)
        assert entered.wait(2)
        headers = login(client, 'operator')
        response = client.post('/api/vms/bulk/action', headers=headers,
                               json=dict(vm_ids=ids, action='shutdown', confirmed=True))
        assert response.status_code == 200
        assert other_ran.wait(2), 'Unrelated bulk task was blocked by backup'
        with store.db() as db:
            queued = db.execute('SELECT status FROM tasks WHERE id=?', (response.json()['tasks'][0]['task_id'],)).fetchone()
        assert queued['status'] == 'queued'
        assert calls == [ids[1]]
        release.set()
        normal.submit(lambda: None).result(timeout=3)
        assert calls == [ids[1], 'backup', ids[0]]
    finally:
        release.set()
        normal.shutdown()


def test_vm_creation_and_backup_restore_reserve_correct_vm_keys(client, monkeypatch):
    import app.main as main
    submitted = []
    monkeypatch.setattr(main.executor, 'submit', lambda fn, **options: submitted.append(options['resources']))
    monkeypatch.setattr(main, 'get_iso', lambda _: None)
    main.create_vm(main.VMCreate(name='new-vm', pool='pool', network='net'), {'username': 'operator'})
    assert submitted[-1] == {'incus:new-vm'}
    monkeypatch.setattr(main.backup_manager, 'record', lambda _: {'status': 'completed', 'vm_id': 'libvirt:source'})
    main.restore_backup('backup', main.backups.Restore(name='restored'), {'username': 'operator'})
    assert submitted[-1] == {'libvirt:source', 'libvirt:restored'}
