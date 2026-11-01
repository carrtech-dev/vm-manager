"""Root-only ZFS storage provisioning behind a local, group-restricted socket.

Accepts structured operations, never commands or paths. A root-owned registry and
ZFS ownership properties constrain deletion to datasets created by this service.
"""

import grp
import json
import os
from pathlib import Path
import pwd
import re
import socketserver
import threading
import uuid

from .hypervisor import Incus, HypervisorError, run
from .storage import BlockCreate, BlockSize
from . import external_storage

REGISTRY = Path("/var/lib/vm-manager-storage/blocks.json")
MOUNTS = Path("/var/lib/vm-manager-storage/mounts")
SOCKET = Path("/run/vm-manager-storage/control.sock")
PROPERTY = "carrtech:vmm-block"
incus = Incus()
operation_lock = threading.RLock()


def read_registry():
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else []


def save_registry(blocks):
    REGISTRY.parent.mkdir(mode=0o711, parents=True, exist_ok=True)
    temporary = REGISTRY.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(blocks, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, REGISTRY)


def owned(block):
    value = run("zfs", "get", "-H", "-o", "value", PROPERTY, block["dataset"])
    if value != block["id"]:
        raise HypervisorError("Dataset ownership does not match; manual recovery required")


def stats(block):
    result = dict(block, used=0, available=0)
    try:
        owned(block)
        values = run("zfs", "get", "-Hp", "-o", "value", "used,available,quota", block["dataset"]).splitlines()
        result.update(used=int(values[0]), available=int(values[1]), capacity=int(values[2]))
        if block["purpose"] != "vm" and not os.path.ismount(block["path"]):
            result["status"] = "unmounted"
    except Exception as exc:
        result.update(status="unavailable", error=str(exc))
    return result


def create(body):
    spec = BlockCreate.model_validate(body)
    blocks = read_registry()
    if any(b["name"].casefold() == spec.name.casefold() for b in blocks):
        raise HypervisorError("A storage block with this name already exists")
    parent = next((p for p in incus.pools() if p["name"] == spec.parent), None)
    if not parent or parent["driver"] != "zfs" or parent["name"].startswith("vmm-block-"):
        raise HypervisorError("Choose an underlying ZFS storage pool")
    source = parent["config"].get("zfs.pool_name") or parent["config"].get("source", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*(/[A-Za-z0-9_.:-]+)*", source):
        raise HypervisorError("This pool has no supported ZFS dataset source")
    root = source.split("/")[0]
    capacity = spec.size_gib * 1024**3
    available = int(run("zfs", "get", "-Hp", "-o", "value", "available", root))
    if capacity + 1024**3 > available:
        raise HypervisorError("Not enough underlying space; keep at least 1 GiB free")
    block_id = uuid.uuid4().hex
    block = dict(id=block_id, name=spec.name, parent=spec.parent, purpose=spec.purpose,
                 size_gib=spec.size_gib, dataset=root + "/vmm-block-" + block_id,
                 path=str(MOUNTS / block_id) if spec.purpose != "vm" else "",
                 pool="vmm-block-" + block_id if spec.purpose == "vm" else "",
                 status="creating")
    # Persist intent before mutation, leaving interrupted provisioning recoverable.
    blocks.append(block)
    save_registry(blocks)
    try:
        run("zfs", "create", "-o", PROPERTY + "=" + block_id,
            "-o", "quota=" + str(capacity), "-o", "reservation=" + str(capacity),
            "-o", "mountpoint=" + (block["path"] or "none"), block["dataset"])
        if spec.purpose == "vm":
            incus.request("POST", "/1.0/storage-pools", {
                "name": block["pool"], "driver": "zfs",
                "description": "VM Manager storage block: " + spec.name,
                "config": {"source": block["dataset"]},
            })
        else:
            account = pwd.getpwnam("vm-manager")
            os.chown(block["path"], account.pw_uid, account.pw_gid)
            os.chmod(block["path"], 0o755 if spec.purpose == "iso" else 0o700)
        block["status"] = "ready"
        save_registry(blocks)
    except Exception:
        block["status"] = "incomplete"
        save_registry(blocks)
        raise
    return stats(block)


def find_block(block_id):
    blocks = read_registry()
    block = next((b for b in blocks if b["id"] == block_id), None)
    if not block:
        raise HypervisorError("Storage block not found")
    owned(block)
    return blocks, block


def grow(block_id, body):
    spec = BlockSize.model_validate(body)
    blocks, block = find_block(block_id)
    current = stats(block)
    if block["status"] != "ready" or spec.size_gib * 1024**3 <= current.get("capacity", 0):
        raise HypervisorError("Choose a larger capacity for a ready storage block")
    delta = spec.size_gib * 1024**3 - current["capacity"]
    available = int(run("zfs", "get", "-Hp", "-o", "value", "available", block["dataset"].split("/")[0]))
    if delta + 1024**3 > available:
        raise HypervisorError("Not enough underlying space; keep at least 1 GiB free")
    run("zfs", "set", "quota=" + str(spec.size_gib * 1024**3),
        "reservation=" + str(spec.size_gib * 1024**3), block["dataset"])
    block["size_gib"] = spec.size_gib
    save_registry(blocks)
    return stats(block)


def delete(block_id):
    blocks, block = find_block(block_id)
    if block["status"] != "ready":
        raise HypervisorError("Incomplete provisioning requires manual recovery")
    if block["purpose"] == "vm":
        volumes = incus.get("/1.0/storage-pools/" + block["pool"] + "/volumes?recursion=1")
        if volumes:
            raise HypervisorError("Storage block contains volumes; remove or move them first")
        incus.request("DELETE", "/1.0/storage-pools/" + block["pool"])
    else:
        if not os.path.ismount(block["path"]):
            raise HypervisorError("Cannot verify an unmounted storage block is empty")
        if any(Path(block["path"]).iterdir()):
            raise HypervisorError("Storage block contains files; retain or move them first")
        # No recursive destroy: child datasets and snapshots prevent deletion.
        run("zfs", "destroy", block["dataset"])
    blocks.remove(block)
    save_registry(blocks)
    return {"ok": True}


def dispatch(request):
    operation = request.get("operation")
    if isinstance(operation, str) and operation.startswith('edge_'):
        from . import edge_runtime
        return edge_runtime.dispatch(operation, request)
    if operation == 'patch_guest':
        from . import patching
        return patching.dispatch(request)
    if isinstance(operation, str) and operation.startswith('external_'):
        return external_storage.dispatch(operation, request)
    if operation == "list":
        return [stats(b) for b in read_registry()]
    if operation == "libvirt_export":
        from .backup_libvirt import export
        block_id, backup_id = request.get('block_id', ''), request.get('backup_id', '')
        if not all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{32}', v) for v in (block_id, backup_id)):
            raise HypervisorError('Invalid backup reference')
        live = request.get('live', False)
        if not isinstance(live, bool):
            raise HypervisorError('Invalid live backup policy')
        if not any(b['id'] == block_id for b in read_registry()):
            path = external_storage.backup_directory(block_id)
            return export(path, backup_id, request.get('vm_name', ''), live=live)
        _, block = find_block(block_id)
        if block['purpose'] != 'backup' or stats(block)['status'] != 'ready':
            raise HypervisorError('Backup storage is unavailable')
        if live:
            # Existing blocks predate native QEMU writers. Enabling POSIX ACLs
            # preserves their data/modes and permits temporary per-job access.
            acltype = run('zfs', 'get', '-H', '-o', 'value', 'acltype', block['dataset'])
            if acltype == 'off':
                run('zfs', 'set', 'acltype=posixacl', block['dataset'])
            elif acltype not in ('posix', 'posixacl'):
                raise HypervisorError('Live libvirt backup requires POSIX ACL support on its backup block')
        return export(block['path'], backup_id, request.get('vm_name', ''), live=live)
    if operation == "create":
        with operation_lock:
            return create(request.get("body"))
    if operation in ("grow", "delete"):
        block_id = request.get("block_id", "")
        if not isinstance(block_id, str) or not re.fullmatch(r"[0-9a-f]{32}", block_id):
            raise HypervisorError("Invalid storage block reference")
        with operation_lock:
            return grow(block_id, request.get("body")) if operation == "grow" else delete(block_id)
    raise HypervisorError("Unsupported storage operation")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(200)
        try:
            request = json.loads(self.rfile.readline(2 * 1024 * 1024 + 1))
            response = {"result": dispatch(request)}
        except (KeyError, ValueError, TypeError):
            response = {"error": "Invalid storage request"}
        except Exception as exc:
            response = {"error": str(exc)[:1500]}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    os.umask(0o077)
    external_storage.initialize()
    SOCKET.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(SOCKET.parent, 0, grp.getgrnam("vm-manager").gr_gid)
    MOUNTS.mkdir(mode=0o755, parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(SOCKET), Handler) as server:
        os.chown(SOCKET, 0, grp.getgrnam("vm-manager").gr_gid)
        os.chmod(SOCKET, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()
