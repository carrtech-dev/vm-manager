"""CarrTech VM Manager: authenticated local hypervisor control plane."""

import asyncio
import errno
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from types import SimpleNamespace
from websockets.asyncio.client import unix_connect
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse, urlsplit

import psutil
from fastapi import (
    FastAPI,
    Request,
    Response,
    HTTPException,
    Depends,
    UploadFile,
    Form,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .hypervisor import Incus, Libvirt, HypervisorError, name, run, bytes_size
from .security import Store, ROLES, hash_password, verify_password, token_hash
from . import organization
from . import storage
from . import backups
from .power import PowerInterrupted, PowerRequests
from .protection import ProtectionPower
from .task_queue import ResourceExecutor
from .snapshots import Snapshots

DATA = Path(os.environ.get("VM_MANAGER_DATA", "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
ISOS = DATA / "isos"
ISOS.mkdir(exist_ok=True)
store = Store(DATA)
store.bootstrap(DATA)
backups.initialize(store)
incus, libvirt = Incus(), Libvirt()
snapshots = Snapshots(store, incus, libvirt)


@asynccontextmanager
async def backup_lifespan(application):
    async def loop():
        while True:
            try:
                await asyncio.to_thread(backup_tick)
            except Exception:
                logging.exception('Backup scheduler failed')
            await asyncio.sleep(15)
    scheduler = asyncio.create_task(loop())
    try:
        yield
    finally:
        scheduler.cancel()
        try:
            await scheduler
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="CarrTech VM Manager", docs_url=None, redoc_url=None, openapi_url=None, lifespan=backup_lifespan
)
executor = ResourceExecutor(max_workers=4, thread_name_prefix="hypervisor")
stop_executor = ResourceExecutor(max_workers=4, thread_name_prefix="force-stop")
power_requests = PowerRequests()
DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing-only")
cache_lock = threading.Lock()
cache = {"at": 0, "data": None}
deleting_vms = set()  # Guarded by backup_manager.lock.


def invalidate():
    cache["at"] = 0


def task(user, action, resource, fn, resources=None):
    task_id = uuid.uuid4().hex
    with store.db() as db:
        db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
            (
                task_id,
                action,
                resource,
                user["username"],
                "queued",
                time.time(),
                None,
                "",
            ),
        )

    def execute():
        with store.db() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        try:
            if action.startswith("vm.") and ":" in resource:
                provider, vm_name = vm_ref(resource)
                original = (
                    json.dumps(incus.vm(vm_name), indent=2)
                    if provider == "incus"
                    else libvirt.cli("dumpxml", vm_name, "--inactive")
                )
                # Emergency stop must not wait behind a full disk export or a
                # failed backup mount. Its small config snapshot uses local state.
                if action == 'vm.stop':
                    directory = DATA / 'config-backups'
                    directory.mkdir(mode=0o700, exist_ok=True)
                    backup = directory / (task_id + ('.json' if provider == 'incus' else '.xml'))
                    try:
                        backup.write_text(original)
                        backup.chmod(0o600)
                    except OSError:
                        logging.exception('Emergency stop configuration snapshot failed')
                else:
                    with storage.lock:
                        directory = storage.directory(store, DATA, "backup")
                        backup = directory / (task_id + (".json" if provider == "incus" else ".xml"))
                        backup.write_text(original)
                        backup.chmod(0o600)
            result = fn()
            status, message = "completed", (
                result if isinstance(result, str) else "Completed successfully"
            )
        except PowerInterrupted as error:
            status, message = "interrupted", str(error)
        except Exception as error:
            logging.exception("Task %s failed for %s", action, resource)
            status, message = "failed", str(error)[:2000]
        invalidate()
        with store.db() as db:
            db.execute(
                "UPDATE tasks SET status=?,message=?,finished=? WHERE id=?",
                (status, message, time.time(), task_id),
            )
            store.audit(
                user["username"], action, resource, status + ": " + message, db=db
            )

    if resources is None:
        if action == "vm.create":
            resources = {"incus:" + resource}
        elif action.startswith(("vm.", "backup.")):
            resources = {resource}
    (stop_executor if action == "vm.stop" else executor).submit(execute, resources=resources)
    return {"task_id": task_id}


def session(request):
    raw = request.cookies.get("vmm_session", "")
    with store.db() as db:
        row = db.execute(
            "SELECT u.id,u.username,u.role,s.csrf,s.token FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.enabled=1",
            (token_hash(raw), time.time()),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Sign in to continue")
    return dict(row)


def require(permission="read"):
    def check(request: Request):
        user = session(request)
        if permission not in ROLES.get(user["role"], set()):
            raise HTTPException(403, "Your role does not permit this action")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", ""), user["csrf"]
            ):
                raise HTTPException(
                    403, "Session validation failed; refresh and try again"
                )
        return user

    return check


@app.middleware("http")
async def security_headers(request: Request, call_next):
    public_host = request.url.hostname == "vmm.carrtech.dev"
    # Cloudflare sets this to the visitor's protocol. It is used only to upgrade
    # public HTTP requests, never to establish identity or relax origin checks.
    if public_host and request.headers.get("x-forwarded-proto") == "http":
        return RedirectResponse(
            str(request.url.replace(scheme="https", netloc="vmm.carrtech.dev")),
            status_code=308,
        )
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse(
                {"detail": "Cross-origin request rejected"}, status_code=403
            )
    if request.url.path.startswith("/api/") and request.url.path != "/api/login":
        try:
            user = session(request)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if not secrets.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), user["csrf"]
                ):
                    raise HTTPException(
                        403, "Session validation failed; refresh and try again"
                    )
                if request.url.path == "/api/isos" and "operate" not in ROLES.get(
                    user["role"], set()
                ):
                    raise HTTPException(403, "Your role does not permit ISO uploads")
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    if public_host:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss:; font-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HypervisorError)
async def hypervisor_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.get("/health")
def health():
    revision = Path(__file__).resolve().parents[1] / "REVISION"
    return {
        "status": "ok",
        "service": "vm-manager",
        "revision": (
            revision.read_text().strip() if revision.exists() else "development"
        ),
    }


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    peer = request.client.host
    now = time.time()
    with store.db() as db:
        db.execute("DELETE FROM attempts WHERE at<?", (now - 900,))
        if (
            db.execute(
                "SELECT count(*) FROM attempts WHERE peer=?", (peer,)
            ).fetchone()[0]
            >= 12
        ):
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
        row = db.execute(
            "SELECT * FROM users WHERE username=?", (body.username,)
        ).fetchone()
        valid = verify_password(
            body.password, row["password"] if row else DUMMY_PASSWORD_HASH
        )
        failed = not row or not row["enabled"] or not valid
        if failed:
            db.execute("INSERT INTO attempts VALUES(?,?)", (peer, now))
        else:
            raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (token_hash(raw), row["id"], csrf, now + 43200),
            )
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute("DELETE FROM attempts WHERE peer=?", (peer,))
    if failed:
        store.audit(body.username, "login.failed", peer)
        raise HTTPException(401, "Invalid username or password")
    response.set_cookie(
        "vmm_session",
        raw,
        max_age=43200,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    store.audit(row["username"], "login", peer)
    return {"username": row["username"], "role": row["role"], "csrf": csrf}


@app.get("/api/me")
def me(user=Depends(require())):
    return {k: user[k] for k in ("id", "username", "role", "csrf")}


@app.post("/api/logout")
def logout(response: Response, user=Depends(require())):
    with store.db() as db:
        db.execute("DELETE FROM sessions WHERE token=?", (user["token"],))
    response.delete_cookie("vmm_session")
    return {"ok": True}


class Password(BaseModel):
    current: str = Field(max_length=256)
    password: str = Field(min_length=12, max_length=256)


@app.put("/api/password")
def password(body: Password, user=Depends(require())):
    with store.db() as db:
        row = db.execute(
            "SELECT password FROM users WHERE id=?", (user["id"],)
        ).fetchone()
        if not verify_password(body.current, row["password"]):
            raise HTTPException(400, "Current password is incorrect")
        db.execute(
            "UPDATE users SET password=? WHERE id=?",
            (hash_password(body.password), user["id"]),
        )
        db.execute(
            "DELETE FROM sessions WHERE user_id=? AND token<>?",
            (user["id"], user["token"]),
        )
    (DATA / "initial-admin.txt").unlink(missing_ok=True)
    store.audit(user["username"], "password.changed")
    return {"ok": True}


def inventory():
    with cache_lock:
        if cache["data"] and time.monotonic() - cache["at"] < 5:
            return cache["data"]
        vms, nets, pools, isos, warnings = [], [], [], [], []
        for label, fn, destination in [
            ("Incus VMs", incus.vms, vms),
            ("libvirt VMs", libvirt.vms, vms),
            ("Incus networks", incus.networks, nets),
            ("libvirt networks", libvirt.networks, nets),
            ("Storage", incus.pools, pools),
            ("ISO volumes", incus.isos, isos),
        ]:
            try:
                destination.extend(fn())
            except Exception as exc:
                warnings.append(label + ": " + str(exc)[:300])
        with store.db() as db:
            pending_rows = db.execute(
                "SELECT * FROM pending_hardware ORDER BY created,id"
            ).fetchall()
        for vm in vms:
            vm["pending"] = [
                dict(r, body=json.loads(r["body"]))
                for r in pending_rows
                if r["vm_id"] == vm["id"]
            ]
        for net in nets:
            if net["provider"] == "libvirt":
                net["used_by"] = [
                    vm["id"]
                    for vm in vms
                    if vm["provider"] == "libvirt"
                    and any(
                        d.get("network") == net["name"] for d in vm["devices"].values()
                    )
                ]
        for net in nets:
            net.setdefault("used_by", []).extend(
                "pending:" + r["vm_id"]
                for r in pending_rows
                if json.loads(r["body"]).get("network") == net["name"]
            )
        for iso in local_isos():
            iso["used_by"] = [
                vm["id"]
                for vm in vms
                if any(d.get("source") == iso["path"] for d in vm["devices"].values())
            ]
            isos.append(iso)
        for iso in isos:
            iso.setdefault("used_by", []).extend(
                "pending:" + r["vm_id"]
                for r in pending_rows
                if json.loads(r["body"]).get("iso_id") == iso["id"]
            )
        memory = psutil.virtual_memory()
        disk = shutil.disk_usage(DATA)
        cache["data"] = {
            "vms": vms,
            "networks": nets,
            "pools": pools,
            "isos": isos,
            "warnings": warnings,
            "host": {
                "name": socket.gethostname(),
                "address": os.environ.get("VM_MANAGER_HOST", "192.168.8.235"),
                "cpu_count": psutil.cpu_count(),
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_total": memory.total,
                "memory_used": memory.total - memory.available,
                "memory_percent": memory.percent,
                "load": list(os.getloadavg()),
                "disk_total": disk.total,
                "disk_free": disk.free,
                "uptime": time.time() - psutil.boot_time(),
            },
            "updated": time.time(),
        }
        cache["at"] = time.monotonic()
        return cache["data"]


def local_isos():
    with store.db() as db:
        return [
            dict(r, source="upload", pool=None)
            for r in db.execute("SELECT * FROM isos ORDER BY created DESC")
        ]


def get_iso(iso_id):
    if not iso_id:
        return None
    for iso in inventory()["isos"]:
        if iso["id"] == iso_id:
            return iso
    raise HTTPException(404, "ISO not found")


def vm_ref(vm_id):
    provider, sep, vm_name = vm_id.partition(":")
    if not sep or provider not in ("incus", "libvirt"):
        raise HTTPException(400, "Invalid VM reference")
    name(vm_name)
    return provider, vm_name


def ensure_stopped(provider, vm_name):
    if provider == "libvirt":
        libvirt.stopped(vm_name)
    elif incus.vm(vm_name)["status"].lower() != "stopped":
        raise HypervisorError("Shut down the VM before editing its hardware.")


@app.get("/api/inventory")
def get_inventory(user=Depends(require())):
    result = organization.decorate(store, inventory())
    try:
        blocks = storage.broker("list")
        error = ""
    except HypervisorError as exc:
        blocks, error = [], str(exc)
    return dict(result, storage_blocks=blocks, storage_defaults=storage.defaults(store),
                storage_error=error, backup_plans=backups.plans(store), vm_backups=backups.catalog(store))


@app.post("/api/storage/blocks")
def create_storage_block(body: storage.BlockCreate, user=Depends(require("operate"))):
    def create():
        with storage.lock:
            return storage.broker("create", body=body.model_dump())
    return task(user, "storage.create", body.name, create)


@app.put("/api/storage/blocks/{block_id}")
def grow_storage_block(block_id: str, body: storage.BlockSize, user=Depends(require("operate"))):
    def grow():
        with storage.lock:
            return storage.broker("grow", block_id=block_id, body=body.model_dump())
    return task(user, "storage.grow", block_id, grow)


@app.put("/api/storage/defaults/{purpose}")
def set_storage_default(purpose: storage.Purpose, body: storage.DefaultBlock,
                        user=Depends(require("operate"))):
    with storage.lock:
        if body.block_id:
            block = next((b for b in storage.broker("list") if b["id"] == body.block_id), None)
            if not block or block["purpose"] != purpose or block["status"] != "ready":
                raise HTTPException(400, "Choose an available block with the matching purpose")
        with store.db() as db:
            db.execute("INSERT INTO storage_defaults VALUES(?,?) ON CONFLICT(purpose) DO UPDATE SET block_id=excluded.block_id",
                       (purpose, body.block_id))
        store.audit(user["username"], "storage.default", purpose, body.block_id or "Original storage")
    invalidate()
    return {"ok": True}


class StorageDelete(BaseModel):
    confirm_name: str


@app.delete("/api/storage/blocks/{block_id}")
def delete_storage_block(block_id: str, body: StorageDelete, user=Depends(require("operate"))):
    def delete():
        with storage.lock:
            block = next((b for b in storage.broker("list") if b["id"] == block_id), None)
            if not block or body.confirm_name != block["name"]:
                raise HypervisorError("Type the storage block name to confirm deletion")
            if block_id in storage.defaults(store).values():
                raise HypervisorError("Change the default storage assignment before deleting this block")
            with store.db() as db:
                if any(json.loads(r[0]).get('block_id') == block_id for r in db.execute('SELECT settings FROM backup_plans')):
                    raise HypervisorError('Storage block is referenced by VM backup settings')
                if db.execute('SELECT 1 FROM vm_backups WHERE block_id=?', (block_id,)).fetchone():
                    raise HypervisorError('Storage block contains VM backup records')
                if any(json.loads(r[0]).get("pool") == block["pool"] for r in
                       db.execute("SELECT body FROM pending_hardware")) and block["pool"]:
                    raise HypervisorError("Storage block is referenced by pending hardware changes")
                if block["path"] and db.execute("SELECT 1 FROM isos WHERE path LIKE ?",
                                                 (block["path"] + "/%",)).fetchone():
                    raise HypervisorError("Storage block contains ISO records")
            return storage.broker("delete", block_id=block_id)
    return task(user, "storage.delete", block_id, delete)


@app.get("/api/collections")
def get_collections(user=Depends(require())):
    return organization.collections(store)


@app.post("/api/collections/{kind}")
def create_collection(
    kind: organization.Collection,
    body: organization.CollectionName,
    user=Depends(require("operate")),
):
    result = organization.create_collection(store, kind, body)
    store.audit(user["username"], kind + ".create", result["id"], result["name"])
    return result


@app.put("/api/collections/{kind}/{collection_id}")
def rename_collection(
    kind: organization.Collection,
    collection_id: str,
    body: organization.CollectionName,
    user=Depends(require("operate")),
):
    # Check and update under one transaction so a concurrent delete cannot recreate it.
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            f"SELECT 1 FROM {kind} WHERE id=?", (collection_id,)
        ).fetchone():
            raise HTTPException(404, "Item not found")
        try:
            db.execute(
                f"UPDATE {kind} SET name=?,name_key=? WHERE id=?",
                (body.name, body.name.casefold(), collection_id),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "A name like this already exists") from exc
    store.audit(user["username"], kind + ".rename", collection_id, body.name)
    return {"id": collection_id, "name": body.name}


@app.delete("/api/collections/{kind}/{collection_id}")
def delete_collection(
    kind: organization.Collection, collection_id: str, user=Depends(require("operate"))
):
    with store.db() as db:
        if not db.execute(f"DELETE FROM {kind} WHERE id=?", (collection_id,)).rowcount:
            raise HTTPException(404, "Item not found")
    store.audit(user["username"], kind + ".delete", collection_id)
    return {"ok": True}


@app.put("/api/organization/{kind}/{resource_id}")
def assign_resource(
    kind: organization.ResourceKind,
    resource_id: str,
    body: organization.Assignment,
    user=Depends(require("operate")),
):
    key = "vms" if kind == "vm" else "networks"
    if not any(r["id"] == resource_id for r in inventory()[key]):
        raise HTTPException(404, "Resource not found")
    organization.assign(store, kind, resource_id, body)
    store.audit(
        user["username"],
        "organization.assign",
        kind + ":" + resource_id,
        body.model_dump_json(),
    )
    return {"ok": True}


class VMCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    cpu: int = Field(ge=1, le=128, default=2)
    memory_gib: float = Field(ge=0.5, le=256, default=4)
    disk_gib: int = Field(ge=4, le=4096, default=32)
    pool: str = Field(min_length=1, max_length=63)
    network: str = Field(min_length=1, max_length=63)
    iso_id: str = Field(default="", max_length=256)
    secure_boot: bool = False
    autostart: bool = False
    description: str = Field(default="", max_length=1000)
    extra_disks: list[int] = Field(default_factory=list, max_length=8)

    @field_validator("extra_disks")
    @classmethod
    def disks_valid(cls, values):
        if any(v < 1 or v > 4096 for v in values):
            raise ValueError("Extra disks must be between 1 and 4096 GiB")
        return values

    @field_validator("name", "pool", "network")
    @classmethod
    def valid_name(cls, value):
        try:
            return name(value)
        except HypervisorError as e:
            raise ValueError(str(e)) from e


@app.post("/api/vms")
def create_vm(body: VMCreate, user=Depends(require("operate"))):
    iso = get_iso(body.iso_id)

    def create():
        storage.check_vm_pool(body.pool)
        inv = inventory()
        if any(v["name"] == body.name for v in inv["vms"]):
            raise HypervisorError("A VM with this name already exists")
        if body.pool not in [p["name"] for p in inv["pools"]]:
            raise HypervisorError("Storage pool not found")
        if body.network not in [
            n["name"] for n in inv["networks"] if n["provider"] == "incus"
        ]:
            raise HypervisorError("Managed network not found")
        if body.memory_gib * 1024**3 > psutil.virtual_memory().available:
            raise HypervisorError("Not enough available host memory")
        incus.create(body.model_dump(), iso)

    return task(user, "vm.create", body.name, create)


class VMEdit(BaseModel):
    cpu: int = Field(ge=1, le=128)
    memory_gib: float = Field(ge=0.5, le=256)
    description: str = Field(default="", max_length=1000)
    secure_boot: bool = False
    autostart: bool = False


@app.put("/api/vms/{vm_id}")
def edit_vm(vm_id: str, body: VMEdit, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)

    return hardware_task(user, vm_id, "resources", body)


def hardware_edit_vm(vm_id, body):
    provider, vm_name = vm_ref(vm_id)
    ensure_stopped(provider, vm_name)
    if provider == "libvirt":
        libvirt.edit_resources(vm_name, body.cpu, body.memory_gib, body.description)
    else:

        def transform(vm):
            vm["config"].update(
                {
                    "limits.cpu": str(body.cpu),
                    "limits.memory": str(int(body.memory_gib * 1024**3)),
                    "security.secureboot": str(body.secure_boot).lower(),
                    "boot.autostart": str(body.autostart).lower(),
                }
            )
            vm["description"] = body.description

        incus.update(vm_name, transform)


class Action(BaseModel):
    action: str
    confirm_name: str = ""


class BulkVMAction(BaseModel):
    vm_ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["shutdown", "restart", "stop", "delete"]
    confirmed: bool = False
    preserve_backups: list[str] = Field(default_factory=list, max_length=200)


@app.post("/api/vms/bulk/action")
def bulk_vm_action(body: BulkVMAction, user=Depends(require("operate"))):
    if not body.confirmed:
        raise HTTPException(400, "Confirm the bulk operation before continuing")
    vm_ids = list(dict.fromkeys(body.vm_ids))
    for vm_id in vm_ids:
        vm_ref(vm_id)
    if not set(body.preserve_backups).issubset(vm_ids) or (body.preserve_backups and body.action != "delete"):
        raise HTTPException(400, "Backup preservation applies only to VMs selected for deletion")
    known = {v["id"] for v in inventory()["vms"]}
    if any(vm_id not in known for vm_id in vm_ids):
        raise HTTPException(404, "A selected VM no longer exists; refresh the VM list")
    results = []
    for vm_id in vm_ids:
        if body.action == "delete":
            fn = lambda vm_id=vm_id: remove_vm(vm_id, cleanup=True, preserve_backups=vm_id in body.preserve_backups, actor=user['username'])
        else:
            cancelled = power_requests.register(vm_id, body.action)
            fn = lambda vm_id=vm_id, cancelled=cancelled: power_action(vm_id, body.action, cancelled)
        results.append(dict(vm_id=vm_id, **task(user, "vm." + body.action, vm_id, fn)))
    return {"tasks": results}


@app.post("/api/vms/{vm_id}/action")
def vm_action(vm_id: str, body: Action, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    if body.action not in ("start", "shutdown", "restart", "stop"):
        raise HTTPException(400, "Invalid power action")
    if body.action == "stop" and body.confirm_name != vm_name:
        raise HTTPException(400, "Type the VM name to confirm force stop")
    cancelled = power_requests.register(vm_id, body.action)

    return task(
        user,
        "vm." + body.action,
        vm_id,
        lambda: power_action(vm_id, body.action, cancelled),
    )


class Confirm(BaseModel):
    confirm_name: str


class SnapshotCreate(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,62}$")
    description: str = Field(default="", max_length=1000)
    mode: Literal["live", "shutdown", "stopped"] = "live"


@app.get("/api/vms/{vm_id}/snapshots")
def list_snapshots(vm_id: str, user=Depends(require())):
    provider, vm_name = vm_ref(vm_id)
    return snapshots.listing(provider, vm_name, vm_id)


@app.post("/api/vms/{vm_id}/snapshots")
def create_snapshot(vm_id: str, body: SnapshotCreate, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    cancelled = power_requests.register(vm_id, 'snapshot')

    def create():
        if pending_hardware(vm_id):
            raise HypervisorError("Apply or discard pending hardware changes before taking a snapshot.")
        with protection_power.operation(vm_id, body.mode, cancelled) as live:
            return snapshots.create(provider, vm_name, vm_id, body.name, body.description, live, cancelled)

    return task(user, "vm.snapshot.create", vm_id, create)


@app.post("/api/vms/{vm_id}/snapshots/{snapshot}/restore")
def restore_snapshot(vm_id: str, snapshot: str, body: Confirm, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    name(snapshot)
    if body.confirm_name != vm_name:
        raise HTTPException(400, "Type the VM name to confirm snapshot restore")

    def restore():
        if pending_hardware(vm_id):
            raise HypervisorError("Apply or discard pending hardware changes before restoring a snapshot.")
        return snapshots.restore(provider, vm_name, vm_id, snapshot)

    return task(user, "vm.snapshot.restore", vm_id, restore)


@app.delete("/api/vms/{vm_id}/snapshots/{snapshot}")
def delete_snapshot(vm_id: str, snapshot: str, body: Confirm, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    name(snapshot)
    if body.confirm_name != snapshot:
        raise HTTPException(400, "Type the snapshot name to confirm deletion")
    return task(user, "vm.snapshot.delete", vm_id,
                lambda: snapshots.delete(provider, vm_name, vm_id, snapshot))


@app.delete("/api/vms/{vm_id}")
def delete_vm(vm_id: str, body: Confirm, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    if body.confirm_name != vm_name:
        raise HTTPException(400, "Type the VM name to confirm deletion")

    return task(user, "vm.delete", vm_id, lambda: remove_vm(vm_id))


def remove_vm(vm_id, cleanup=False, preserve_backups=True, actor='system'):
    with backup_manager.lock:
        with store.db() as db:
            if db.execute("SELECT 1 FROM vm_backups WHERE vm_id=? AND status IN ('queued','running')", (vm_id,)).fetchone():
                raise HypervisorError("Wait for the VM's active backup to finish before deleting it.")
        deleting_vms.add(vm_id)
    try:
        return remove_vm_contents(vm_id, cleanup, preserve_backups, actor)
    finally:
        with backup_manager.lock:
            invalidate()
            deleting_vms.discard(vm_id)


def remove_vm_contents(vm_id, cleanup, preserve_backups, actor):
    provider, vm_name = vm_ref(vm_id)
    ensure_stopped(provider, vm_name)
    if cleanup:
        snapshots.guard_start(vm_id)
        snapshots.delete_all(provider, vm_name, vm_id)
        if not preserve_backups:
            with store.db() as db:
                backup_ids = [r['id'] for r in db.execute("SELECT id FROM vm_backups WHERE vm_id=?", (vm_id,))]
            for backup_id in backup_ids:
                backup_manager.delete(backup_id)
                store.audit(actor, 'backup.delete', backup_id, 'Bulk VM deletion: ' + vm_id)
    elif snapshots.rows(vm_id):
        raise HypervisorError("Delete the VM's snapshots first, newest first, before deleting the VM.")
    if provider == "incus":
        incus.delete(vm_name)
    else:
        # Existing VM storage is preserved; undefine never deletes a disk.
        libvirt.cli("undefine", vm_name, "--nvram")
    with store.db() as db:
        db.execute("DELETE FROM pending_hardware WHERE vm_id=?", (vm_id,))
        db.execute("DELETE FROM snapshot_heads WHERE vm_id=?", (vm_id,))
        db.execute("DELETE FROM backup_plans WHERE vm_id=?", (vm_id,))
        organization.clear(store, "vm", vm_id, db=db)
    return "VM deleted." + (" Backups preserved." if cleanup and preserve_backups else " Snapshots and backups deleted." if cleanup else "")


class Disk(BaseModel):
    size_gib: int = Field(ge=1, le=4096)
    pool: str = Field(min_length=1, max_length=63)


@app.post("/api/vms/{vm_id}/disks")
def add_disk(vm_id: str, body: Disk, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    name(body.pool)

    return hardware_task(user, vm_id, "disk.add", body)


def hardware_add_disk(vm_id, body):
    provider, vm_name = vm_ref(vm_id)
    ensure_stopped(provider, vm_name)
    disk_id = "disk" + uuid.uuid4().hex[:8]
    if provider == "incus":
        storage.check_vm_pool(body.pool)
        volume = "vmm-" + vm_name + "-" + disk_id
        incus.request(
            "POST",
            f"/1.0/storage-pools/{quote(body.pool)}/volumes/custom",
            {
                "name": volume,
                "type": "custom",
                "content_type": "block",
                "config": {"size": str(body.size_gib) + "GiB"},
            },
        )
        try:
            incus.update(
                vm_name,
                lambda vm: vm["devices"].update(
                    {disk_id: {"type": "disk", "pool": body.pool, "source": volume}}
                ),
            )
        except Exception:
            incus.request(
                "DELETE",
                f"/1.0/storage-pools/{quote(body.pool)}/volumes/custom/{volume}",
            )
            raise
    else:
        directory = DATA / "disks"
        directory.mkdir(exist_ok=True)
        path = directory / f"{vm_name}-{disk_id}.qcow2"
        run("qemu-img", "create", "-f", "qcow2", str(path), str(body.size_gib) + "G")
        root = libvirt.xml(vm_name)
        used = {n.get("dev") for n in root.findall("./devices/disk/target")}
        target = next(
            ("vd" + chr(c) for c in range(98, 123) if "vd" + chr(c) not in used),
            None,
        )
        if not target:
            path.unlink()
            raise HypervisorError("No available disk slots")
        disk = ET.SubElement(
            root.find("devices"), "disk", {"type": "file", "device": "disk"}
        )
        ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2"})
        ET.SubElement(disk, "source", {"file": str(path)})
        ET.SubElement(disk, "target", {"dev": target, "bus": "virtio"})
        try:
            libvirt.define(root)
        except Exception:
            path.unlink(missing_ok=True)
            raise


class DiskResize(BaseModel):
    size_gib: int = Field(ge=1, le=4096)


@app.put("/api/vms/{vm_id}/disks/{device}")
def resize_disk(
    vm_id: str, device: str, body: DiskResize, user=Depends(require("operate"))
):
    provider, vm_name = vm_ref(vm_id)
    name(device)

    return hardware_task(user, vm_id, "disk.resize", body, device)


def hardware_resize_disk(vm_id, body, device):
    provider, vm_name = vm_ref(vm_id)
    ensure_stopped(provider, vm_name)
    if provider != "incus":
        raise HypervisorError(
            "Resizing existing libvirt disks is not supported; add a disk instead"
        )
    vm = incus.vm(vm_name)
    d = vm.get("expanded_devices", vm["devices"]).get(device)
    if not d or d.get("type") != "disk" or d.get("readonly") == "true":
        raise HypervisorError("Writable disk not found")
    if d.get("path") == "/":
        volume = incus.get(
            f"/1.0/storage-pools/{quote(d['pool'])}/volumes/virtual-machine/{quote(vm_name)}"
        )
        previous = bytes_size(volume.get("config", {}).get("size", d.get("size", "0")))
        if body.size_gib * 1024**3 <= previous:
            raise HypervisorError("Disks can only be expanded. Choose a larger size.")
        incus.update(
            vm_name,
            lambda v: v["devices"].update(
                {device: {**d, "size": str(body.size_gib) + "GiB"}}
            ),
        )
    elif d.get("pool") and d.get("source"):
        current = incus.get(
            f"/1.0/storage-pools/{quote(d['pool'])}/volumes/custom/{quote(d['source'])}"
        )
        if body.size_gib * 1024**3 <= bytes_size(
            current.get("config", {}).get("size", "0")
        ):
            raise HypervisorError("Disks can only be expanded. Choose a larger size.")
        current["config"]["size"] = str(body.size_gib) + "GiB"
        incus.request(
            "PUT",
            f"/1.0/storage-pools/{quote(d['pool'])}/volumes/custom/{quote(d['source'])}",
            {
                "config": current["config"],
                "description": current.get("description", ""),
            },
        )
    else:
        raise HypervisorError("This disk cannot be resized from the app")


@app.delete("/api/vms/{vm_id}/devices/{device}")
def detach_device(
    vm_id: str, device: str, body: Confirm, user=Depends(require("operate"))
):
    provider, vm_name = vm_ref(vm_id)
    name(device)
    if body.confirm_name != device:
        raise HTTPException(400, "Type the device name to confirm detaching it")

    return hardware_task(user, vm_id, "device.detach", body, device)


def hardware_detach_device(vm_id, body, device):
    provider, vm_name = vm_ref(vm_id)
    ensure_stopped(provider, vm_name)
    if provider == "incus":

        def change(vm):
            d = vm.get("expanded_devices", vm["devices"]).get(device)
            if not d or d.get("path") == "/":
                raise HypervisorError("The root disk cannot be detached")
            if d.get("type") not in ("disk", "nic"):
                raise HypervisorError("Only disks and network adapters can be detached")
            if device in vm["devices"]:
                del vm["devices"][device]
            else:
                vm["devices"][device] = {"type": "none"}

        incus.update(vm_name, change)
    else:
        root = libvirt.xml(vm_name)
        devices = root.find("devices")
        if device.startswith("nic"):
            selected = devices.findall("interface")[int(device[3:])]
        else:
            selected = next(
                (
                    d
                    for d in devices.findall("disk")
                    if d.find("target") is not None
                    and d.find("target").get("dev") == device
                ),
                None,
            )
            if selected is None or selected is next(
                (d for d in devices.findall("disk") if d.get("device") == "disk"),
                None,
            ):
                raise HypervisorError("Disk not found or is the root disk")
        devices.remove(selected)
        libvirt.define(root)


class NIC(BaseModel):
    network: str = Field(min_length=1, max_length=63)
    device: str = Field(default="", max_length=63)


@app.post("/api/vms/{vm_id}/nics")
def edit_nic(vm_id: str, body: NIC, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    name(body.network)
    if body.device:
        name(body.device)

    return hardware_task(user, vm_id, "nic", body)


def hardware_edit_nic(vm_id, body):
    provider, vm_name = vm_ref(vm_id)
    ensure_stopped(provider, vm_name)
    networks = incus.networks()
    if body.network not in [n["name"] for n in networks]:
        raise HypervisorError("Incus network not found")
    if provider == "incus":

        def change(vm):
            key = body.device or "nic" + uuid.uuid4().hex[:8]
            if (
                body.device
                and vm.get("expanded_devices", vm["devices"]).get(key, {}).get("type")
                != "nic"
            ):
                raise HypervisorError("Network adapter not found")
            if any(
                k != key and d.get("type") == "nic" and d.get("network") == body.network
                for k, d in vm.get("expanded_devices", vm["devices"]).items()
            ):
                raise HypervisorError(
                    "This VM already has an adapter on that network. Choose a different network or edit the existing adapter."
                )
            vm["devices"][key] = {
                "type": "nic",
                "network": body.network,
                "name": key,
            }

        incus.update(vm_name, change)
    else:
        root = libvirt.xml(vm_name)
        nics = root.findall("./devices/interface")
        if body.device:
            index = int(body.device.removeprefix("nic"))
            if index >= len(nics):
                raise HypervisorError("Network adapter not found")
            nic = nics[index]
            for child in list(nic):
                if child.tag in ("source", "virtualport", "filterref"):
                    nic.remove(child)
            nic.set("type", "bridge")
        else:
            nic = ET.SubElement(root.find("devices"), "interface", {"type": "bridge"})
            ET.SubElement(nic, "model", {"type": "virtio"})
        ET.SubElement(nic, "source", {"bridge": body.network})
        libvirt.define(root)


class CDROM(BaseModel):
    iso_id: str = Field(default="", max_length=256)
    boot: bool = True


@app.put("/api/vms/{vm_id}/cdrom")
def cdrom(vm_id: str, body: CDROM, user=Depends(require("operate"))):
    provider, vm_name = vm_ref(vm_id)
    get_iso(body.iso_id)

    return hardware_task(user, vm_id, "cdrom", body)


def hardware_cdrom(vm_id, body):
    provider, vm_name = vm_ref(vm_id)
    iso = get_iso(body.iso_id)
    ensure_stopped(provider, vm_name)
    if provider == "incus":

        def change(vm):
            # Eject all CD-ROMs visible to this VM, including inherited ISO devices.
            for key, device in list(vm.get("expanded_devices", vm["devices"]).items()):
                if device.get("type") == "disk" and (
                    key == "cdrom"
                    or device.get("source", "").lower().endswith(".iso")
                    or any(
                        i.get("pool") == device.get("pool")
                        and i["name"] == device.get("source")
                        for i in inventory()["isos"]
                        if i.get("source") == "incus"
                    )
                ):
                    if key in vm["devices"]:
                        del vm["devices"][key]
                    else:
                        vm["devices"][key] = {"type": "none"}
            if iso:
                vm["devices"]["cdrom"] = incus.cdrom(iso, body.boot)
                if body.boot:
                    priorities = [
                        int(d.get("boot.priority", "0"))
                        for d in vm.get("expanded_devices", vm["devices"]).values()
                        if d.get("boot.priority", "0").lstrip("-").isdigit()
                    ]
                    vm["devices"]["cdrom"]["boot.priority"] = str(
                        max([0, *priorities]) + 10
                    )

        incus.update(vm_name, change)
    else:
        if iso and iso.get("source") != "upload":
            raise HypervisorError(
                "Upload the ISO to the library to attach it to a libvirt VM"
            )
        root = libvirt.xml(vm_name)
        devices = root.find("devices")
        for old in list(devices.findall("disk")):
            if old.get("device") == "cdrom":
                devices.remove(old)
        # Use OS boot ordering; remove per-device boot order to avoid mixed schema modes.
        for parent in devices.findall("disk") + devices.findall("interface"):
            for boot in list(parent.findall("boot")):
                parent.remove(boot)
        os_node = root.find("os")
        for boot in list(os_node.findall("boot")):
            os_node.remove(boot)
        if iso:
            disk = ET.SubElement(devices, "disk", {"type": "file", "device": "cdrom"})
            ET.SubElement(disk, "driver", {"name": "qemu", "type": "raw"})
            ET.SubElement(disk, "source", {"file": iso["path"]})
            used = {n.get("dev") for n in devices.findall("disk/target")}
            target = next(
                ("sd" + chr(c) for c in range(97, 123) if "sd" + chr(c) not in used),
                None,
            )
            if not target:
                raise HypervisorError("No available CD-ROM slots")
            ET.SubElement(disk, "target", {"dev": target, "bus": "sata"})
            ET.SubElement(disk, "readonly")
            if body.boot:
                ET.SubElement(os_node, "boot", {"dev": "cdrom"})
        ET.SubElement(os_node, "boot", {"dev": "hd"})
        libvirt.define(root)


# Pending operations are application state, never applied to a running guest.
def pending_hardware(vm_id):
    with store.db() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM pending_hardware WHERE vm_id=? ORDER BY created,id",
                (vm_id,),
            )
        ]


def hardware_task(user, vm_id, kind, body, device=""):
    def save_or_apply():
        snapshots.guard_start(vm_id)
        provider, vm_name = vm_ref(vm_id)
        status = (
            incus.vm(vm_name)["status"].lower()
            if provider == "incus"
            else libvirt.cli("domstate", vm_name).strip()
        )
        if status in ("stopped", "shut off"):
            apply_pending(vm_id)
            apply_hardware(vm_id, kind, body.model_dump(), device)
            return
        if status != "running":
            raise HypervisorError(
                "Wait until the VM is running or stopped before changing hardware"
            )
        # Repeated edits to the same resource replace its planned value.
        with store.db() as db:
            if (
                kind == "resources"
                or kind == "cdrom"
                or kind in ("disk.resize", "device.detach")
                or (kind == "nic" and body.device)
            ):
                target = body.device if kind == "nic" else device
                for row in db.execute(
                    "SELECT * FROM pending_hardware WHERE vm_id=? AND kind=?",
                    (vm_id, kind),
                ).fetchall():
                    previous_target = (
                        json.loads(row["body"]).get("device", "")
                        if kind == "nic"
                        else row["device"]
                    )
                    if previous_target == target:
                        db.execute(
                            "DELETE FROM pending_hardware WHERE id=?", (row["id"],)
                        )
            db.execute(
                "INSERT INTO pending_hardware VALUES(?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    vm_id,
                    kind,
                    json.dumps(body.model_dump()),
                    device,
                    time.time(),
                ),
            )

        return "Hardware changes saved for Restart in VM Manager or the next Start."

    return task(user, "vm.hardware." + kind, vm_id, save_or_apply)


def apply_hardware(vm_id, kind, body, device):
    handlers = {
        "resources": (hardware_edit_vm, VMEdit),
        "disk.add": (hardware_add_disk, Disk),
        "disk.resize": (hardware_resize_disk, DiskResize),
        "device.detach": (hardware_detach_device, Confirm),
        "nic": (hardware_edit_nic, NIC),
        "cdrom": (hardware_cdrom, CDROM),
    }
    handler, model = handlers[kind]
    args = (vm_id, model(**body))
    (
        handler(*args, device)
        if kind in ("disk.resize", "device.detach")
        else handler(*args)
    )


def apply_pending(vm_id):
    provider, vm_name = vm_ref(vm_id)
    ensure_stopped(provider, vm_name)
    for row in pending_hardware(vm_id):
        # Delete only successfully applied operations. A failed operation and all
        # following changes remain recoverable; the VM is not started on failure.
        apply_hardware(vm_id, row["kind"], json.loads(row["body"]), row["device"])
        with store.db() as db:
            db.execute("DELETE FROM pending_hardware WHERE id=?", (row["id"],))
        invalidate()


def power_action(vm_id, action, cancelled=None):
    provider, vm_name = vm_ref(vm_id)
    driver = incus if provider == "incus" else libvirt
    power_requests.check(cancelled)
    if action in ("shutdown", "restart"):
        try:
            driver.action(vm_name, "shutdown")
            deadline = time.monotonic() + 60
            while True:
                power_requests.check(cancelled)
                status = (
                    incus.vm(vm_name)["status"].lower()
                    if provider == "incus"
                    else libvirt.cli("domstate", vm_name).strip()
                )
                if status in ("stopped", "shut off"):
                    break
                if time.monotonic() >= deadline:
                    raise HypervisorError("Guest shutdown timed out")
                time.sleep(1)
        except PowerInterrupted:
            raise
        except HypervisorError as error:
            power_requests.check(cancelled)
            raise HypervisorError(
                "Guest did not shut down. Boot menus and unresponsive guests may ignore "
                "Shutdown; use Force stop to power off immediately (unsaved data may be lost). "
                "Pending hardware changes retained. " + str(error)
            ) from error
        if action == "shutdown":
            return
    with power_requests.transition(vm_id, cancelled):
        if action in ("start", "restart"):
            snapshots.guard_start(vm_id)
            if pending_hardware(vm_id):
                apply_pending(vm_id)
            power_requests.check(cancelled)
            driver.action(vm_name, "start")
        elif action == "stop":
            status = (
                incus.vm(vm_name)["status"].lower()
                if provider == "incus"
                else libvirt.cli("domstate", vm_name).strip()
            )
            if status not in ("stopped", "shut off"):
                driver.action(vm_name, "stop")


@app.delete("/api/vms/{vm_id}/pending")
def discard_pending(vm_id: str, user=Depends(require("operate"))):
    vm_ref(vm_id)

    def discard():
        with store.db() as db:
            db.execute("DELETE FROM pending_hardware WHERE vm_id=?", (vm_id,))

    return task(user, "vm.hardware.discard", vm_id, discard)


def pending_reference(field, value):
    with store.db() as db:
        return any(
            json.loads(r["body"]).get(field) == value
            for r in db.execute("SELECT body FROM pending_hardware")
        )


metric_samples = {}
metric_lock = threading.Lock()


def cpu_percentage(vm_id, counter, identity, cpus, now):
    with metric_lock:
        previous = metric_samples.get(vm_id)
        metric_samples[vm_id] = (counter, identity, now)
        # Bound state even if externally created/deleted VMs are sampled.
        for key, sample in list(metric_samples.items()):
            if now - sample[2] > 3600:
                del metric_samples[key]
    if (
        previous
        and previous[1] == identity
        and counter >= previous[0]
        and now > previous[2]
    ):
        return round(
            min(
                100,
                max(
                    0,
                    (counter - previous[0]) / (now - previous[2]) / max(cpus, 1) * 100,
                ),
            ),
            1,
        )
    return None


@app.get("/api/vms/{vm_id}/metrics")
def vm_metrics(vm_id: str, user=Depends(require())):
    provider, vm_name = vm_ref(vm_id)
    memory_used, memory_source, cpu_source, counter, identity = (
        None,
        None,
        None,
        None,
        None,
    )
    if provider == "incus":
        vm = incus.vm(vm_name)
        state = incus.get("/1.0/instances/" + quote(vm_name) + "/state")
        running = state.get("status", "").lower() == "running"
        config = vm.get("expanded_config", vm.get("config", {}))
        cpu_config = config.get("limits.cpu", "1")
        cpus = int(cpu_config) if cpu_config.isdigit() else cpu_count(cpu_config)
        memory_total = bytes_size(config.get("limits.memory", "0"))
        if running:
            memory = state.get("memory") or {}
            if memory.get("total", 0) > 0:
                memory_used = memory.get("usage")
                memory_total = memory["total"]
                memory_source = "Guest memory used"
            # State counters can be zero when no guest agent is installed (e.g. an ISO).
            try:
                process = psutil.Process(state["pid"])
                times = process.cpu_times()
                counter = times.user + times.system
                identity = process.create_time()
                cpu_source = "Host VM process"
                if memory_used is None:
                    memory_used = process.memory_info().rss
                    memory_source = "Host VM resident memory"
            except (psutil.Error, KeyError):
                cpu = state.get("cpu") or {}
                if cpu.get("usage", 0) > 0:
                    counter = cpu["usage"] / 1e9
                    identity = state.get("started_at")
                    cpu_source = "Guest CPU time"
    else:
        stats = libvirt.cli(
            "domstats",
            vm_name,
            "--cpu-total",
            "--balloon",
            "--vcpu",
            "--state",
            "--raw",
        )
        values = dict(
            line.strip().split("=", 1) for line in stats.splitlines() if "=" in line
        )
        running = values.get("state.state") == "1"
        cpus = int(values.get("vcpu.current", "1"))
        memory_total = int(values.get("balloon.current", "0")) * 1024
        if running:
            counter = int(values["cpu.time"]) / 1e9 if "cpu.time" in values else None
            identity = "libvirt"
            cpu_source = "Host VM CPU time"
            if "balloon.available" in values and "balloon.unused" in values:
                memory_used = (
                    max(
                        0,
                        int(values["balloon.available"])
                        - int(values["balloon.unused"]),
                    )
                    * 1024
                )
                memory_source = "Guest memory used"
            elif "balloon.rss" in values:
                memory_used = int(values["balloon.rss"]) * 1024
                memory_source = "Host VM resident memory"
    now = time.monotonic()
    if not running:
        with metric_lock:
            metric_samples.pop(vm_id, None)
    cpu_percent = (
        cpu_percentage(vm_id, counter, identity, cpus, now)
        if running and counter is not None
        else None
    )
    return {
        "vm_id": vm_id,
        "running": running,
        "cpu_percent": cpu_percent,
        "cpu_count": cpus,
        "cpu_source": cpu_source,
        "memory_used": memory_used,
        "memory_total": memory_total,
        "memory_source": memory_source,
        "updated": time.time(),
    }


def cpu_count(value):
    if value.startswith("sockets="):
        count = 1
        for part in value.split(","):
            count *= int(part.split("=")[1])
        return count
    try:
        return sum(
            int(part.split("-")[-1]) - int(part.split("-")[0]) + 1
            for part in value.split(",")
        )
    except ValueError:
        return psutil.cpu_count() or 1


class Network(BaseModel):
    name: str = Field(min_length=1, max_length=15)
    cidr: str = Field(default="auto", max_length=32)
    nat: bool = True
    dhcp: bool = True
    description: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def valid_network(cls, value):
        try:
            return name(value)
        except HypervisorError as e:
            raise ValueError(str(e)) from e


def network_config(body, exclude=""):
    existing = []
    for net in inventory()["networks"]:
        if net["id"] == "incus:" + exclude:
            continue
        try:
            existing.append(
                ipaddress.ip_network(
                    net["config"].get("ipv4.address", "none"), strict=False
                )
            )
        except ValueError:
            pass
    for iface, addresses in psutil.net_if_addrs().items():
        if iface == exclude:
            continue
        for address in addresses:
            if address.family == socket.AF_INET and address.netmask:
                existing.append(
                    ipaddress.ip_network(
                        address.address + "/" + address.netmask, strict=False
                    )
                )
    cidr = body.cidr
    if cidr == "auto":
        cidr = next(
            (
                f"10.200.{i}.1/24"
                for i in range(1, 255)
                if not any(
                    ipaddress.ip_network(f"10.200.{i}.0/24").overlaps(n)
                    for n in existing
                )
            ),
            None,
        )
        if not cidr:
            raise HypervisorError("No free subnet found; specify one manually")
    try:
        interface = ipaddress.ip_interface(cidr)
    except ValueError as exc:
        raise HypervisorError(
            "Enter an IPv4 gateway and prefix, for example 10.200.10.1/24"
        ) from exc
    if (
        interface.version != 4
        or not any(
            interface.ip in ipaddress.ip_network(c)
            for c in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        or interface.network.prefixlen < 16
        or interface.network.prefixlen > 29
        or interface.ip
        in (interface.network.network_address, interface.network.broadcast_address)
    ):
        raise HypervisorError("Use a private IPv4 gateway with a /16 to /29 prefix")
    if any(interface.network.overlaps(n) for n in existing):
        raise HypervisorError("This subnet overlaps an existing host or VM network")
    return {
        "ipv4.address": str(interface),
        "ipv4.nat": str(body.nat).lower(),
        "ipv4.dhcp": str(body.dhcp).lower(),
        "ipv6.address": "none",
        "ipv4.routing": str(body.nat).lower(),
    }


@app.post("/api/networks")
def create_network(body: Network, user=Depends(require("operate"))):
    def create():
        incus.request(
            "POST",
            "/1.0/networks",
            {
                "name": body.name,
                "type": "bridge",
                "description": body.description,
                "config": network_config(body),
            },
        )

    return task(user, "network.create", body.name, create)


@app.put("/api/networks/{net_name}")
def edit_network(net_name: str, body: Network, user=Depends(require("operate"))):
    name(net_name)

    def edit():
        net = incus.get("/1.0/networks/" + quote(net_name))
        if net.get("used_by") or pending_reference("network", net_name):
            raise HypervisorError(
                "Detach VMs and profiles before changing this network"
            )
        incus.request(
            "PUT",
            "/1.0/networks/" + quote(net_name),
            {
                "description": body.description,
                "config": {**net["config"], **network_config(body, net_name)},
            },
        )

    return task(user, "network.edit", net_name, edit)


@app.delete("/api/networks/{net_name}")
def delete_network(net_name: str, body: Confirm, user=Depends(require("operate"))):
    name(net_name)
    if body.confirm_name != net_name:
        raise HTTPException(400, "Type the network name to confirm deletion")

    def delete():
        if pending_reference("network", net_name):
            raise HypervisorError(
                "Network is referenced by pending VM hardware changes"
            )
        if incus.get("/1.0/networks/" + quote(net_name)).get("used_by"):
            raise HypervisorError(
                "Network is in use. Detach its VMs and profiles first."
            )
        incus.request("DELETE", "/1.0/networks/" + quote(net_name))
        organization.clear(store, "network", "incus:" + net_name)

    return task(user, "network.delete", net_name, delete)


@app.post("/api/isos")
def upload_iso(file: UploadFile, storage_id: str | None = Form(default=None), user=Depends(require("operate"))):
    # Serialize uploads with storage deletion and default changes, so a mounted
    # destination cannot disappear between validation and the final rename.
    with storage.lock:
        try:
            directory = storage.directory(store, DATA, "iso", "" if storage_id == "application" else storage_id)
            return save_iso(file, directory, user)
        finally:
            file.file.close()


def save_iso(file, directory, user):
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".iso") or len(filename) > 180:
        raise HTTPException(400, "Choose an .iso file with a name under 180 characters")
    with store.db() as db:
        if db.execute("SELECT 1 FROM isos WHERE name=?", (filename,)).fetchone():
            raise HTTPException(409, "An ISO with this name already exists")
    iso_id = uuid.uuid4().hex
    partial = directory / (iso_id + ".part")
    destination = directory / (iso_id + ".iso")
    size, digest = 0, hashlib.sha256()
    try:
        with partial.open("xb") as stream:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 32 * 1024**3 or shutil.disk_usage(directory).free < min(2 * 1024**3, shutil.disk_usage(directory).total // 20):
                    raise HTTPException(
                        413, "ISO exceeds the 32 GiB limit or selected storage reserve"
                    )
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        with partial.open("rb") as stream:
            stream.seek(32768)
            descriptors = stream.read(256 * 1024)
        if not any(
            signature in descriptors for signature in (b"CD001", b"NSR02", b"NSR03")
        ):
            raise HTTPException(
                400, "This file does not contain an ISO9660 or UDF filesystem"
            )
        partial.rename(destination)
        destination.chmod(0o644)
        with store.db() as db:
            db.execute(
                "INSERT INTO isos VALUES(?,?,?,?,?,?)",
                (
                    iso_id,
                    filename,
                    str(destination),
                    size,
                    digest.hexdigest(),
                    time.time(),
                ),
            )
    except OSError as exc:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(507, "Selected storage block is full") from exc
        raise
    except Exception:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()
    invalidate()
    store.audit(
        user["username"],
        "iso.upload",
        filename,
        f"{size} bytes; SHA256 {digest.hexdigest()}",
    )
    return {"id": iso_id, "name": filename, "size": size, "sha256": digest.hexdigest()}


@app.delete("/api/isos/{iso_id:path}")
def delete_iso(iso_id: str, body: Confirm, user=Depends(require("operate"))):
    iso = get_iso(iso_id)
    if body.confirm_name != iso["name"]:
        raise HTTPException(400, "Type the ISO name to confirm deletion")

    def delete():
        if pending_reference("iso_id", iso_id):
            raise HypervisorError("ISO is referenced by pending VM hardware changes")
        invalidate()
        inv = inventory()
        if inv["warnings"]:
            raise HypervisorError(
                "Cannot verify ISO usage while a hypervisor is unavailable"
            )
        current = next((i for i in inv["isos"] if i["id"] == iso_id), None)
        if not current:
            raise HypervisorError("ISO no longer exists")
        if current.get("used_by"):
            raise HypervisorError("ISO is attached to a VM. Eject it first.")
        if iso["source"] == "incus":
            incus.request(
                "DELETE",
                f"/1.0/storage-pools/{quote(iso['pool'])}/volumes/custom/{quote(iso['name'])}",
            )
        else:
            Path(iso["path"]).unlink()
            with store.db() as db:
                db.execute("DELETE FROM isos WHERE id=?", (iso_id,))

    return task(user, "iso.delete", iso["name"], delete)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: str

    @field_validator("role")
    @classmethod
    def role_valid(cls, value):
        if value not in ROLES:
            raise ValueError("Choose administrator, operator or viewer")
        return value


@app.get("/api/users")
def users(user=Depends(require("users"))):
    with store.db() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT id,username,role,enabled,created FROM users ORDER BY username"
            )
        ]


@app.post("/api/users")
def create_user(body: UserCreate, user=Depends(require("users"))):
    import sqlite3

    try:
        with store.db() as db:
            db.execute(
                "INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)",
                (body.username, hash_password(body.password), body.role, time.time()),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username already exists")
    store.audit(user["username"], "user.create", body.username, body.role)
    return {"ok": True}


class UserEdit(BaseModel):
    role: str
    enabled: bool = True
    password: str = Field(default="", max_length=256)


@app.put("/api/users/{user_id}")
def edit_user(user_id: int, body: UserEdit, user=Depends(require("users"))):
    if body.role not in ROLES or body.password and len(body.password) < 12:
        raise HTTPException(400, "Invalid role or password shorter than 12 characters")
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if user_id == user["id"] and (not body.enabled or body.role != "administrator"):
            raise HTTPException(
                400, "You cannot disable or demote your own administrator account"
            )
        admins = db.execute(
            "SELECT count(*) FROM users WHERE enabled=1 AND role='administrator'"
        ).fetchone()[0]
        if (
            target["role"] == "administrator"
            and target["enabled"]
            and admins <= 1
            and (body.role != "administrator" or not body.enabled)
        ):
            raise HTTPException(400, "Keep at least one enabled administrator")
        db.execute(
            "UPDATE users SET role=?,enabled=? WHERE id=?",
            (body.role, int(body.enabled), user_id),
        )
        if body.password:
            db.execute(
                "UPDATE users SET password=? WHERE id=?",
                (hash_password(body.password), user_id),
            )
        db.execute(
            "DELETE FROM sessions WHERE user_id=? AND token<>?",
            (user_id, user["token"]),
        )
    store.audit(
        user["username"],
        "user.edit",
        target["username"],
        f"role={body.role}, enabled={body.enabled}",
    )
    return {"ok": True}


@app.get("/api/tasks")
def tasks(user=Depends(require())):
    with store.db() as db:
        return [
            dict(r)
            for r in db.execute("SELECT * FROM tasks ORDER BY started DESC LIMIT 100")
        ]


@app.get("/api/audit")
def audit(user=Depends(require("users"))):
    with store.db() as db:
        return [
            dict(r)
            for r in db.execute("SELECT * FROM audit ORDER BY at DESC LIMIT 200")
        ]


CONSOLE_AUTH_INTERVAL = 20
console_groups = {}
console_lock = asyncio.Lock()


async def incus_console_group(user, vm_name):
    key = (user["token"], vm_name)
    async with console_lock:
        group = console_groups.get(key)
        if not group:
            operation = await asyncio.to_thread(
                incus.request,
                "POST",
                "/1.0/instances/" + quote(vm_name) + "/console",
                {"type": "vga"},
                False,
            )
            operation_url = urlsplit(operation["operation"])
            base = "ws://incus" + operation_url.path + "/websocket?"
            suffix = "&" + operation_url.query if operation_url.query else ""
            fds = operation["metadata"]["metadata"]["fds"]
            try:
                control = await unix_connect(
                    incus.socket,
                    uri=base + "secret=" + fds["control"] + suffix,
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                )
            except Exception:
                await asyncio.to_thread(
                    incus.request, "DELETE", operation["operation"], None, False
                )
                raise
            group = {
                "operation": operation,
                "control": control,
                "uri": base + "secret=" + fds["0"] + suffix,
                "refs": 0,
            }
            console_groups[key] = group
        group["refs"] += 1
        return key, group


async def release_console_group(key, group):
    async with console_lock:
        group["refs"] -= 1
        if group["refs"] <= 0 and console_groups.get(key) is group:
            del console_groups[key]
            await group["control"].close()
            try:
                await asyncio.to_thread(
                    incus.request,
                    "DELETE",
                    group["operation"]["operation"],
                    None,
                    False,
                )
            except Exception:
                pass


@app.websocket("/api/console/{vm_id}")
async def console(ws: WebSocket, vm_id: str):
    group = None
    remote = None
    writer = None
    pending = []
    try:
        user = session(ws)
        if "operate" not in ROLES.get(user["role"], set()):
            await ws.close(code=4403)
            return
        origin = ws.headers.get("origin", "")
        expected = (
            ("https" if ws.url.scheme == "wss" else "http")
            + "://"
            + ws.headers.get("host", "")
        )
        if origin != expected or not secrets.compare_digest(
            ws.query_params.get("csrf", ""), user["csrf"]
        ):
            await ws.close(code=4403)
            return
        provider, vm_name = vm_ref(vm_id)
        if provider == "incus":
            # All SPICE channels share one Incus operation and control connection.
            key, group = await incus_console_group(user, vm_name)
            remote = await unix_connect(
                incus.socket,
                uri=group["uri"],
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            )
        else:
            display = await asyncio.to_thread(libvirt.cli, "domdisplay", vm_name)
            url = urlparse(display)
            if url.scheme != "vnc" or url.hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                raise HypervisorError("This VM needs a local VNC display configured")
            reader, writer = await asyncio.open_connection(
                url.hostname, 5900 + (url.port or 0)
            )
        await ws.accept(
            subprotocol=(
                "binary"
                if "binary" in ws.headers.get("sec-websocket-protocol", "")
                else None
            )
        )
        store.audit(user["username"], "console.open", vm_id)

        async def browser_to_vm():
            while True:
                session(ws)
                data = await ws.receive_bytes()
                if remote:
                    await remote.send(data)
                else:
                    writer.write(data)
                    await writer.drain()

        async def vm_to_browser():
            while True:
                session(ws)
                chunk = await remote.recv() if remote else await reader.read(65536)
                if not chunk:
                    return
                await ws.send_bytes(chunk)

        async def validate_session():
            while True:
                await asyncio.sleep(CONSOLE_AUTH_INTERVAL)
                current = session(ws)
                if "operate" not in ROLES.get(current["role"], set()):
                    raise HTTPException(403, "Console permission revoked")

        pending = [
            asyncio.create_task(validate_session()),
            asyncio.create_task(browser_to_vm()),
            asyncio.create_task(vm_to_browser()),
        ]
        done, remaining = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED
        )
        for completed in done:
            completed.result()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as exc:
        logging.info("Console unavailable for %s: %s", vm_id, type(exc).__name__)
        try:
            await ws.close(
                code=1011,
                reason="Console unavailable. Check VM power, active console connections and session.",
            )
        except RuntimeError:
            pass
    finally:
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if remote:
            await remote.close()
        if writer:
            writer.close()
            await writer.wait_closed()
        if group:
            await release_console_group(key, group)


protection_power = ProtectionPower(incus, libvirt, power_requests, power_action)
snapshots.power = protection_power
backup_manager = backups.Manager(store, DATA, incus, libvirt, power_requests, power_action)


def check_backup_vms(vm_ids):
    for vm_id in set(vm_ids):
        provider, vm_name = vm_ref(vm_id)
        if provider == 'incus':
            if incus.vm(vm_name).get('type') != 'virtual-machine':
                raise HypervisorError('Only virtual machines can be backed up')
        else:
            libvirt.xml(vm_name)


@app.get('/api/backups')
def list_backups(user=Depends(require())):
    return {'backups': backups.catalog(store), 'plans': backups.plans(store)}


def queue_backups(user, vm_ids, block_id, shutdown=False, trigger='manual', keep=None, mode=None):
    with backup_manager.lock:
        if deleting_vms.intersection(vm_ids):
            raise HypervisorError('A selected VM is being deleted; wait for deletion to finish')
        check_backup_vms(vm_ids)
        now = time.time()
        rows = [(uuid.uuid4().hex, vm_id) for vm_id in sorted(set(vm_ids))]
        try:
            with store.db() as db:
                db.executemany("INSERT INTO vm_backups(id,vm_id,block_id,status,created,trigger) VALUES(?,?,?,'queued',?,?)",
                               [(bid, vm_id, block_id, now, trigger) for bid, vm_id in rows])
        except sqlite3.IntegrityError as exc:
            raise HypervisorError('A selected VM already has a queued or running backup') from exc
        result = []
        for backup_id, vm_id in rows:
            cancelled = power_requests.register(vm_id, 'backup')
            def create(backup_id=backup_id, vm_id=vm_id, cancelled=cancelled):
                try:
                    snapshots.guard_start(vm_id)
                except HypervisorError as exc:
                    backup_manager.update(backup_id, 'failed', str(exc))
                    raise
                message = backup_manager.create(backup_id, shutdown, cancelled, mode)
                if trigger == 'scheduled' and keep is not None:
                    backup_manager.retain(vm_id, block_id, keep)
                return message
            queued = task(user, 'backup.create', vm_id, create)
            with store.db() as db:
                db.execute('UPDATE vm_backups SET task_id=? WHERE id=?', (queued['task_id'], backup_id))
            result.append(dict(queued, backup_id=backup_id, vm_id=vm_id))
        return {'tasks': result}


@app.post('/api/backups')
def create_backups(body: backups.BackupRequest, user=Depends(require('operate'))):
    check_backup_vms(body.vm_ids)
    with storage.lock:
        backups.destination(store, DATA, body.block_id)
    return queue_backups(user, body.vm_ids, body.block_id, body.shutdown, mode=body.mode)


@app.put('/api/backups/plans')
def save_backup_plans(body: backups.PlanRequest, user=Depends(require('operate'))):
    check_backup_vms(body.vm_ids)
    plan = backups.Plan.model_validate(body.model_dump())
    with backup_manager.lock, storage.lock:
        backups.destination(store, DATA, plan.block_id)
        with store.db() as db:
            for vm_id in set(body.vm_ids):
                db.execute('INSERT INTO backup_plans(vm_id,settings,next_run) VALUES(?,?,?) ON CONFLICT(vm_id) DO UPDATE SET settings=excluded.settings,next_run=excluded.next_run',
                           (vm_id, plan.model_dump_json(), backups.next_run(plan, time.time()) if plan.enabled else None))
                store.audit(user['username'], 'backup.plan', vm_id, plan.model_dump_json(), db=db)
    return {'ok': True}


@app.delete('/api/backups/plans/{vm_id}')
def remove_backup_plan(vm_id: str, user=Depends(require('operate'))):
    vm_ref(vm_id)
    with backup_manager.lock, store.db() as db:
        db.execute('DELETE FROM backup_plans WHERE vm_id=?', (vm_id,))
        store.audit(user['username'], 'backup.plan.remove', vm_id, db=db)
    return {'ok': True}


@app.post('/api/backups/{backup_id}/restore')
def restore_backup(backup_id: str, body: backups.Restore, user=Depends(require('operate'))):
    row = backup_manager.record(backup_id)
    if row['status'] != 'completed':
        raise HypervisorError('Only completed backups can be restored')
    def restore():
        try:
            return backup_manager.restore(backup_id, body)
        except Exception as exc:
            raise HypervisorError(str(exc) + '. Inspect destination ' + body.name + ' for partial resources before retrying; source VM and backup are retained.') from exc
    return task(user, 'backup.restore', row['vm_id'] + ' → ' + body.name, restore,
                resources={row['vm_id'], row['vm_id'].split(':', 1)[0] + ':' + body.name})


@app.delete('/api/backups/{backup_id}')
def delete_backup(backup_id: str, body: Confirm, user=Depends(require('operate'))):
    row = backup_manager.record(backup_id)
    if body.confirm_name != row['vm_id'].split(':', 1)[1]:
        raise HTTPException(400, 'Type the source VM name to delete this backup')
    return task(user, 'backup.delete', row['vm_id'], lambda: backup_manager.delete(backup_id))


def backup_tick(now=None):
    now = time.time() if now is None else now
    with backup_manager.lock:
        for entry in backups.plans(store):
            if not entry['enabled'] or entry['next_run'] is None or entry['next_run'] > now:
                continue
            plan = backups.Plan.model_validate(entry)
            # Persist the next calendar slot before dispatch. After downtime,
            # run once, then skip missed slots instead of flooding the queue.
            with store.db() as db:
                db.execute('UPDATE backup_plans SET next_run=? WHERE vm_id=?', (backups.next_run(plan, now), entry['vm_id']))
            try:
                queued = queue_backups({'username': 'scheduler'}, [entry['vm_id']], plan.block_id, plan.shutdown, 'scheduled', plan.keep, plan.mode)
                with store.db() as db:
                    db.execute('UPDATE backup_plans SET last_task=? WHERE vm_id=?', (queued['tasks'][0]['task_id'], entry['vm_id']))
            except Exception as exc:
                store.audit('scheduler', 'backup.schedule.failed', entry['vm_id'], str(exc))




STATIC = Path(__file__).parent / "static"
ASSET_VERSION = hashlib.sha256(b"".join(
    p.read_bytes() for p in sorted(STATIC.iterdir()) if p.suffix in (".js", ".css")
)).hexdigest()[:16]
# Relative module imports share the same versioned prefix, avoiding old modules
# in browser/CDN caches after a deployment.
app.mount("/assets/" + ASSET_VERSION, StaticFiles(directory=STATIC), name="assets")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
if Path("/usr/share/spice-html5").exists():
    app.mount(
        "/vendor/spice", StaticFiles(directory="/usr/share/spice-html5"), name="spice"
    )


@app.get("/")
def index():
    return frontend_page("index.html")


@app.get("/console")
def console_page(user=Depends(require("operate"))):
    return frontend_page("console.html")


def frontend_page(filename):
    html = (STATIC / filename).read_text().replace("/static/", "/assets/" + ASSET_VERSION + "/")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


from .limits import BodyLimit

app.add_middleware(BodyLimit)
