"""Client for the narrowly scoped, privileged storage broker."""

import json
import os
import socket
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .hypervisor import HypervisorError, name
from .task_output import emit

Purpose = Literal["vm", "iso", "backup"]
lock = threading.RLock()


class BlockCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    parent: str = Field(min_length=1, max_length=63)
    purpose: Purpose
    size_gib: int = Field(ge=1, le=65536)

    @field_validator("name", "parent")
    @classmethod
    def valid_name(cls, value):
        try:
            return name(value)
        except HypervisorError as exc:
            raise ValueError(str(exc)) from exc


class BlockSize(BaseModel):
    size_gib: int = Field(ge=1, le=65536)


class DefaultBlock(BaseModel):
    block_id: str = Field(default="", max_length=32)


def broker(operation, **arguments):
    path = os.environ.get("VM_MANAGER_STORAGE_SOCKET", "/run/vm-manager-storage/control.sock")
    if operation != 'list':
        emit(f"Storage host: {operation} started\n")
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(86500 if operation == "libvirt_export" else 195)
            connection.connect(path)
            connection.sendall(json.dumps({"operation": operation, **arguments}).encode() + b"\n")
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline(1024 * 1024))
        if "error" in response:
            raise HypervisorError(response["error"])
        if operation != 'list':
            emit(f"Storage host: {operation} completed\n")
        return response["result"]
    except (OSError, ValueError) as exc:
        raise HypervisorError("Storage service unavailable: " + str(exc)) from exc


def defaults(store):
    with store.db() as db:
        return dict(db.execute("SELECT purpose,block_id FROM storage_defaults").fetchall())


def directory(store, data, purpose, block_id=None):
    if block_id is None:
        block_id = defaults(store).get(purpose, "")
    if not block_id:
        path = data / {"iso": "isos", "backup": "config-backups"}[purpose]
        path.mkdir(exist_ok=True, mode=0o700 if purpose == "backup" else 0o755)
        return path
    block = next((b for b in broker("list") if b["id"] == block_id), None)
    if not block or block["purpose"] != purpose or block["status"] != "ready":
        raise HypervisorError("Selected storage block is unavailable or has the wrong purpose")
    path = Path(block["path"])
    # Never recreate an absent mount: that could silently write to the system disk.
    if not path.is_dir() or not os.path.ismount(path):
        raise HypervisorError("Storage block is not mounted")
    return path


def check_vm_pool(pool):
    if not pool.startswith("vmm-block-"):
        return
    if not any(b["pool"] == pool and b["purpose"] == "vm" and b["status"] == "ready"
               for b in broker("list")):
        raise HypervisorError("VM storage block is unavailable")
