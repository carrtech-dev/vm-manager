"""Incus REST API and libvirt adapter. Never invokes a shell."""

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlsplit
import httpx

from .task_output import emit, capturing, stream_command, command_label

NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,62}$")


class HypervisorError(Exception):
    pass


class HypervisorNotFound(HypervisorError):
    pass


def name(value):
    if not NAME.fullmatch(value):
        raise HypervisorError(
            "Use 1–63 letters, digits, underscores or hyphens, starting with a letter."
        )
    return value


def run(*args, timeout=180, stdin=None):
    try:
        label = command_label(args) if capturing() else None
        if label:
            emit(f"Host: {label} started\n")
            p = stream_command(args, timeout, stdin)
            emit(f"Host: {label} exited with code {p.returncode}\n")
        else:
            p = subprocess.run(
                args,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HypervisorError(str(e)) from e
    if p.returncode:
        raise HypervisorError((p.stderr or p.stdout or "Command failed").strip()[:1500])
    return p.stdout.strip()


def bytes_size(value):
    match = re.fullmatch(r"([\d.]+)\s*([KMGT]?i?B)?", str(value), re.I)
    if not match:
        return 0
    number, unit = match.groups()
    unit = (unit or "B").upper()
    power = {
        "B": 0,
        "KB": 1,
        "KIB": 1,
        "MB": 2,
        "MIB": 2,
        "GB": 3,
        "GIB": 3,
        "TB": 4,
        "TIB": 4,
    }[unit]
    return int(float(number) * (1024 if "I" in unit else 1000) ** power)


class Incus:
    def __init__(self):
        self.socket = os.environ.get("INCUS_SOCKET", "/var/lib/incus/unix.socket")

    def request(self, method, path, data=None, wait=True, etag=None):
        mutating = method != "GET" and capturing()
        if mutating:
            emit(f"Incus: {method} {urlsplit(path).path}\n")
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=self.socket),
            base_url="http://incus",
            timeout=190,
        ) as client:
            response = client.request(
                method, path, json=data, headers={"If-Match": etag} if etag else None
            )
            try:
                result = response.json()
            except ValueError as e:
                raise HypervisorError("Incus returned an invalid response") from e
            if response.status_code >= 400 or result.get("type") == "error":
                error = HypervisorNotFound if response.status_code == 404 else HypervisorError
                raise error(result.get("error", "Incus request failed"))
            if result.get("type") == "async" and wait:
                operation_url = urlsplit(result["operation"])
                deadline = time.monotonic() + 180
                previous = None
                while True:
                    operation = client.get(
                        operation_url.path
                        + ("/wait?timeout=1" if mutating else "/wait?timeout=180")
                        + ("&" + operation_url.query if operation_url.query else "")
                    ).json()
                    meta = operation.get("metadata") or {}
                    if operation.get("type") == "error":
                        raise HypervisorError(operation.get("error", "Failed waiting for operation"))
                    progress = meta.get("metadata") or {}
                    # Progress strings are host messages. Never expose API bodies,
                    # configuration, console credentials, or arbitrary metadata.
                    detail = "; ".join(str(v) for k, v in progress.items()
                                       if k.endswith('_progress') and isinstance(v, str))
                    message = f"Incus: {meta.get('status', 'Waiting for host')}" + (f" · {detail}" if detail else "")
                    if mutating and message != previous:
                        emit(message + "\n")
                        previous = message
                    if meta.get("status_code") == 200:
                        break
                    if (not mutating or meta.get("status_code") not in (100, 103, 104, 105)
                            or time.monotonic() >= deadline):
                        raise HypervisorError(meta.get("err") or
                            "Incus operation is still running; inspect the resource before retrying.")
            elif mutating:
                emit("Incus: request completed\n")
            result["_etag"] = response.headers.get("etag")
            return result

    def get(self, path):
        return self.request("GET", path)["metadata"]

    def vms(self):
        result = []
        for vm in self.get("/1.0/instances?recursion=2"):
            if vm["type"] != "virtual-machine":
                continue
            conf = vm.get("expanded_config") or vm.get("config") or {}
            state = vm.get("state") or {}
            devices = vm.get("expanded_devices") or vm.get("devices") or {}
            addresses = []
            for nic in (state.get("network") or {}).values():
                addresses.extend(
                    a["address"]
                    for a in nic.get("addresses", [])
                    if a.get("family") == "inet" and a.get("scope") == "global"
                )
            result.append(
                {
                    "id": "incus:" + vm["name"],
                    "name": vm["name"],
                    "provider": "incus",
                    "status": vm.get("status", "Unknown").lower(),
                    "cpu": conf.get("limits.cpu", "host"),
                    "memory": bytes_size(conf.get("limits.memory", "0")),
                    "addresses": addresses,
                    "description": vm.get("description", ""),
                    "devices": devices,
                    "config": conf,
                    "managed": conf.get("user.vm-manager") == "true",
                    "state": state,
                }
            )
        return result

    def vm(self, vm_name):
        return self.get("/1.0/instances/" + quote(name(vm_name)))

    def networks(self):
        return [
            {
                "id": "incus:" + n["name"],
                "provider": "incus",
                "name": n["name"],
                "description": n.get("description", ""),
                "config": n.get("config", {}),
                "used_by": n.get("used_by", []),
                "managed": n.get("managed", False),
                "status": n.get("status", "Created"),
                "type": n["type"],
            }
            for n in self.get("/1.0/networks?recursion=1")
            if n.get("managed")
        ]

    def pools(self):
        pools = self.get("/1.0/storage-pools?recursion=1")
        for pool in pools:
            try:
                pool["resources"] = self.get(
                    "/1.0/storage-pools/" + quote(pool["name"]) + "/resources"
                )
            except HypervisorError:
                pool["resources"] = {}
        return pools

    def isos(self):
        items = []
        for pool in self.get("/1.0/storage-pools?recursion=1"):
            for volume in self.get(
                "/1.0/storage-pools/"
                + quote(pool["name"])
                + "/volumes/custom?recursion=1"
            ):
                if volume.get("content_type") == "iso":
                    items.append(
                        {
                            "id": "incus:" + pool["name"] + ":" + volume["name"],
                            "name": volume["name"],
                            "pool": pool["name"],
                            "source": "incus",
                            "size": bytes_size(
                                volume.get("config", {}).get("size", "0")
                            ),
                            "used_by": volume.get("used_by", []),
                            "created": volume.get("created_at"),
                        }
                    )
        return items

    def update(self, vm_name, transform):
        original = self.request("GET", "/1.0/instances/" + quote(name(vm_name)))
        vm = original["metadata"]
        transform(vm)
        payload = {
            k: vm[k]
            for k in (
                "architecture",
                "config",
                "devices",
                "ephemeral",
                "profiles",
                "description",
            )
            if k in vm
        }
        self.request(
            "PUT",
            "/1.0/instances/" + quote(vm_name),
            payload,
            etag=original.get("_etag"),
        )

    def create(self, spec, iso):
        vm_name = name(spec["name"])
        devices = {
            "root": {
                "type": "disk",
                "path": "/",
                "pool": spec["pool"],
                "size": str(spec["disk_gib"]) + "GiB",
                # Match Incus's root-disk default: ahead of NICs/data disks (0),
                # behind installation media (10). A tie lets PXE run first.
                "boot.priority": "1",
            },
            "eth0": {"type": "nic", "name": "eth0", "network": spec["network"]},
        }
        if iso:
            devices["cdrom"] = self.cdrom(iso, True)
        volumes = []
        try:
            for index, size in enumerate(spec.get("extra_disks", [])):
                volume = "vmm-" + vm_name + "-disk" + str(index + 1)
                self.request(
                    "POST",
                    f"/1.0/storage-pools/{quote(spec['pool'])}/volumes/custom",
                    {
                        "name": volume,
                        "type": "custom",
                        "content_type": "block",
                        "config": {"size": str(size) + "GiB"},
                    },
                )
                volumes.append(volume)
                devices["disk" + str(index + 1)] = {
                    "type": "disk",
                    "pool": spec["pool"],
                    "source": volume,
                }
            self.request(
                "POST",
                "/1.0/instances",
                {
                    "name": vm_name,
                    "type": "virtual-machine",
                    "source": {"type": "none"},
                    "profiles": [],
                    "description": spec.get("description", ""),
                    "config": {
                        "limits.cpu": str(spec["cpu"]),
                        "limits.memory": str(int(spec["memory_gib"] * 1024**3)),
                        "security.secureboot": str(spec["secure_boot"]).lower(),
                        "boot.autostart": str(spec.get("autostart", False)).lower(),
                        "user.vm-manager": "true",
                    },
                    "devices": devices,
                },
            )
        except Exception:
            # Delete only volumes created by this attempt and only when unused.
            for volume in volumes:
                try:
                    current = self.get(
                        f"/1.0/storage-pools/{quote(spec['pool'])}/volumes/custom/{quote(volume)}"
                    )
                    if not current.get("used_by"):
                        self.request(
                            "DELETE",
                            f"/1.0/storage-pools/{quote(spec['pool'])}/volumes/custom/{quote(volume)}",
                        )
                except HypervisorError:
                    pass
            raise

    @staticmethod
    def cdrom(iso, boot):
        device = {
            "type": "disk",
            "source": iso.get("path", iso["name"]),
            "boot.priority": "10" if boot else "0",
            "readonly": "true",
        }
        if iso.get("pool"):
            device["pool"] = iso["pool"]
        return device

    def action(self, vm_name, action):
        if action == "start":
            vm = self.vm(vm_name)
            root = vm.get("devices", {}).get("root", {})
            if (
                vm.get("status", "").lower() == "stopped"
                and vm.get("config", {}).get("user.vm-manager") == "true"
                and root.get("path") == "/"
                and root.get("boot.priority") == "0"
            ):
                def repair_boot_order(current):
                    if current.get("status", "").lower() != "stopped":
                        raise HypervisorError("VM must be stopped to repair its boot order")
                    device = current.get("devices", {}).get("root", {})
                    if device.get("path") == "/" and device.get("boot.priority") == "0":
                        device["boot.priority"] = "1"

                # Incus 6.0 hot-removes/re-adds disks on a priority change.
                # Repair only while stopped, before QEMU builds the device order.
                self.update(vm_name, repair_boot_order)
        self.request(
            "PUT",
            "/1.0/instances/" + quote(name(vm_name)) + "/state",
            {
                "action": {
                    "start": "start",
                    "shutdown": "stop",
                    "stop": "stop",
                    "restart": "restart",
                }[action],
                "timeout": 0 if action == "stop" else 60,
                "force": action == "stop",
                "stateful": False,
            },
        )

    def delete(self, vm_name):
        vm = self.vm(vm_name)
        if vm["status"].lower() != "stopped":
            raise HypervisorError("Shut down the VM before deleting it.")
        owned = [
            (d["pool"], d["source"])
            for d in vm.get("devices", {}).values()
            if d.get("type") == "disk"
            and d.get("pool")
            and d.get("source", "").startswith("vmm-" + vm_name + "-")
        ]
        self.request("DELETE", "/1.0/instances/" + quote(vm_name))
        for pool, volume in owned:
            self.request(
                "DELETE",
                f"/1.0/storage-pools/{quote(pool)}/volumes/custom/{quote(volume)}",
            )


class Libvirt:
    def cli(self, *args, stdin=None):
        return run("virsh", "-c", "qemu:///system", *args, stdin=stdin)

    def xml(self, vm_name):
        return ET.fromstring(self.cli("dumpxml", name(vm_name), "--inactive"))

    def vms(self):
        result = []
        for vm_name in self.cli("list", "--all", "--name").splitlines():
            if not vm_name:
                continue
            root = self.xml(vm_name)
            state = self.cli("domstate", vm_name).strip()
            devices = {}
            for index, disk in enumerate(root.findall("./devices/disk")):
                source = disk.find("source")
                target = disk.find("target")
                devices[(target.get("dev") if target is not None else str(index))] = {
                    "type": "disk",
                    "kind": disk.get("device"),
                    "source": source.get("file", source.get("dev", ""))
                    if source is not None
                    else "",
                    "readonly": disk.find("readonly") is not None,
                }
            for index, nic in enumerate(root.findall("./devices/interface")):
                source = nic.find("source")
                devices[f"nic{index}"] = {
                    "type": "nic",
                    "network": source.get("network", source.get("bridge", ""))
                    if source is not None
                    else "",
                    "nictype": nic.get("type"),
                }
            memory = root.find("memory")
            factor = (
                {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "bytes": 1}.get(
                    memory.get("unit", "KiB"), 1024
                )
                if memory is not None
                else 1
            )
            result.append(
                {
                    "id": "libvirt:" + vm_name,
                    "name": vm_name,
                    "provider": "libvirt",
                    "status": "running"
                    if state == "running"
                    else "stopped"
                    if state == "shut off"
                    else state,
                    "cpu": root.findtext("vcpu", "0"),
                    "memory": int(root.findtext("memory", "0")) * factor,
                    "description": root.findtext("description", ""),
                    "devices": devices,
                    "addresses": [],
                    "managed": False,
                    "config": {},
                }
            )
        return result

    def networks(self):
        result = []
        for net in self.cli("net-list", "--all", "--name").splitlines():
            if not net:
                continue
            root = ET.fromstring(self.cli("net-dumpxml", net))
            ip = root.find("ip")
            forward = root.find("forward")
            config = {
                "ipv4.address": f"{ip.get('address')}/{ipaddress.IPv4Network('0.0.0.0/' + ip.get('netmask', '255.255.255.0')).prefixlen}"
                if ip is not None
                else "none",
                "ipv4.nat": "true"
                if forward is not None and forward.get("mode") == "nat"
                else "false",
                "ipv4.routing": "true" if forward is not None else "false",
                "ipv4.dhcp": "true" if root.find("./ip/dhcp") is not None else "false",
            }
            result.append(
                {
                    "id": "libvirt:" + net,
                    "name": net,
                    "provider": "libvirt",
                    "config": config,
                    "description": "Existing libvirt network",
                    "type": "bridge",
                    "managed": False,
                    "used_by": [],
                }
            )
        return result

    def action(self, vm_name, action):
        self.cli(
            {
                "start": "start",
                "shutdown": "shutdown",
                "stop": "destroy",
                "restart": "reboot",
            }[action],
            name(vm_name),
        )

    def define(self, root):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml") as f:
            f.write(ET.tostring(root, encoding="unicode"))
            f.flush()
            self.cli("define", f.name, "--validate")

    def edit_resources(self, vm_name, cpu, memory_gib, description):
        self.stopped(vm_name)
        root = self.xml(vm_name)
        root.find("vcpu").text = str(cpu)
        root.find("vcpu").attrib.pop("current", None)
        topology = root.find("./cpu/topology")
        if topology is not None:
            for attribute in topology.attrib:
                topology.set(attribute, str(cpu) if attribute == "cores" else "1")
        for field in ("memory", "currentMemory"):
            node = root.find(field)
            if node is not None:
                node.set("unit", "KiB")
                node.text = str(int(memory_gib * 1024**2))
        node = root.find("description")
        if node is None:
            node = ET.SubElement(root, "description")
        node.text = description
        self.define(root)

    def stopped(self, vm_name):
        if self.cli("domstate", name(vm_name)).strip() != "shut off":
            raise HypervisorError("Shut down the VM before editing its hardware.")
