# Linux distribution packages

The initial supported targets are **Debian 13 amd64** (DEB, Python 3.13) and
**Rocky/AlmaLinux 10 x86_64** (RPM, Python 3.12). Other distributions/architectures
are not yet validated. The web interface works immediately after installation.
Install/configure Incus and/or libvirt when you want to manage VMs on this host;
the package never initializes a hypervisor or changes physical networks.
RPM hosts need EPEL's `rclone` for external S3 storage, and an appropriate Incus
repository if Incus is not already installed. Enable the host's normal OS repos.

## Install and update

The repository origin is `https://packages.carrtech.dev`. Download
`vm-manager.asc` and compare its fingerprint with the independently published
fingerprint in `packaging/signing-key.asc` in the GitHub repository before trusting
it. The initial signing fingerprint is
`E8474165B8EAF8C9A56068DB2B47E4311A98416D` (expires 2028-10-31). Download the matching package, then install with:

```sh
# Debian 13
sudo apt install ./vm-manager_VERSION-1_amd64.deb
# Rocky/AlmaLinux 10 (import verified key before installing)
sudo rpm --import ./vm-manager.asc
sudo dnf install ./vm-manager-VERSION-1.el10.x86_64.rpm
```

The package installs its signed repository configuration. Subsequent updates use:

```sh
sudo apt update
sudo apt install --only-upgrade vm-manager
# Or include VM Manager in normal sudo apt upgrade.
sudo dnf update vm-manager
```

`apt update` refreshes available versions; `apt upgrade` installs them.
DEBs are authenticated through signed APT metadata and SHA-256; RPMs and DNF
metadata are both GPG-signed. Direct DEB downloads must be verified against the
signed `SHA256SUMS` before installation. Never use `trusted=yes` or disable GPG
checks. You can also add the APT repository before the initial installation:

```sh
sudo install -D -m 644 vm-manager.asc /usr/share/keyrings/vm-manager.asc
printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/vm-manager.asc] https://packages.carrtech.dev/apt stable main' | sudo tee /etc/apt/sources.list.d/vm-manager.list
sudo apt update
sudo apt install vm-manager
```

Remove that manually created `.list` after installation because the package owns
`vm-manager.sources` with the same repository.

After installation, open one of the **browser URLs printed by the installer**,
for example `https://192.168.8.10:8443`. Show the current URLs and local health with:

```sh
sudo vm-manager status
```

The service starts automatically and listens on the host's network interfaces.
The installer permits HTTPS port 8443 in an active firewalld or UFW firewall,
including persistent rules, without enabling/resetting either firewall or reloading
firewalld. Existing firewalld rich rules for port 8443 are preserved. Only rules
added by this package are removed on uninstall; existing administrator rules remain.
On a host connected to your LAN, no separate firewall command is required.

The generated certificate includes the host name and current interface addresses.
It is self-signed, so trust it in your browser or replace
`/etc/vm-manager/tls/server.{key,crt}` with a trusted host certificate (root:vm-manager
0640). Existing certificates are preserved on upgrade.

Local settings in `/etc/vm-manager/vm-manager.conf` are preserved. Set
`VM_MANAGER_MANAGE_FIREWALL=no` to manage firewall policy yourself; reconfigure
the package to remove only its previously added rules. Optionally select specific
firewalld zones with `VM_MANAGER_FIREWALL_ZONES="public internal"`. By default,
active zones are used except intentionally blocked `drop`/`block` zones.

For VMs behind NAT or a cloud security group, the upstream VM/router/cloud platform
also needs to make the port reachable. A guest package cannot change that upstream
network. Use the platform's published LAN address, or attach the VM to a LAN/routed
network. For example, publish a LAN host port 9443 to guest port 8443. Set
`VM_MANAGER_PUBLIC_URL=https://LAN_HOST:9443` in the guest configuration to print
that published address first in the installer and status output.

Retrieve the random initial administrator credentials locally as root:

```sh
sudo cat /var/lib/vm-manager/initial-admin.txt
```

Username is `admin`. The credentials file and SQLite database are mode 0600,
owned by the service account; the application stores an scrypt password hash.
Change the password in Account after first login; that removes the initial
credentials file. Upgrades never reset accounts or recreate credentials when
accounts exist. No default password or private key is shipped in either package.

The main service runs as `vm-manager`; the existing storage broker runs as root
through its local restricted socket. If Incus/libvirt is installed after the
package, reconfigure/reinstall the package to refresh service group membership.

## Lifecycle and recovery

Application files are in `/usr/lib/vm-manager`. A root-owned virtualenv is built
from bundled, pinned wheels in `/var/lib/vm-manager-runtime/venv`, without network
access during package configuration. OS dependencies are resolved by APT/DNF.
The previous virtualenv is retained until the next successful configuration.
TLS and local settings live in `/etc/vm-manager`; VM/account data stays in
`/var/lib/vm-manager`, broker data in `/var/lib/vm-manager-storage`, and edge data
in `/var/lib/vm-manager-edge`. Systemd starts both services and verifies the local
HTTPS revision after installation. Containers without systemd configure the
application but do not start services.

Before an upgrade, services stop and a consistent SQLite backup, TLS/settings,
broker registries, and edge configuration are archived under the root-only
`/var/lib/vm-manager-runtime/backups`. Large mounted VM/backup volumes are excluded.
A failed upgrade is reported by the package manager; inspect the service journal
and retain its backup. Recovery is an explicit operator action: stop both
services, reinstall the prior signed package, restore its database/configuration
backup if migrations require it, restore file ownership, then start and verify.
Do not downgrade a migrated database blindly. Uninstall/purge stops services but
retains data, certificates, credentials, backups and the service account.

The source deployment at `/opt/carrtech/vm-manager/current` is protected by a
pre-install check. Do not install a package over that deployment: explicit
migration is required. The project's established source deployment continues to
use `scripts/deploy.py`.

## Repeatable release workflow

Run builds from a clean tested Git revision; increment a numeric semantic version
for every publication, including packaging-only changes. Never reuse an uploaded
package version. Build only from public source, with no runtime data or credentials.
The following commands require an x86_64 Linux Docker host (or amd64 emulation).

Keep the persistent signing key outside Git and R2, with an encrypted offline
backup. Initialize once, for example using GPG inside the build image:

```sh
docker build -f packaging/Containerfile -t vm-manager-packager .
# /secure/vmm-signing must be mode 0700 and persist between releases.
docker run --rm --entrypoint gpg -v /secure/vmm-signing:/keys \
  -e GNUPGHOME=/keys vm-manager-packager --batch --passphrase '' \
  --quick-generate-key 'CarrTech VM Manager packages <support@carrtech.dev>' rsa3072 sign 2y
# Export the public key; never export the private key into the source tree.
```

Use the full signing fingerprint (`gpg --list-keys --with-colons`) for KEY below.
The checked-in public key is `packaging/signing-key.asc`.

```sh
REVISION=$(git rev-parse HEAD)
docker run --rm --platform linux/amd64 -v "$PWD:/src:ro" -v "$PWD/dist:/out" \
  vm-manager-packager --version 1.0.0 --revision "$REVISION" \
  --public-key /src/packaging/signing-key.asc
# Keep /release/repository across releases, or restore the complete previous R2
# mirror before generating new metadata. Never replace it with an empty directory.
docker run --rm --entrypoint python3 -e GNUPGHOME=/keys \
  -v /secure/vmm-signing:/keys -v "$PWD:/src:ro" -v "$PWD/dist:/out" \
  -v /release/repository:/repository vm-manager-packager \
  scripts/distribution/repository.py --artifacts /out --repository /repository --signing-key KEY
```

Always run one publisher at a time. A repository retains old packages and
content-addressed metadata so active update clients continue to work. APT metadata
expires after 90 days; refresh/sign/publish it before expiry even if the app has
not changed (use an empty artifacts directory). Packages are immutable. New files
are uploaded before APT's InRelease. DNF clients can need a retry during the brief
metadata/signature rollover. Invalidate any CDN rule overriding `no-cache` on
repository metadata. Keep private signing keys and R2 credentials out of `dist`.

Provision the bucket and custom domain once with a Cloudflare API token having
R2 Edit and access to the chosen account/zone. The script preserves existing
buckets/domains and does not delete data. Cloudflare API reference:
https://developers.cloudflare.com/api/resources/r2/subresources/buckets/

```sh
python3 scripts/distribution/r2.py provision --account ACCOUNT_ID --zone ZONE_ID
# Install boto3 in a separate publishing venv. Use bucket-scoped Object Read &
# Write credentials for vm-manager-packages, through standard AWS env variables.
python3 scripts/distribution/r2.py publish --account ACCOUNT_ID --repository /release/repository
```

`CLOUDFLARE_API_TOKEN` is used only for provisioning; S3 publication uses
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. R2's development URL stays disabled;
public access is through `packages.carrtech.dev`. No secrets are embedded in the
installed repository files. After publication, verify both package managers in
disposable supported hosts: fresh installation, HTTPS login, credential permissions,
password change, upgrade preserving accounts/settings, signature rejection and
uninstall retaining persistent data. Never use production data for acceptance.

### Single-command releases

On the Linux Docker build host, after tests and integration, the equivalent
orchestrator is (the signing key must match the checked-in public key):

```sh
python3 scripts/distribution/release.py --version 1.0.1 \
  --state /var/lib/vm-manager-distribution \
  --signing-home /var/lib/vm-manager-distribution/gnupg \
  --signing-key E8474165B8EAF8C9A56068DB2B47E4311A98416D --publish
```

Omit `--publish` to prepare a repository for review. The orchestrator enforces a
clean committed source tree, serializes local releases, and refuses version reuse.
The initial signing key is retained on the established build host in the path
above, outside Git and the downloads bucket. Its public half is checked in.
The publishing credentials are saved owner-only on the operator Mac at
`~/.config/carrtech/vm-manager-packages-r2-credentials.json`; load them into AWS
environment variables in the publishing process without echoing them. The separate
provisioning token expires after seven days; recurring releases use the
bucket-scoped publisher credential. Preserve the repository mirror and securely
back up the signing directory before moving the release process to another host.

`scripts/distribution/check_install.py` runs inside an explicitly marked
disposable supported host. Its `prepare` phase verifies HTTPS login and changes
the temporary admin password, `verify-upgrade` verifies preservation after an
APT/DNF upgrade, and `verify-removed` verifies uninstall retention. It deliberately
requires `/root/vmm-distribution-disposable` and must never run on production data.

Version 1.0.2 adds automatic host firewall access, install-time browser URLs,
`vm-manager status`, and concise monitoring notices when a hypervisor is unavailable.
