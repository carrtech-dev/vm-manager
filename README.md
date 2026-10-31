# CarrTech VM Manager

A self-hosted virtualization console for `ctlnxdbn001` (`192.168.8.235`). Manages Incus virtual machines and discovers existing libvirt machines through `qemu:///system`. Incus containers are intentionally excluded from the VM list.

## Open the manager

Open **https://vmm.carrtech.dev** through the dedicated Cloudflare Tunnel and sign in with your existing VM Manager account. Public HTTPS uses Cloudflare's trusted certificate. HTTP visitors are redirected to HTTPS, with HSTS on public HTTPS responses. The connector verifies the app's existing HTTPS certificate using a private certificate bundle; VM Manager continues to enforce its own login, permissions and same-origin checks.

Local network access remains available at **https://192.168.8.235:8443**. The installer generates a server certificate for this address. Trust that certificate on your devices to avoid browser certificate warnings. Use this LAN address for large ISO uploads that exceed your Cloudflare plan's request-size limit.

The first run creates `admin` with a random password in `/var/lib/vm-manager/initial-admin.txt` (readable by the service account and root). Retrieve it over your existing SSH connection:

```sh
ssh claude@192.168.8.235 'sudo cat /var/lib/vm-manager/initial-admin.txt'
```

Change the password through **Account** after signing in; the initial credentials file is removed when the password is changed. Account data and sessions are stored in SQLite; no external identity provider is required.

## Workflow

1. Create a **Network**. NAT provides outbound connectivity; isolated networks disable IP forwarding. DHCP and a non-overlapping subnet can be configured automatically.
2. Upload an operating-system `.iso` in **ISO library**. Existing Incus ISO volumes are also discovered. Files are validated as ISO9660/UDF, hashed with SHA-256, and stored persistently. Maximum size is 32 GiB, subject to available host space.
3. Select **Create VM** and complete **General**, **Hardware**, and **Network & media**. Use **Next**, **Back**, or the step list to navigate; your entries are retained. Review the configuration in **Create**, then select **Create VM** to create the stopped VM.
4. Start the VM and open **Console** to install the operating system. Incus uses the host's SPICE console; libvirt uses its local VNC display.

To paste from your computer, focus the destination field or terminal in the VM,
choose **Paste text** in the console toolbar, and paste into the text box (or use
**Read local clipboard** where the browser permits it). For a full-screen Linux
login/shell console, leave **Linux text console** selected: it preserves ASCII
quotes, punctuation and case independently of the keyboard layout and lock keys.
For desktop apps (including terminal apps) and installers, select the VM's English
US/UK keyboard layout with Caps Lock off. The destination choice is remembered
per VM in this browser. Click **Send text**. Cmd+V, Ctrl+V and
Ctrl+Shift+V while the display is focused also open the paste dialog. This types
up to 4,096 characters in short bursts through SPICE/VNC without a guest agent,
including during OS installation. Linux text consoles use decimal Alt+numpad
entry; desktop/installer mode uses physical keys. Unsupported characters are
rejected before any text is sent.
Line breaks require confirmation because they send Enter and may run commands;
no extra Enter is added. **Stop sending** interrupts a transfer; review any
partially typed text in the VM before retrying. Reconnection stops the transfer
and never automatically replays it. Clipboard text stays in the browser and the
console channel; it is not stored in task history.

For ordinary Linux shell work on an Incus VM with a connected guest agent, the
VM's **Console** tab defaults to the embedded **Text terminal**. Use the
**Text terminal / SPICE** switch above it at any time. Both views remain loaded
after first use, preserving the text shell and its scrollback while switching;
the SPICE connection starts only when selected. **Full screen** expands the
selected embedded view and keeps the switch available; Escape exits. **Open in
new tab** opens the selected view separately, where each viewer also has a
full-screen control. Libvirt VMs retain their embedded VNC view because text
terminals currently require Incus.
The text terminal starts at the VM’s own login prompt. Sign in with an account
and password configured inside that VM; VM Manager credentials do not sign you
into the guest. Guest login policies apply, so locked/passwordless cloud-image
accounts may need a password configured first. VM Manager administrators can
choose **Open as root** to start a root shell directly without guest credentials.
Operators always use guest login. **Reconnect to login** starts a fresh guest
login session, including after using root access. The guest must provide a Linux
`login` program; if it is unavailable, use the graphical console.
The terminal supports normal text paste, selectable output and 5,000 lines of
browser scrollback. **Copy output** copies the selection
or retained output; **Download output** saves it as text. Cmd+V / Ctrl+Shift+V
pastes directly; multiline text opens a review dialog because line breaks can
run commands. No additional Enter is added. The **Paste** button also supports
manual paste when browser clipboard permission is unavailable. Unicode and
punctuation do not depend on guest keyboard layout.

Terminal history stays in the open browser tab, including after a disconnect.
Reloading/closing the tab discards that browser buffer. Reconnect starts a new
shell and never replays input. Closing the connection terminates that terminal's
guest exec session. Shells can keep their normal history files inside the guest;
VM Manager records the terminal connection and login/root mode in audit history, not its commands
or output. Terminal access requires an operator/administrator, same-origin
session and CSRF token, with ongoing role/session checks. It currently supports
running Incus VMs only and requires the Incus agent. The graphical SPICE/VNC
console remains available for installers, boot screens and guests without an
agent; it shows a framebuffer and has no browser text scrollback.
5. Eject its ISO through **Hardware**. For a running VM this is saved as a pending change; use **Restart** in VM Manager to apply it and boot from disk.
6. CPU, memory, disks, network adapters, CD-ROMs, Secure Boot and autostart can be changed through the VM workspace. While running, hardware changes are saved persistently and shown as **Current → After restart** in the Hardware tab. Use **Restart** in VM Manager to shut down gracefully, apply the pending changes and start again, or shut down and use **Start**. Rebooting only inside the guest does not apply these pending changes. Stopped VMs apply edits immediately. Pending changes can be discarded before application. A failed change leaves the VM stopped with remaining changes retained; inspect the task log before retrying. Disk expansion does not expand the guest filesystem; complete that inside the guest.

New Incus VMs boot installation media first (priority 10), then their system disk
(priority 1), before network/PXE and extra data disks (default priority 0). Older
VM Manager VMs created with root disk priority 0 can spend minutes waiting for
IPv4 and IPv6 PXE to time out. VM Manager repairs that legacy setting during the
next **Start** or **Restart**, after the VM has stopped and its configuration has
been backed up. Other explicit priorities and VMs created outside VM Manager are
retained. Rebooting inside the guest retains the existing QEMU device order.
For a manual repair, back up the configuration, shut down the VM, set
`incus config device set <vm-name> root boot.priority=1` on the host, then start
it again. Do not change boot priority on a running VM: Incus 6.0 attempts to
hot-remove and re-add its root disk.

VM summaries refresh CPU and memory usage every five seconds, separately from configured resource limits. CPU percentage is normalized across allocated vCPUs. Memory uses guest usage when reported; otherwise it is explicitly labelled as host VM resident memory, which includes VM overhead and is not guest application usage. Missing measurements are shown as unavailable.

Console WebSockets send ping/pong keep-alives every 20 seconds. Idle input channels stay connected, permissions are rechecked every 20 seconds, and the browser reconnects broken display/input channels. The normal 12-hour sign-in expiry still applies.

Normal shutdown asks the guest to power off. Boot menus and unresponsive guests may ignore this request; the task reports a failure if the guest does not shut down. Force stop bypasses the normal task queue and powers off without guest cooperation, cancelling earlier power requests for that VM so a pending restart cannot start it again. Unapplied hardware changes remain saved for the next Start. Force stop is separately confirmed and may lose unsaved guest data. Deleting a VM requires its exact name and a stopped VM. Incus deletion removes its root volume and attached extra volumes created by this application. Detached volumes are retained. Deleting an existing libvirt definition retains its disk files. VM configurations are backed up before operations in `/var/lib/vm-manager/config-backups`.


On **Virtual machines**, select the checkboxes beside one or more VMs (or select
all visible rows), then choose **Shutdown**, **Restart**, **Force Stop**, or
**Delete** from the operation dropdown. Review the listed VMs and confirm once
for the entire selection. Up to four operations run concurrently; each VM has
its own task, result and audit entry. A failure does not stop the other VMs.
Changing filters removes hidden VMs from the selection.

Bulk deletion requires stopped VMs and removes all managed snapshots, including
attached data-volume snapshots, and native VM snapshots created outside the
manager. Backups are also deleted by default. In the confirmation dialog, check
**Preserve backups** beside each VM whose independent restore points should
remain in **Backups** after deletion. Snapshots cannot be preserved with a deleted
VM. Backup schedules are removed in either case. Active backups and incomplete
snapshot restores block deletion. Cleanup stops on an error for that VM; inspect
its task before retrying because already deleted restore points cannot be
recovered. Automatic configuration backups are retained. Existing libvirt disk
files and detached Incus volumes remain retained as with individual deletion.

Networks and ISO media that are still referenced cannot be deleted. Existing libvirt networks are listed for visibility; new networks are managed by Incus and can also be attached to libvirt VMs as bridges. Existing Incus ISO volumes can be used directly by Incus VMs; libvirt VMs use uploaded ISO files. The app does not modify host physical interfaces.

## Projects and tags

Open **Projects & tags** to create, rename or delete projects and tags. Each VM and network can belong to one project and carry multiple tags, including existing Incus and libvirt resources. Use **Assign project & tags** on a VM's Summary tab or **Organize** in the Networks list. Assignments can be changed while VMs are running and networks are in use.

The **Project** and **Tag** filters above each page combine to narrow VM and network lists, overview resource counts, and the resource tree. **View resources** on a project or tag opens a combined view of its VMs and networks. **Unassigned resources** and **Untagged resources** help find resources to organize; **Clear filters** restores the full view. Host CPU, memory and storage metrics always describe the whole host.

Projects are inventory groups within VM Manager, independent of Incus projects and user permissions. Assigning a VM does not assign its attached networks automatically; shared networks can remain unassigned or belong to a different project. Metadata is kept in the existing SQLite database and included in deployment backups. Deleting a project or tag removes its assignments and retains the VMs, networks and data. Administrators and operators can manage assignments; viewers can browse and filter them. Changes are audited.

## VM snapshots

Open a VM's **Snapshots** tab and select **Take snapshot**. Give the snapshot a
unique name and an optional description. Choose **Live**, **Gracefully shut down,
capture, then restart**, or **Require the VM to be stopped**. Live is the default
for new snapshot requests. A VM that is already stopped stays stopped. Snapshots capture the VM
configuration and all supported writable disks, including attached Incus custom
data volumes. Incus snapshots exclude runtime memory; libvirt live internal
snapshots also capture memory because the hypervisor requires it. Restores always
leave the VM stopped and discard runtime memory. Apply or discard pending hardware
changes first. Administrators and operators can create, restore and delete
snapshots; viewers can inspect the history. Every operation is backed up at the
configuration level and recorded in the task log and audit trail.

Each snapshot shares unchanged disk blocks through the storage backend's native
copy-on-write support. History records its parent and the current snapshot base;
after a restore, a new snapshot branches from the restored snapshot. Incus uses
native ZFS, Btrfs, thin LVM or Ceph snapshots. Directory storage and thick LVM are
rejected because they cannot provide the required incremental storage. Libvirt
uses internal qcow2 snapshots on exclusive file-backed disks; raw disks, shared
disks, host mounts, passthrough disks, NVRAM and TPM configurations are rejected
with an explanation. Read-only media are excluded. Snapshots created outside VM
Manager are listed for visibility and remain managed through the hypervisor.

**Restore** requires the VM's exact name, replaces its current disk contents and
configuration, and leaves the VM stopped. Restore the original disk attachments
first if the disk layout has changed. ZFS requires newer snapshots to be deleted
explicitly before restoring an older one, even when the host is configured to
delete newer snapshots automatically. **Delete** requires the snapshot's exact
name and removes that restore point while retaining current VM contents. Delete
child snapshots first, newest first. Delete managed snapshots before deleting a
VM so that separately snapshotted data volumes are cleaned up together.

Live snapshots are **crash-consistent**, not application-quiesced. Incus briefly
pauses the VM while capturing its root and custom data volumes, then resumes it
even if capture fails. Libvirt uses native internal disk-and-memory snapshots,
which can pause the VM while saving memory. Applications may need crash recovery
after disk restoration; unsaved data in guest memory is not part of an Incus disk
snapshot. Choose shutdown and restart when a clean guest shutdown is required.
Shutdown waits up to 60 seconds and never forces power off. Only VMs that were
running are restarted, after success or failure; pending hardware remains saved.
Force stop cancels automatic restart/resume. Restore and delete still require a
stopped VM. A failed or interrupted operation retains its manifest and appears as
incomplete. Delete
an incomplete creation, retry an incomplete deletion, or retry the same restore
after fixing the reported problem. A partial restore blocks VM Start and hardware
changes until the restore succeeds. The manifest survives service restarts and
is included in SQLite deployment backups. Snapshot creation and restore across
multiple Incus volumes are coordinated operations, not a single atomic storage
transaction; keep a VM with an incomplete restore stopped during recovery.

Snapshots stay on the VM's storage and do not protect against storage failure or
loss of the host. They are separate from independent VM backups and from the
configuration backups automatically taken before management operations. Creating
snapshots does not impose an automatic retention schedule; delete old restore
points to reclaim their uniquely retained blocks.

`scripts/snapshot_check.py` is an opt-in acceptance check against the deployed
service. Run as root on the host with the release environment and `PYTHONPATH` set
to the release directory. It creates disposable Incus/ZFS and libvirt/qcow2 VMs,
writes and restores known contents on root and data disks, verifies restored
configuration, shared storage, parent chains, permissions and deletion guards,
then removes its resources and temporary operator. Existing VMs are retained.

`scripts/bulk_check.py` is an opt-in check against the deployed service. Run as
root with the release Python environment and `PYTHONPATH`. It creates disposable
Incus/ZFS and libvirt/qcow2 VMs and backup storage, verifies simultaneous bulk/individual power
operations, same-VM hardware ordering and Force Stop cancellation, deletes snapshot chains and backups,
restores a preserved backup after its source VM is gone, and removes its resources
and temporary operator. `--browser-state <path>` allows a temporary browser check
before deletion; protect and remove the generated authentication file.

## Permissions

| Role | Inventory and tasks | VM, network and ISO operations | User administration and audit |
| --- | --- | --- | --- |
| Administrator | Yes | Yes | Yes |
| Operator | Yes | Yes | No |
| Viewer | Yes | No | No |

Console access requires the ability to operate VMs. Roles are checked on every API request and throughout console sessions. Disabled users lose access immediately. Users can change their own password; administrators can reset passwords, change roles and disable accounts. The last enabled administrator and the current administrator's own account are protected from lockout.

Authentication uses salted scrypt password hashes and 12-hour, opaque, server-side sessions in HttpOnly/SameSite cookies. HTTPS cookies are Secure. Mutations require a session CSRF token and same-origin requests. Login failures are throttled. All management tasks and account changes are audited. Task failures are visible in the task log; an interrupted task is never reported as completed.

Select a row in **Task log** or **Task history** to view live task status, updated
every second while the dialog is open. Expand **Host output** to connect to the
live output stream; collapsing it, closing the dialog or hiding the browser tab
disconnects the output stream. Reopening resumes from the last received entry.
The task continues running independently of the stream. Completed tasks display
saved output. Tasks from before this feature have their existing result only.

Output includes task milestones, Incus operation status/progress when provided
by the host, and stdout/stderr from supported host mutation commands. Quiet
operations may only report milestones; this is management output, not the guest
console. Configuration/query responses, command arguments, standard input and
binary backup data are excluded. Each task retains its latest 256 output entries
(up to 1,024 characters each) in SQLite, included in deployment metadata backups.
Omitted older output is indicated when catching up. Output is sent only to signed-in
users with task read permission; active connections recheck access continuously.

## Development and tests

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.lock
npm ci
.venv/bin/python -m pytest
npm run check
npm test
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8088
```

The local frontend loads even without hypervisors; provider connection failures are shown explicitly after login. Production requires Linux with existing Incus and libvirt. The installer provisions the app's service account, Python environment, SPICE browser client, certificate and systemd service. It does not initialize or replace either hypervisor.

`scripts/live_check.py` is an opt-in acceptance test for the target host. It creates uniquely named disposable networks, a VM, disks and an ISO, checks real hypervisor operations and the SPICE handshake, and removes its resources. Run it with a disposable `VM_MANAGER_DATA` directory. Do not point it at production account data: it resets the `admin` password in its selected test database.

`scripts/power_check.py --iso-name <uploaded-installer-name>` runs on the hypervisor against the deployed HTTPS service. It uses a temporary operator/session and disposable installer VM to verify that Force stop bypasses a waiting restart, cancels earlier queued power actions, and retains pending hardware. Run it as root with the release Python environment and `PYTHONPATH` set to the release directory. Existing VM and ISO data are retained.

## Deployment and recovery

After testing and merging into a clean local `main`:

```sh
python3 scripts/deploy.py
```

Deployment archives the exact Git commit, builds a versioned release, stops the app, takes a consistent SQLite backup, switches the current-release symlink, restarts the service, and checks live HTTPS health. Application state and ISO/disk data remain in `/var/lib/vm-manager`; source releases are under `/opt/carrtech/vm-manager/releases`; database backups are under `/opt/carrtech/vm-manager/backups`. Failed health checks restore the previous source release when available. Database backups are retained for manual recovery and do not contain guest disks or ISO media.

Useful operations:

```sh
ssh claude@192.168.8.235 'sudo systemctl status vm-manager'
ssh claude@192.168.8.235 'sudo journalctl -u vm-manager --since "10 minutes ago"'
```

### Cloudflare Tunnel

One-time provisioning from clean, integrated `main`, using an operator Mac authenticated for the `carrtech.dev` Cloudflare zone:

```sh
brew install cloudflared
cloudflared tunnel login
python3 scripts/setup_tunnel.py
python3 scripts/deploy.py
```

Provisioning creates or reuses the dedicated `vm-manager` tunnel and routes only `vmm.carrtech.dev`. Conflicting DNS records are not overwritten. The account certificate remains on the operator Mac; the tunnel credentials, configuration and origin certificate are installed outside Git under `/opt/carrtech/vm-manager/tunnel`, readable only by the connector's container user/root. Existing tunnel configuration is backed up before replacement. No Cloudflare Access policy is added; the application's existing accounts provide authentication.

Deployment maintains the pinned connector container with automatic restart, validates ingress, checks connector readiness on host loopback port `20243`, and checks public HTTPS health against the deployed revision. The connector uses host networking to reach the existing LAN HTTPS listener, retains the public Host header for CSRF and console WebSocket checks, and returns 404 for unmatched hosts. Other applications' tunnels are independent. Docker is enabled at boot on the established host.

```sh
ssh claude@192.168.8.235 'sudo docker compose -p vm-manager -f /opt/carrtech/vm-manager/current/deploy/compose.tunnel.yaml logs --tail 50 tunnel'
curl --fail https://vmm.carrtech.dev/health
```

To temporarily withdraw public access, stop only this connector with the same Compose file and `stop tunnel`; LAN access continues. A later deployment starts the configured connector again. Removing public access permanently also requires removing its DNS route and provisioning state; retain credentials securely for recovery.

The service runs as `vm-manager` in the existing `incus-admin` and `libvirt` groups. Its filesystem writes are restricted to its persistent state directory. Hypervisor group membership is powerful; protect administrator and operator accounts accordingly. This is a single-host control plane.

Frontend display software is bundled locally: noVNC (MPL-2.0, license in `app/static/vendor/novnc`), Inter (OFL, license in `app/static/fonts`) and the system `spice-html5` package (LGPL-3.0+).

## Storage blocks

Open **Storage → Create storage block**, choose an underlying ZFS pool, a name,
capacity in GiB, and one purpose: **VM disks (Incus)**, **ISO images**, or
**VM & configuration backups**. A block uses a dedicated dataset on the same physical
ZFS storage as the chosen pool. ZFS enforces its capacity limit and reserves that
capacity, so creating or expanding blocks reduces the underlying available space.
The purpose is fixed at creation. Existing Btrfs pools remain usable for VM disks;
creating bounded blocks on Btrfs is not currently supported.

VM blocks appear by name in **Create VM** and **Add disk**. **Assign defaults**
sets the initial VM storage selection, the destination for new ISO uploads, and
the destination for configuration snapshots taken before VM operations. The ISO
library also has an upload destination selector. Existing disks, ISOs and backups
remain where they are; changing a default does not migrate data. Backup blocks
hold full VM backups as well as automatic configuration snapshots. Existing libvirt disk
creation continues to use application disk storage.

Blocks can be expanded but cannot be shrunk. Deletion requires the exact name,
an empty block, no default assignment and no pending hardware references. Incus
also checks pool/volume references. Files and volumes are retained until explicitly
removed; the application never empties a block to delete it. A full, unavailable
or unmounted destination reports an error rather than writing to another disk.
An interrupted provisioning operation appears as incomplete/unavailable and needs
administrator inspection before recovery; its registry entry is retained.

Provisioning is handled by `vm-manager-storage.service`, a root service with a
local Unix socket accessible only to root and the application group. It accepts
validated storage operations, derives all dataset names and paths itself, and
checks a dataset ownership property before modification. The application service
continues to run as `vm-manager`. The root-owned storage registry is
`/var/lib/vm-manager-storage/blocks.json`; filesystem blocks mount below
`/var/lib/vm-manager-storage/mounts`. ZFS mounts them at boot. Deployment backs up
the registry alongside SQLite; these metadata backups do not contain block data.

`scripts/storage_check.py` is an opt-in acceptance check against the deployed
service. Run as root on the host with the release environment and `PYTHONPATH` set
to the release directory. It creates temporary blocks, a VM and an ISO; checks
actual hard quotas, capacity reservation, upload/backup routing, expansion and
deletion guards; restores existing defaults and removes its own resources. It
uses a temporary operator and never resets existing passwords.

## VM backups and restore

Create storage with purpose **VM & configuration backups** in **Storage**. Open a
VM’s **Backups** tab or **Options → Backup settings** to select its destination,
enable a daily or weekly schedule, set an IANA timezone and retention count.
The global **Backups** screen supports selecting several VMs for immediate backup
or applying settings to all selected VMs. Administrators and operators manage
backups; viewers can inspect schedules and restore points. Settings and the
backup catalog live in SQLite; archive files live on the selected backup block.
Full backups always require an explicit backup block and never fall back to the
system disk if it is missing, unmounted or full.

Immediate backups and schedules offer the same three capture modes as snapshots.
New dialogs default to **Live**; existing schedules retain their stopped/shutdown
policy until edited. **Live** captures a crash-consistent point in time while the
VM continues running during export. Guest memory is excluded and applications may
need crash recovery after restore. **Gracefully shut down, capture, then restart**
allows downtime for the entire backup. **Require the VM to be stopped** fails a
running VM without changing its power state. Shutdown has the existing 60-second timeout and never forces power off.
After success or export failure the manager attempts to start a VM it shut down;
it leaves pending hardware edits unapplied. Force stop cancels that automatic
restart and stays available during exports. Its configuration snapshot always
uses local application storage so a slow/full backup block cannot delay it.
If the service/host stops during backup, the task and restore point become
interrupted on startup; partial files are retained for inspection/deletion and
the VM may remain stopped or paused. Inspect its task and power state before
retrying. For an interrupted Incus live backup, `capture.json` in that backup's
archive directory lists temporary copies/snapshots for operator cleanup. They are
retained for inspection after an unclean service exit; deleting archive files does
not delete those hypervisor resources.

Incus live backups briefly pause the VM to snapshot the root and all supported
private data volumes, resume it, then export temporary stopped copies. Normal
completion/failure cleans up those copies and snapshots. Snapshot-capable storage
(ZFS, Btrfs, thin LVM or Ceph) is required for live Incus backup; use shutdown mode
on other storage. Temporary copies consume source-pool space during export.
Incus backups use portable instance exports plus separate exports for each
attached private custom data volume. They include root/firmware and current VM
configuration, excluding Incus snapshots and ISO installation media. Root exports
stream through a metadata rewrite that removes original volume/profile/network
references before import; no second full guest archive is staged locally. Host-path
data disks, shared volumes, passthrough devices and ephemeral/stateful instances
require manual backup and cause an explicit failure. Libvirt exports support
private raw/qcow2 file disks, flattening backing chains into standalone qcow2
files, plus domain XML and file-based NVRAM. TPM, passthrough, shared filesystems,
block/network disks and encrypted guest state require manual backup. Live libvirt
backups use the native point-in-time push backup API, verify successful job
completion, and include every supported data disk. NVRAM is copied during a brief
pause at capture. The broker grants QEMU access to the selected backup directory
for the job and restores the original directory ACLs afterward.
On first live libvirt export, the broker enables POSIX ACL support on an owned
backup block if it was disabled; existing files and permissions are retained.
Disk reads for libvirt exports run through the existing privileged storage broker; clients
supply VM/block/backup identifiers, never host file paths or commands.

The scheduler checks every 15 seconds while the service is running. It persists
next-run timestamps, runs once after missed downtime, and skips additional missed
slots. Calendar schedules account for the configured timezone and daylight saving
changes; a repeated clock hour does not run twice. One backup per VM can be active.
Up to four ordinary tasks run concurrently across different VMs, including backups,
snapshots, hardware changes and individual or bulk power operations. Tasks affecting
the same VM wait in submission order without occupying workers. A backup restore
reserves both its source and destination VM so archive deletion and destination
changes wait until it finishes. Shared host operations (network, storage and ISO
deletion) wait for ordinary tasks and run exclusively. Force Stop uses a separate
queue and can interrupt a VM even while all four ordinary workers are busy.
Retention removes only older successful
**scheduled** backups of that VM on the same destination, after a successful
scheduled backup. Manual, failed and interrupted backups remain until deleted.
Review failures in **Task history**; failure to queue an overlapping schedule is
recorded in the audit trail.

Select **Restore** on a completed backup, supply an unused VM name and (for Incus)
a VM disk pool. SHA-256 and file sizes are verified before restoration. Restore
creates independent disks and a stopped VM with new MAC addresses, autostart
disabled and ISO media ejected; it never overwrites an existing VM. Libvirt disks
restore to application disk storage. Network definitions and host firmware must
still exist. Guest static IPs, hostnames, credentials and application identities
remain in the disk data: review them before starting the restored VM alongside
the original. A failed restore may retain partial destination resources for
inspection; its source VM and backup remain available. Source VM deletion removes
its schedule but retains restore points. A block referenced by backup settings or
catalog records cannot be deleted.

Deployment metadata backups contain the backup catalog/plans, not guest archives.
Protect the selected backup storage independently against host/storage failure.
`scripts/backup_check.py` is an opt-in host acceptance test using temporary VMs,
a temporary backup block and a temporary operator. It checks disk content after
Incus and libvirt restores, checksum rejection, schedules, retention and cleanup.

## External SMB and S3 storage

In **Storage → Add external source**, save a named SMB share or S3-compatible
bucket. Multiple connections can coexist. SMB accepts a hostname/IPv4 address,
share, username/password, optional domain, port and subfolder. S3 accepts an HTTP(S)
API endpoint, bucket, access/secret keys, region and optional prefix. For Amazon
S3, use the appropriate regional S3 endpoint/region; for Cloudflare R2, use the
account's S3 endpoint and region `auto`. HTTPS is recommended outside a trusted
LAN. Saving tests read/list access; backup or guest writes also require write
permissions. Failed connections remain listed for correction or retesting.

Sources have no VM Manager quota, size or reservation. Any configured SMB server
quotas and permissions continue to apply. A source can be read-only. Credentials
are kept in the root-owned `/var/lib/vm-manager-storage/external.json` registry
and generated root-only files, never returned by inventory or included in task
bodies/audit logs. Deployment backs up this registry as a mode-0600 `.external.json`
file beside the existing database backup. Protect these files as credentials.

On a VM's **Hardware → Add disk**, choose a source and an empty mount path below
`/mnt` or `/media`, and optionally choose read-only access. The Linux guest must
be running with its **Incus agent** or, for libvirt, an enabled **QEMU guest agent
channel/service**, plus `/usr/bin/python3` and systemd. This is the one-time guest
integration prerequisite; VM Manager cannot configure an arbitrary OS without a
working guest agent. Windows guests are not supported by this mount installer.
For new Linux VMs, use an image with the appropriate agent prepared.

For an existing Incus VM, select **Install Incus agent** on its **Summary** or
**Hardware** tab. Open the VM console, log in and
run the provided Linux command with sudo (or as root without sudo). First-time
setup requires this guest-side step because guest commands need an agent already
running. **Check agent** verifies the connection and whether Python 3 and systemd
are available for automatic mounts. It reports an unavailable connection without
assuming the agent is absent. The command first uses Incus's built-in shared
filesystem (9p), which requires no CD. Attach the optional installer CD only if
that filesystem is unavailable, then run the command again. The CD can be
prepared while running or stopped; if it is not visible inside a running guest,
restart the VM. Guests that rely on the CD need it attached at boot for
credentials and agent updates; it need not stay mounted inside Linux. CD
preparation is queued and audited, preserves other devices and installation
media, and is safe to repeat.
Use **Prepare optional installer CD** to repair a previously attached installer
whose device has a different name. The supported CD uses the device name `agent`,
because Incus's guest startup script expects the disk serial `incus_agent`.
Preparation replaces only local generated installer CDs and retains profile
devices, ordinary disks and OS installation media. If another device already
uses `agent`, preparation stops without changes so that device can be renamed
deliberately. **Check agent** identifies installer CDs that need this repair.
On SELinux guests, the console command installs `semanage`/`restorecon` tools
through dnf or apt if needed, registers the persistent `bin_t` file context for
`/run/incus_agent/incus-agent`, and relabels an existing binary before restarting
the service. SELinux remains enabled. This handles older host installers that
only attempt the `/var/run` alias or skip labelling when `semanage` is absent.
If the installed service and cached agent binary are already present, rerunning
the command repairs permissions and restarts the service without requiring the
ejected installer CD. The persistent context also applies when the agent's boot
setup copies a fresh binary from the CD. Restarting the agent interrupts current
guest-command sessions.
This guided installer supports Linux with systemd; libvirt VMs still need their
QEMU guest agent prepared separately. See the
[Incus agent installation documentation](https://linuxcontainers.org/incus/docs/main/howto/instances_create/#install-the-incus-agent-into-virtual-machine-instances).

VM Manager installs `cifs-utils` for SMB or `rclone`/`fuse3` for S3 when necessary,
using apt or dnf. Those guests need access to their package repositories, or the
packages must already be installed (some distributions require an additional
repository for rclone). Connection credentials travel over the guest agent's
stdin, and are installed beneath `/etc/vm-manager/mounts/<generated-id>` with
root-only permissions. Each source mounts directly inside the guest; credentials
must therefore be trusted to the guest's administrators. Root disk backups also
contain these files, just as they contain other OS credentials.

A generated `vm-manager-share-<id>.service` mounts the source at boot, after
network-online, and retries failures. SMB uses the kernel's SMB3 client, preserving
server-side file locking between separate VMs. Sources can be attached to several
VMs with different paths and read-only settings. These are mounted filesystems,
not partitionable block devices. Guest users can access the mounted files subject
to the source credentials' remote permissions; use separate restricted sources
when VMs need different permissions.

S3 mounts use rclone with a persistent local read/write cache and background
uploads. The 5 GiB cache target is an eviction setting, not a hard quota; open or
pending files can exceed it and need sufficient guest disk space. A local save
can finish before its upload completes. S3 does not provide SMB's coordinated
shared-file editing semantics; avoid multiple writers to the same object and
workloads requiring strict POSIX locking, such as database data files. Other
clients' changes can remain cached. Cache/configuration survive service failures
and reboot. Do not delete a guest's cache while uploads are outstanding.

**Mounted external storage** shows the last setup/check result, not continuous
guest telemetry. **Check mount** tests current access. Incomplete setup retains
its attachment ID and configuration for **Retry setup**. **Detach** requires a
running guest, disables its generated service and retains remote files. Open S3
files, queued/failed uploads or an inaccessible upload-status service prevent
detachment; restore the connection/service, close files, wait and retry.
An unsuccessful S3 setup with an empty local cache can be detached even when its
initial connection/read is blocked; the failed service is stopped before cleanup.
Any cache containing files is retained until pending uploads can be verified.
Normal
unmount refuses busy SMB mounts. Detach all external storage before deleting a VM.
A VM restored/cloned from its system disk can retain these automatic mounts and
credentials; review them before booting it alongside the original VM.

Writable, successfully tested sources also appear in **Backups** alongside local
backup blocks. External backups use local staging below
`/var/lib/vm-manager/external-staging/<source-id>`; allow enough host space for the
full archive and concurrent jobs. Only VM disks/configuration are exported, not
files mounted inside the guest. The archive uploads into
`vm-manager-backups/<backup-id>` beneath the configured share/prefix. Remote file
contents are read back and verified before the completion manifest is published;
the complete remote archive is checked again before success is recorded. Local
staging is removed after success. Failed/interrupted staging remains recoverable
and can be removed by deleting its backup record. Restore downloads a fresh copy
and validates its SHA-256 manifest before importing. Retention uses the same
verified-upload-before-deletion policy as local backups.

Removing a source requires exact-name confirmation and no VM attachments, backup
settings or backup records. Remote files are retained when removing a connection;
deleting a backup explicitly deletes only that generated backup directory/prefix.
A source's location/access mode cannot change while backup records/settings refer
to it. Host CIFS mounts used for backup transfers are created on demand, checked
before use, and unmounted before editing/removing an unused source. Local staging
is never treated as a successful substitute for unavailable remote storage.

`scripts/external_storage_check.py --sources <root-only-json> --incus-vms
<disposable-vm-one> <disposable-vm-two> [--libvirt-vm <disposable-vm>]` is an opt-in
acceptance check for this feature. Run it as root with the release environment
and `PYTHONPATH` set. Its source JSON must contain `smb` and `s3` connection bodies
for disposable test endpoints; guests must be running Linux VMs named
`vmm-external-check-*`. It uses a temporary operator, unique mounts and files,
checks shared SMB access, mounts through both agents when supplied, and performs
real VM remote backups and stopped restores on both storage types. It cleans up
its own sources, mounts, backups, restores and account. It does not provision or
remove the supplied guests/endpoints. `--browser-state <path>` pauses before
cleanup for a browser check; protect/remove that session file, and create
`<path>.continue` to resume cleanup. Do not supply production datasets or VMs.

## Monitoring history

**Overview** has host CPU, memory and disk-space charts. Each VM's **Monitor** tab
has CPU and memory charts plus a chart for every configured data/system disk
(CD-ROM and agent installer media are excluded). Choose the last **5 minutes**,
**1 Hour**, **12 Hours**, **1 Day**, **1 Week**, **1 Month**, **6 Months**, or
**12 Months**. Charts refresh every 10 seconds while visible. Hover over a chart,
or focus it and use the arrow keys, to inspect timestamped values and min/max peaks.

A background collector samples every 10 seconds, including when nobody is signed
in. History begins at deployment; earlier usage cannot be reconstructed. SQLite
stores 10-second samples for two days, five-minute summaries for 32 days and
hourly summaries for up to 12 calendar months. Expired samples are pruned hourly;
SQLite reuses their storage. Longer chart ranges use weighted averages and retain
min/max peaks, with at most about 750 points per series. History is included in
the existing deployment database backups and survives upgrades/restarts.

Host disk space is the aggregate allocated space of underlying VM storage pools,
matching the Overview storage card (child storage blocks are excluded). Host
memory excludes available cache. VM CPU is normalized to its allocated vCPUs;
VM memory retains the guest/host-resident source labels used on Summary.

VM disk charts measure **used space in mounted guest filesystems**, summed per
configured disk, rather than virtual disk capacity, thin-provisioned host
allocation or disk I/O. Incus Linux guests need a connected Incus agent, Python 3
and `lsblk`; libvirt guests need a responding QEMU guest agent with filesystem
information support. Collection is read-only and never installs guest software,
formats disks or mounts filesystems. Disk identities are matched through guest
serials or libvirt's filesystem-to-target mapping; guest device order is never
assumed. Bind mounts are deduplicated. Filesystems spanning multiple disks,
unformatted/unmounted disks and guests without usable telemetry show unavailable.
Network SMB/S3 mounts are independent filesystems and are not counted as VM block
disk usage. Stopped VMs, unavailable metrics and service downtime create gaps,
not artificial zero usage. Collector errors are isolated from VM operations;
slow/unavailable hosts can delay a collection cycle beyond 10 seconds.
