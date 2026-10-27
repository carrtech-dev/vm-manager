#!/usr/bin/env python3
"""Deploy a clean integrated main commit, preserve data, back up SQLite, and verify health."""

import argparse
import json
import pathlib
import subprocess
import tempfile
import urllib.request
import ssl
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="claude@192.168.8.235")
    args = parser.parse_args()
    if run("git", "branch", "--show-current", cwd=ROOT) != "main" or run(
        "git", "status", "--porcelain", cwd=ROOT
    ):
        raise SystemExit("Deploy only a clean integrated main checkout")
    revision = run("git", "rev-parse", "HEAD", cwd=ROOT)
    with tempfile.TemporaryDirectory() as tmp:
        archive = pathlib.Path(tmp) / "source.tar.gz"
        subprocess.run(
            ["git", "archive", "--format=tar.gz", "-o", str(archive), revision],
            cwd=ROOT,
            check=True,
        )
        remote_archive = "/tmp/vm-manager-" + revision + ".tar.gz"
        subprocess.run(
            ["scp", str(archive), args.host + ":" + remote_archive], check=True
        )
        script = (ROOT / "scripts" / "install_remote.py").read_text()
        subprocess.run(
            ["ssh", args.host, "sudo", "python3", "-", revision, remote_archive],
            input=script,
            text=True,
            check=True,
        )
    context = (
        ssl._create_unverified_context()
    )  # Explicitly verify LAN self-signed endpoint after deployment.
    for attempt in range(20):
        try:
            with urllib.request.urlopen(
                "https://192.168.8.235:8443/health", context=context, timeout=5
            ) as response:
                health = json.load(response)
                if (
                    health.get("status") == "ok"
                    and health.get("service") == "vm-manager"
                    and health.get("revision") == revision
                ):
                    print(
                        "Verified live HTTPS health at https://192.168.8.235:8443 — revision "
                        + revision
                    )
                    return
        except Exception:
            time.sleep(1)
    raise SystemExit(
        "Deployment health verification failed; inspect vm-manager.service"
    )


if __name__ == "__main__":
    main()
