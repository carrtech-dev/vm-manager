"""Opt-in acceptance against the running HTTPS service on its hypervisor host.

Creates a temporary operator/session and a uniquely named disposable VM using an
existing installer ISO. Never changes an existing VM, ISO, or user's credentials.
"""

import argparse
import secrets
import sqlite3
import time
import uuid

import httpx

from app.hypervisor import Incus
from app.security import hash_password, token_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://192.168.8.235:8443")
    parser.add_argument("--data", default="/var/lib/vm-manager")
    parser.add_argument("--iso-name", required=True)
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:10]
    vm_name = "vmm-power-" + suffix
    vm_id = "incus:" + vm_name
    username = "power-check-" + suffix
    raw_session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    incus = Incus()
    created = False
    db = sqlite3.connect(args.data + "/manager.sqlite3", timeout=30)
    with db:
        user_id = db.execute(
            "INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)",
            (username, hash_password(secrets.token_urlsafe(32)), "operator", time.time()),
        ).lastrowid
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (
            token_hash(raw_session), user_id, csrf, time.time() + 1800,
        ))
    client = httpx.Client(
        base_url=args.url, verify=False, timeout=30,
        cookies={"vmm_session": raw_session}, headers={"X-CSRF-Token": csrf},
    )

    def request(method, path, body=None):
        response = client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()

    def wait_task(task_id, timeout=30, statuses=("completed",)):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = next(t for t in request("GET", "/api/tasks") if t["id"] == task_id)
            if row["status"] in statuses:
                return row
            assert row["status"] not in ("failed", "interrupted", "completed"), row
            time.sleep(0.1)
        raise AssertionError("Task did not reach expected state: " + str(row))

    def action(value):
        return request("POST", f"/api/vms/{vm_id}/action", {
            "action": value, "confirm_name": vm_name if value == "stop" else "",
        })["task_id"]

    try:
        inventory = request("GET", "/api/inventory")
        iso = next(i for i in inventory["isos"] if i["name"] == args.iso_name)
        creation = request("POST", "/api/vms", {
            "name": vm_name, "cpu": 1, "memory_gib": 2, "disk_gib": 4,
            "pool": "hdd-pool", "network": "incusbr0", "iso_id": iso["id"],
            "secure_boot": False, "autostart": False,
        })
        # Only this unique VM is eligible for cleanup, including partial creation.
        created = True
        wait_task(creation["task_id"])
        wait_task(action("start"))
        # The root disk is empty and the installer has just reached its boot menu.
        time.sleep(3)
        assert incus.vm(vm_name)["status"] == "Running"
        edit = request("PUT", f"/api/vms/{vm_id}", {"cpu": 2, "memory_gib": 2})
        wait_task(edit["task_id"])
        restart = action("restart")
        wait_task(restart, statuses=("running",))
        time.sleep(2)
        queued_start, queued_shutdown = action("start"), action("shutdown")
        before = time.monotonic()
        wait_task(action("stop"), timeout=15)
        elapsed = time.monotonic() - before
        assert incus.vm(vm_name)["status"] == "Stopped"
        for task_id in (restart, queued_start, queued_shutdown):
            wait_task(task_id, timeout=65, statuses=("interrupted",))
        time.sleep(1)
        vm = next(v for v in request("GET", "/api/inventory")["vms"] if v["id"] == vm_id)
        assert vm["status"] == "stopped", vm
        assert vm["pending"], "Interrupted restart applied or lost pending hardware"
        assert incus.vm(vm_name)["config"]["limits.cpu"] == "1"
        print(f"PASS: installer VM force-stopped in {elapsed:.2f}s during graceful restart; "
              "queued power requests interrupted; VM stayed off; pending hardware retained.", flush=True)
    finally:
        client.close()
        try:
            if created and any(v["name"] == vm_name for v in incus.vms()):
                if incus.vm(vm_name)["status"] != "Stopped":
                    incus.action(vm_name, "stop")
                incus.delete(vm_name)
        finally:
            with db:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                db.execute("DELETE FROM users WHERE id=?", (user_id,))
                db.execute("DELETE FROM pending_hardware WHERE vm_id=?", (vm_id,))
            db.close()
        print("Removed disposable VM and temporary operator/session.", flush=True)


if __name__ == "__main__":
    main()
