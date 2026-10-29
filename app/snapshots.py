"""Coordinated, offline copy-on-write snapshots with durable recovery manifests.

The hypervisor owns disk blocks; SQLite owns grouping, lineage and operation state.
Incus instance snapshots alone do not include attached custom volumes.
"""

import json
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .hypervisor import HypervisorError, name

NO_EXPIRY = "0001-01-01T00:00:00Z"


class Snapshots:
    def __init__(self, store, incus, libvirt):
        self.store, self.incus, self.libvirt = store, incus, libvirt

    def rows(self, vm_id):
        with self.store.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM vm_snapshots WHERE vm_id=? ORDER BY created,name", (vm_id,)
            )]

    def row(self, vm_id, snapshot):
        return next((r for r in self.rows(vm_id) if r["name"] == snapshot), None)

    def stopped(self, provider, vm_name):
        status = (self.incus.vm(vm_name)["status"].lower() if provider == "incus"
                  else self.libvirt.cli("domstate", vm_name).strip())
        if status not in ("stopped", "shut off"):
            raise HypervisorError("Shut down the VM before creating, restoring or deleting snapshots.")

    def guard_start(self, vm_id):
        if any(r["operation"] == "restore" and r["status"] != "ready" for r in self.rows(vm_id)):
            raise HypervisorError("Snapshot restore is incomplete. Retry that restore before starting or changing the VM.")

    def plan(self, provider, vm_name):
        if provider == "libvirt":
            root = self.libvirt.xml(vm_name)
            if root.find("./os/nvram") is not None or root.find("./devices/tpm") is not None:
                raise HypervisorError("This libvirt VM uses NVRAM or TPM state, which offline internal snapshots cannot capture.")
            if root.findall("./devices/filesystem"):
                raise HypervisorError("Host filesystem mounts cannot be included in a VM snapshot.")
            disks = []
            for disk in root.findall("./devices/disk"):
                if disk.get("device") == "cdrom" or disk.find("readonly") is not None:
                    continue
                driver, source, target = disk.find("driver"), disk.find("source"), disk.find("target")
                if (disk.get("device") != "disk" or disk.get("type") != "file"
                        or driver is None or driver.get("type") != "qcow2"
                        or source is None or not source.get("file") or target is None
                        or disk.find("shareable") is not None):
                    raise HypervisorError("Libvirt snapshots require exclusive file-backed qcow2 disks; raw, shared and passthrough disks are unsupported.")
                disks.append({"device": target.get("dev"), "source": source.get("file")})
            if not disks:
                raise HypervisorError("No snapshot-capable disks were found.")
            paths = {str(Path(d["source"]).resolve()) for d in disks}
            for other in self.libvirt.cli("list", "--all", "--name").splitlines():
                if not other or other == vm_name:
                    continue
                for source in self.libvirt.xml(other).findall("./devices/disk/source"):
                    if source.get("file") and str(Path(source.get("file")).resolve()) in paths:
                        raise HypervisorError("A disk is also attached to another libvirt VM. Shared disks cannot be snapshotted safely.")
            return {"disks": disks, "identity": root.findtext("uuid")}

        vm = self.incus.vm(vm_name)
        if vm.get("type") != "virtual-machine":
            raise HypervisorError("Snapshots are available for virtual machines only.")
        devices = vm.get("expanded_devices", vm.get("devices", {}))
        roots = [d for d in devices.values() if d.get("type") == "disk" and d.get("path") == "/"]
        if len(roots) != 1 or not roots[0].get("pool"):
            raise HypervisorError("The VM must have one Incus-managed root disk.")
        root_pool = roots[0]["pool"]
        volumes, disks = [], [{"device": "root", "pool": root_pool, "source": vm_name}]
        for key, device in sorted(devices.items()):
            if device.get("type") != "disk" or device.get("path") == "/":
                continue
            if device.get("readonly") == "true":
                continue
            pool, source = device.get("pool"), device.get("source")
            if not pool or not source or "/" in source:
                raise HypervisorError("Host paths and passthrough disks cannot be included in a VM snapshot.")
            url = f"/1.0/storage-pools/{quote(pool, safe='')}/volumes/custom/{quote(source, safe='')}"
            volume = self.incus.get(url)
            if volume.get("content_type") == "iso":
                continue
            self.exclusive(volume, vm_name)
            if (pool, source) not in [(v["pool"], v["source"]) for v in volumes]:
                volumes.append({"pool": pool, "source": source, "url": url})
            disks.append({"device": key, "pool": pool, "source": source})
        drivers = {}
        for pool in {root_pool, *(v["pool"] for v in volumes)}:
            data = self.incus.get("/1.0/storage-pools/" + quote(pool, safe=""))
            if data["driver"] not in ("zfs", "btrfs", "lvm", "ceph"):
                raise HypervisorError("Incremental snapshots require ZFS, Btrfs, LVM or Ceph storage; full-copy storage is unsupported.")
            if data["driver"] == "lvm" and data.get("config", {}).get("lvm.use_thinpool", "true") != "true":
                raise HypervisorError("LVM snapshots require a thin pool to share unchanged blocks.")
            drivers[pool] = data["driver"]
        return {"root_pool": root_pool, "volumes": volumes, "disks": disks,
                "drivers": drivers, "identity": vm.get("config", {}).get("volatile.uuid"),
                "description": vm.get("description", "")}

    @staticmethod
    def exclusive(volume, vm_name):
        own = "/1.0/instances/" + quote(vm_name, safe="")
        if any(ref != own and not ref.startswith(own + "/snapshots/") for ref in volume.get("used_by", [])):
            raise HypervisorError("A data volume is shared with another resource. Shared volumes cannot be snapshotted or restored safely.")

    def native(self, provider, vm_name):
        if provider == "incus":
            return [s["name"].rsplit("/", 1)[-1] for s in self.incus.get(
                "/1.0/instances/" + quote(vm_name) + "/snapshots?recursion=1")]
        return self.libvirt.cli("snapshot-list", vm_name, "--name").splitlines()

    def listing(self, provider, vm_name, vm_id):
        rows = self.rows(vm_id)
        native = self.native(provider, vm_name)
        with self.store.db() as db:
            head = db.execute("SELECT name FROM snapshot_heads WHERE vm_id=?", (vm_id,)).fetchone()
        try:
            plan = self.plan(provider, vm_name)
            reason = ""
        except HypervisorError as exc:
            plan, reason = {}, str(exc)
        items = []
        for r in rows:
            manifest = json.loads(r["manifest"])
            status = r["status"]
            error = r["error"]
            if status == "ready" and r["name"] not in native:
                status, error = "unavailable", "The hypervisor snapshot is missing. Delete this entry after inspection."
            if status == "ready" and provider == "incus":
                try:
                    for volume in manifest["volumes"]:
                        self.incus.get(volume["url"] + "/snapshots/" + manifest["volume_snapshot"])
                except HypervisorError as exc:
                    status, error = "unavailable", "A data disk snapshot is unavailable: " + str(exc)
            items.append({k: r[k] for k in ("name", "parent", "description", "created", "operation")} | {
                "status": status, "error": error, "managed": True,
                "current": bool(head and head["name"] == r["name"]),
                "children": [s["name"] for s in rows if s["parent"] == r["name"]],
                "disks": manifest["disks"],
            })
        for snapshot in native:
            if not any(r["name"] == snapshot for r in rows):
                items.append({"name": snapshot, "status": "external", "managed": False})
        return {"snapshots": items, "supported": not reason, "reason": reason,
                "disks": plan.get("disks", []), "mode": "copy-on-write"}

    def set_state(self, vm_id, snapshot, status, operation, error=""):
        with self.store.db() as db:
            db.execute("UPDATE vm_snapshots SET status=?,operation=?,error=? WHERE vm_id=? AND name=?",
                       (status, operation, error, vm_id, snapshot))

    def create(self, provider, vm_name, vm_id, snapshot, description):
        name(snapshot)
        self.stopped(provider, vm_name)
        rows = self.rows(vm_id)
        if any(r["status"] != "ready" for r in rows):
            raise HypervisorError("Resolve incomplete snapshots before creating another snapshot.")
        if snapshot in self.native(provider, vm_name) or any(r["name"] == snapshot for r in rows):
            raise HypervisorError("A snapshot with this name already exists.")
        manifest = self.plan(provider, vm_name)
        manifest["volume_snapshot"] = "vmm-" + uuid.uuid4().hex
        with self.store.db() as db:
            head = db.execute("SELECT name FROM snapshot_heads WHERE vm_id=?", (vm_id,)).fetchone()
            db.execute("INSERT INTO vm_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                       (vm_id, snapshot, head["name"] if head else None, description, time.time(),
                        "creating", "create", json.dumps(manifest), ""))
        try:
            if provider == "incus":
                for volume in manifest["volumes"]:
                    self.incus.request("POST", volume["url"] + "/snapshots", {
                        "name": manifest["volume_snapshot"], "expires_at": NO_EXPIRY})
                self.incus.request("POST", "/1.0/instances/" + quote(vm_name) + "/snapshots", {
                    "name": snapshot, "stateful": False, "expires_at": NO_EXPIRY})
            else:
                root = ET.Element("domainsnapshot")
                ET.SubElement(root, "name").text = snapshot
                ET.SubElement(root, "description").text = description
                ET.SubElement(root, "memory", snapshot="no")
                disks = ET.SubElement(root, "disks")
                for disk in manifest["disks"]:
                    ET.SubElement(disks, "disk", name=disk["device"], snapshot="internal")
                self.libvirt.cli("snapshot-create", vm_name, "/dev/stdin", stdin=ET.tostring(root, encoding="unicode"))
            self.finish(vm_id, snapshot, "create")
        except Exception as exc:
            self.set_state(vm_id, snapshot, "incomplete", "create", str(exc))
            raise HypervisorError("Snapshot creation is incomplete; delete it before retrying. " + str(exc)) from exc
        return f"Snapshot {snapshot} created, including {len(manifest['disks'])} disk(s)."

    def finish(self, vm_id, snapshot, operation):
        with self.store.db() as db:
            db.execute("UPDATE vm_snapshots SET status='ready',operation=?,error='' WHERE vm_id=? AND name=?",
                       (operation, vm_id, snapshot))
            db.execute("INSERT INTO snapshot_heads VALUES(?,?) ON CONFLICT(vm_id) DO UPDATE SET name=excluded.name",
                       (vm_id, snapshot))

    def zfs_latest(self, url, snapshot):
        # ZFS rollback to an older snapshot can destroy newer snapshots. Always
        # refuse it ourselves, even if zfs.remove_snapshots is enabled on the host.
        snapshots = self.incus.get(url + "/snapshots?recursion=1")
        target = next((s for s in snapshots if s["name"].rsplit("/", 1)[-1] == snapshot), None)
        if target is None:
            raise HypervisorError("A required disk snapshot is missing.")
        try:
            target_time = datetime.fromisoformat(target["created_at"])
            newer = any(datetime.fromisoformat(s["created_at"]) > target_time for s in snapshots)
        except (KeyError, ValueError, TypeError) as exc:
            raise HypervisorError("Cannot verify ZFS snapshot order; inspect snapshot timestamps before restoring.") from exc
        if newer:
            raise HypervisorError("ZFS requires newer snapshots to be deleted before restoring this snapshot. Delete newer snapshots explicitly, newest first.")

    def restore(self, provider, vm_name, vm_id, snapshot):
        self.stopped(provider, vm_name)
        row = self.row(vm_id, snapshot)
        if not row or not (row["status"] == "ready" or
                           row["status"] == "incomplete" and row["operation"] == "restore"):
            raise HypervisorError("Only a complete snapshot or an interrupted restore can be restored.")
        if any(r["status"] != "ready" and r["name"] != snapshot for r in self.rows(vm_id)):
            raise HypervisorError("Resolve other incomplete snapshots before restoring.")
        manifest = json.loads(row["manifest"])
        current = self.plan(provider, vm_name)
        if current["identity"] != manifest["identity"] or current["disks"] != manifest["disks"]:
            raise HypervisorError("The VM identity or attached disks changed. Restore the original disk attachments before restoring this snapshot.")
        if snapshot not in self.native(provider, vm_name):
            raise HypervisorError("The hypervisor snapshot is missing.")
        if provider == "incus":
            for volume in manifest["volumes"]:
                self.incus.get(volume["url"] + "/snapshots/" + manifest["volume_snapshot"])
                if current["drivers"][volume["pool"]] == "zfs":
                    self.zfs_latest(volume["url"], manifest["volume_snapshot"])
            if current["drivers"][manifest["root_pool"]] == "zfs":
                root_url = ("/1.0/storage-pools/" + quote(manifest["root_pool"], safe="")
                            + "/volumes/virtual-machine/" + quote(vm_name))
                self.zfs_latest(root_url, snapshot)
        self.set_state(vm_id, snapshot, "restoring", "restore")
        try:
            if provider == "incus":
                for volume in manifest["volumes"]:
                    original = self.incus.get(volume["url"])
                    self.incus.request("PUT", volume["url"], {
                        "config": original.get("config", {}), "description": original.get("description", ""),
                        "restore": manifest["volume_snapshot"]})
                self.incus.request("PUT", "/1.0/instances/" + quote(vm_name), {"restore": snapshot, "stateful": False})
                # Incus snapshot metadata contains config/devices but does not
                # retain the instance description. Restore our saved value too.
                self.incus.update(vm_name, lambda vm: vm.update(description=manifest.get("description", "")))
            else:
                self.libvirt.cli("snapshot-revert", vm_name, snapshot)
            self.finish(vm_id, snapshot, "restore")
        except Exception as exc:
            self.set_state(vm_id, snapshot, "incomplete", "restore", str(exc))
            raise HypervisorError("Snapshot restore is incomplete. Keep the VM stopped and retry this restore. " + str(exc)) from exc
        return f"Restored snapshot {snapshot}. VM remains stopped."

    def delete(self, provider, vm_name, vm_id, snapshot):
        self.stopped(provider, vm_name)
        row = self.row(vm_id, snapshot)
        if not row:
            raise HypervisorError("Snapshot is not managed by VM Manager.")
        if row["operation"] == "restore" and row["status"] != "ready":
            raise HypervisorError("Retry the incomplete restore before deleting this snapshot.")
        if any(r["parent"] == snapshot for r in self.rows(vm_id)):
            raise HypervisorError("Delete child snapshots first, newest first, to preserve the snapshot chain.")
        manifest = json.loads(row["manifest"])
        self.set_state(vm_id, snapshot, "deleting", "delete")
        try:
            if snapshot in self.native(provider, vm_name):
                if provider == "incus":
                    self.incus.request("DELETE", "/1.0/instances/" + quote(vm_name) + "/snapshots/" + quote(snapshot))
                else:
                    self.libvirt.cli("snapshot-delete", vm_name, snapshot)
            if provider == "incus":
                for volume in manifest["volumes"]:
                    names = [s["name"].rsplit("/", 1)[-1] for s in self.incus.get(volume["url"] + "/snapshots?recursion=1")]
                    if manifest["volume_snapshot"] in names:
                        self.incus.request("DELETE", volume["url"] + "/snapshots/" + manifest["volume_snapshot"])
            with self.store.db() as db:
                db.execute("DELETE FROM vm_snapshots WHERE vm_id=? AND name=?", (vm_id, snapshot))
                db.execute("UPDATE snapshot_heads SET name=? WHERE vm_id=? AND name=?", (row["parent"], vm_id, snapshot))
        except Exception as exc:
            self.set_state(vm_id, snapshot, "incomplete", "delete", str(exc))
            raise HypervisorError("Snapshot deletion is incomplete; retry deletion. " + str(exc)) from exc
        return f"Snapshot {snapshot} deleted."
