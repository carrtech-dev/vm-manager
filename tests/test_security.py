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
