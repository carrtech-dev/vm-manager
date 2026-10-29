#!/usr/bin/env python3
"""Provision VM Manager's dedicated tunnel using the operator's Cloudflare login.

Run from clean, integrated main, then run deploy.py. Account certificates and
per-tunnel secrets stay outside Git; only tunnel credentials reach the server.
"""

import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "vmm.carrtech.dev"
NAME = "vm-manager"
DEST = "/opt/carrtech/vm-manager/tunnel"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="claude@192.168.8.235")
    args = parser.parse_args()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True
    ).strip()
    if branch != "main" or dirty:
        raise SystemExit("Provision only from a clean integrated main checkout")
    if not shutil.which("cloudflared"):
        raise SystemExit("Install cloudflared and run cloudflared tunnel login first")

    with tempfile.TemporaryDirectory(prefix="vmm-cloudflared-") as temp:
        config = Path(temp) / "config.yml"
        config.write_text("{}\n")

        def cli(*command, capture=False):
            return subprocess.run(
                ["cloudflared", "--config", str(config), "tunnel", *command],
                check=True, text=True, capture_output=capture,
            )

        def matches():
            tunnels = json.loads(cli("list", "--output", "json", capture=True).stdout)
            return [t for t in tunnels if t["name"] == NAME and (
                not t.get("deleted_at") or t["deleted_at"].startswith("0001-")
            )]

        tunnels = matches()
        if not tunnels:
            cli("create", NAME)
            tunnels = matches()
        if len(tunnels) != 1:
            raise SystemExit("Expected exactly one VM Manager tunnel; inspect Cloudflare")
        tunnel_id = tunnels[0]["id"]
        credentials = Path.home() / ".cloudflared" / (tunnel_id + ".json")
        if not credentials.is_file():
            raise SystemExit("Recover this tunnel's missing local credentials before continuing")
        # No overwrite flag: conflicting DNS records must be reviewed explicitly.
        cli("route", "dns", tunnel_id, DOMAIN)
        configuration = {
            "tunnel": tunnel_id,
            "credentials-file": "/etc/cloudflared/credentials.json",
            "metrics": "127.0.0.1:20243",
            "ingress": [
                {
                    "hostname": DOMAIN,
                    "service": "https://192.168.8.235:8443",
                    "originRequest": {
                        "caPool": "/etc/cloudflared/origin.crt",
                        "originServerName": "ctlnxdbn001",
                    },
                },
                {"service": "http_status:404"},
            ],
        }
        staging = subprocess.check_output(
            ["ssh", args.host, "umask 077; mktemp -d /tmp/vmm-tunnel.XXXXXXXX"],
            text=True,
        ).strip()
        if not staging.startswith("/tmp/vmm-tunnel.") or not staging.replace(
            "/", ""
        ).replace("-", "").replace(".", "").isalnum():
            raise SystemExit("Unexpected staging path")
        try:
            for name, content in (
                ("credentials.json", credentials.read_bytes()),
                ("config.yml", json.dumps(configuration, indent=2).encode()),
            ):
                subprocess.run(
                    ["ssh", args.host, "cat > " + shlex.quote(staging + "/" + name)],
                    input=content, check=True,
                )
            script = f"""set -eu
install -d -o 65532 -g 65532 -m 700 {DEST}
if test -f {DEST}/config.yml; then
    backup=/opt/carrtech/vm-manager/backups/tunnel-$(date -u +%Y%m%dT%H%M%SZ)
    install -d -m 700 "$backup"
    cp -a {DEST}/. "$backup/"
fi
install -o 65532 -g 65532 -m 600 {staging}/credentials.json {DEST}/credentials.json
install -o 65532 -g 65532 -m 600 {staging}/config.yml {DEST}/config.yml
install -o 65532 -g 65532 -m 600 /opt/carrtech/vm-manager/tls/server.crt {DEST}/origin.crt
"""
            subprocess.run(["ssh", args.host, "sudo sh -s"], input=script, text=True, check=True)
        finally:
            subprocess.run(
                ["ssh", args.host, "rm -f " + staging + "/credentials.json " + staging
                 + "/config.yml && rmdir " + staging], check=True,
            )
    print("Configured", DOMAIN, "on dedicated tunnel", tunnel_id, "— run scripts/deploy.py")


if __name__ == "__main__":
    main()
