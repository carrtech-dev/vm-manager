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
    parser.add_argument('--address', help='HTTPS listen address on the target (defaults to the SSH hostname)')
    parser.add_argument('--public-url', help='Optional public HTTPS origin to verify')
    parser.add_argument('--jump', help='SSH jump host; verify private HTTPS from the target')
    args = parser.parse_args()
    address = args.address or args.host.rsplit('@', 1)[-1]
    import ipaddress
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise SystemExit('--address must be a reachable IPv4 or IPv6 address')
    authority = '[' + address + ']' if ':' in address else address
    health_url = 'https://' + authority + ':8443/health'
    ssh = ['ssh'] + (['-J', args.jump] if args.jump else [])
    scp = ['scp'] + (['-J', args.jump] if args.jump else [])
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
            scp + [str(archive), args.host + ":" + remote_archive], check=True
        )
        script = (ROOT / "scripts" / "install_remote.py").read_text()
        subprocess.run(
            ssh + [args.host, "sudo", "python3", "-", revision, remote_archive, address],
            input=script,
            text=True,
            check=True,
        )
    context = (
        ssl._create_unverified_context()
    )  # Explicitly verify LAN self-signed endpoint after deployment.
    for attempt in range(20):
        try:
            if args.jump:
                # The private address may be reachable only beyond the jump host.
                # It is validated as an IP above and encoded as a Python string.
                probe = ('import ssl,urllib.request;print(urllib.request.urlopen('
                         + repr(health_url) + ',context=ssl._create_unverified_context(),timeout=5).read().decode())')
                health = json.loads(subprocess.check_output(ssh + [args.host, 'python3', '-'],
                                    input=probe, text=True, timeout=15))
            else:
                with urllib.request.urlopen(health_url, context=context, timeout=5) as response:
                    health = json.load(response)
            if (
                health.get("status") == "ok"
                and health.get("service") == "vm-manager"
                and health.get("revision") == revision
            ):
                print("Verified live HTTPS health at " + health_url + " — revision " + revision)
                break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit(
            "Deployment health verification failed; inspect vm-manager.service"
        )
    tunnel_enabled = subprocess.run(
        ssh + [args.host, "sudo test -f /opt/carrtech/vm-manager/tunnel/credentials.json"]
    ).returncode == 0
    if tunnel_enabled:
        public_url = args.public_url or ('https://vmm.carrtech.dev' if address == '192.168.8.235' else None)
        if not public_url:
            raise SystemExit('Tunnel configured: supply --public-url to verify this host public endpoint')
        for attempt in range(30):
            try:
                request = urllib.request.Request(
                    public_url.rstrip('/') + '/health',
                    headers={"User-Agent": "CarrTech-VM-Manager-Deploy/1.0"},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    health = json.load(response)
                    if (health.get("status") == "ok" and health.get("service") == "vm-manager"
                            and health.get("revision") == revision):
                        print("Verified public HTTPS health at " + public_url + " — revision " + revision)
                        return
            except Exception:
                pass
            time.sleep(2)
        raise SystemExit("LAN service healthy but public HTTPS verification failed; inspect the tunnel and DNS")


if __name__ == "__main__":
    main()
