"""Runs on the target host as root, invoked by deploy.py with a git archive."""

import datetime
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.request

revision, archive = sys.argv[1:]
base = Path("/opt/carrtech/vm-manager")
release = base / "releases" / revision
state = Path("/var/lib/vm-manager")
base.mkdir(parents=True, exist_ok=True)
release.mkdir(parents=True, exist_ok=True)
if not shutil.which("incus") or not shutil.which("virsh"):
    raise SystemExit("Incus and libvirt must already be installed")
if (
    not Path("/usr/share/spice-html5/main.js").exists()
    or not Path("/usr/lib/python3.13/venv").exists()
):
    subprocess.run(
        [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "python3-venv",
            "spice-html5",
        ],
        check=True,
    )
try:
    account = pwd.getpwnam("vm-manager")
except KeyError:
    subprocess.run(
        [
            "useradd",
            "--system",
            "--home-dir",
            str(state),
            "--shell",
            "/usr/sbin/nologin",
            "--groups",
            "incus-admin,libvirt",
            "vm-manager",
        ],
        check=True,
    )
    account = pwd.getpwnam("vm-manager")
state.mkdir(mode=0o755, exist_ok=True)
os.chown(state, account.pw_uid, account.pw_gid)
for directory in ["isos", "disks", "tmp"]:
    target = state / directory
    target.mkdir(mode=0o755, exist_ok=True)
    os.chown(target, account.pw_uid, account.pw_gid)
with tarfile.open(archive) as tar:
    tar.extractall(release, filter="data")
Path(archive).unlink()
(release / "REVISION").write_text(revision + "\n")
venv = release / ".venv"
subprocess.run(["python3", "-m", "venv", str(venv)], check=True)
subprocess.run(
    [
        str(venv / "bin/pip"),
        "install",
        "--quiet",
        "-r",
        str(release / "requirements.lock"),
    ],
    check=True,
)
certs = base / "tls"
certs.mkdir(mode=0o750, exist_ok=True)
os.chown(certs, 0, account.pw_gid)
if not (certs / "server.key").exists():
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "825",
            "-keyout",
            str(certs / "server.key"),
            "-out",
            str(certs / "server.crt"),
            "-subj",
            "/CN=192.168.8.235",
            "-addext",
            "subjectAltName=IP:192.168.8.235,DNS:ctlnxdbn001,DNS:localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
for file in certs.iterdir():
    os.chown(file, 0, account.pw_gid)
    file.chmod(0o640)
current = base / "current"
previous = current.resolve() if current.exists() else None
subprocess.run(["systemctl", "stop", "vm-manager.service"], check=False)
if (state / "manager.sqlite3").exists():
    backups = base / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups / (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + ".sqlite3"
    )
    with (
        sqlite3.connect(state / "manager.sqlite3") as source,
        sqlite3.connect(backup) as destination,
    ):
        source.backup(destination)
    backup.chmod(0o600)
unit = (release / "scripts" / "vm-manager.service").read_text()
Path("/etc/systemd/system/vm-manager.service").write_text(unit)
temporary = base / "next"
temporary.unlink(missing_ok=True)
temporary.symlink_to(release)
os.replace(temporary, current)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "--now", "vm-manager.service"], check=True)
context = ssl._create_unverified_context()
healthy = False
for attempt in range(20):
    try:
        with urllib.request.urlopen(
            "https://192.168.8.235:8443/health", context=context, timeout=3
        ) as response:
            health = json.load(response)
            healthy = (
                health.get("status") == "ok"
                and health.get("service") == "vm-manager"
                and health.get("revision") == revision
            )
        if healthy:
            break
    except Exception:
        time.sleep(1)
if not healthy:
    if previous:
        temporary.symlink_to(previous)
        os.replace(temporary, current)
        subprocess.run(["systemctl", "restart", "vm-manager.service"], check=False)
    raise SystemExit(
        "Service failed health check; previous source restored when available. Database backup retained."
    )
(base / "deployed-revision").write_text(revision + "\n")
print("Installed " + revision + "; persistent data and backups retained")
