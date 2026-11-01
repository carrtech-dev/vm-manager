# CarrTech VM Manager

A self-hosted virtualization console with linked-host cluster management. The established installation runs on `ctlnxdbn001` (`192.168.8.235`). Each installation manages its own Incus virtual machines and discovers existing libvirt machines through `qemu:///system`. Incus containers are intentionally excluded from the VM list.

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
Operators always use guest login. Logging out returns to the guest login prompt
in the same terminal, retaining its scrollback. **Reconnect to login** starts a
fresh guest login session, including after using root access. The guest must
provide a Linux `login` program; if it is unavailable, use the graphical console. SELinux guests
use a temporary systemd getty session so guest policy can transition normally
from login to the user’s shell. This requires `systemd-run`, `systemctl`, `agetty`,
`chcon` and `tty` inside the guest. Only the session’s disposable PTY is
relabeled; SELinux enforcement and policy are retained. Disconnecting stops the
temporary login service.
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

The **Overview** and **Virtual machines** lists group VMs into project folders across both hypervisors, with **Unassigned** VMs in their own folder. Click a folder to expand or collapse it; its state is retained during live refreshes and navigation. Folder counts reflect the current filters. Collapsing a folder clears its selections, and **Select all** selects only the VM rows in expanded folders.

The **Project** and **Tag** filters above each page combine to narrow VM and network lists and overview resource counts on the selected host. The resource tree keeps showing VMs across the whole cluster. **View resources** on a project or tag opens a combined view of its VMs and networks. **Unassigned resources** and **Untagged resources** help find resources to organize; **Clear filters** restores the full view. Host CPU, memory and storage metrics always describe the whole host.

Overview includes every VM in the current project/tag view so folders are never truncated; unassigned VMs appear last.

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

## Operations: Linux patching

Open **Operations** in the sidebar, select one or more VMs and choose **Check
selected**. The check verifies the actual Linux guest, root guest-agent execution,
Python 3, systemd, its native package manager and minimum free disk space. Windows
and unsupported guests cannot be patched. Supported distribution families are
Debian 12+, Ubuntu 22.04+, Rocky Linux, AlmaLinux and RHEL 8+. Incus VMs need a
connected Incus agent; libvirt VMs need an enabled QEMU guest agent with
`guest-exec` and `guest-exec-status` permitted. No SSH credentials are required.
Guest repository access and subscriptions, where applicable, remain the guest
administrator's responsibility.

Choose **Patch selected now**, or **Create schedule** for daily/weekly maintenance
with an IANA timezone, start time and maintenance window. Edit a named schedule
to change its VM membership, disable it, or adjust its policy. Schedules and
results live in SQLite and are included in deployment backups. Administrators
and operators manage patches and schedules; viewers can inspect results.

Patching refreshes configured repositories and installs available package updates
with APT or DNF. This covers system packages, not separately managed applications,
containers or distribution release upgrades. APT retains existing configuration
files and package holds, permits new dependencies, and refuses package removals;
DNF uses normal upgrade policy without `--allowerasing`. Both retain normal
signature verification. Updates can restart guest services even when rebooting
is disabled. Guest package-manager locks are respected, never deleted. There is
no automatic snapshot or rollback; create an appropriate backup before maintenance
when required by your recovery policy.

The default policy patches one VM at a time, stops remaining VMs after a failure,
and reports reboot requirements without rebooting. Concurrency is configurable
from one to four. Automatic reboot, when explicitly enabled and a reboot is
known to be required, waits for a changed Linux boot ID and a responding guest
agent. Reboot need may be **Unknown**, especially without the distribution's
restart-detection utilities; inspect these guests manually. Package output,
updated-package count and up to 200 package names appear under each run's
**Details**, with task and audit records for management actions.

Schedules are checked every 15 seconds while VM Manager is running. An expired
window is skipped, including after downtime. A run that is queued behind other
work must still start within its window; an in-progress transaction and its
optional reboot may finish after that window. Calendar scheduling handles
daylight-saving changes without repeating a local daily slot. Stopped or
unavailable guests are skipped without starting them. **Cancel pending batch**
cancels only undispatched VMs; guest package transactions continue to completion.
Deleting a schedule removes future runs and retains queued/running operations.
Deleting a VM removes it from future patch schedules and retains its run history.

Each guest transaction runs in an independent systemd unit and stores its state
and output under root-only `/var/lib/vm-manager-patching`. A manager restart or
temporary agent disconnect reconnects to the original transaction ID rather than
launching another upgrade. A reconnecting transaction blocks new patch runs and
ordinary VM mutations until reconciled; Force Stop remains available as the
emergency action. A guest reboot or terminated worker during package installation
is recorded as interrupted. Inspect the guest's package database before starting
a new patch after an interrupted or failed transaction. Guest operation records
remain for recovery and can be removed by the guest administrator once the
corresponding operations are terminal and no reconnect is pending.

`scripts/patching_check.py` is an opt-in deployed acceptance check. It requires
root, pre-prepared disposable Linux VMs named `vmm-patch-check-*`, and the release
Python environment with `PYTHONPATH` set to that release. It installs actual
updates on only those guests, checks both roles, duplicate guards, scheduled runs
and missed-window handling, and removes its own temporary users and schedules.
`--restart-check` kills/restarts the VM Manager process during a guest transaction
to verify crash recovery; run outside other management work. `--reboot-check`
marks a disposable Incus APT guest reboot-required and verifies an actual reboot.
Existing accounts, VMs and schedules are retained. The
caller creates and removes the disposable guests; never pass production guests.

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

## VM templates

Open a VM and select **Create template**, or use **Templates → Create template**.
Choose a unique template name, an optional description, and a backup storage block
or writable SMB/S3 source. Templates use the portable full-archive engine for
both Incus and libvirt, including supported private data disks and configuration.
Choose stopped-only capture (the default), graceful shutdown/capture/restart, or
live crash-consistent capture. The backup engine's disk, device, live-capture and
local-staging restrictions below also apply to templates. The source VM is retained.

In **Templates**, select **Create VM**, enter an unused VM name and, for Incus,
choose VM disk storage. The new VM uses the template's hypervisor and configuration,
has independent disks and new VM UUIDs/MAC addresses, and remains stopped with
autostart disabled and ISO media ejected. Libvirt disks use application disk storage.
Edit CPU, memory or network settings on the new VM before starting it if needed.
Prepare/generalize the guest before capture, or review copied hostnames, static
IPs, machine/application identities, secrets and automatic external mounts before
starting copies alongside other VMs. VM Manager does not rewrite guest disk contents
or generalize the installed OS. Shared/external data and host network definitions
are not copied. Failed creation can leave partial destination resources for inspection.

Templates are independent, reusable capture points: changing/deleting the source
VM or any copy does not change the template. Bulk VM deletion and backup retention
preserve templates. Only explicit, exact-name-confirmed template deletion removes
the template/archive; existing VMs remain. Failed/interrupted captures remain in
the library for inspection/deletion. Templates occupy full archive space until
deleted and keep their storage referenced. Template capture, creation and deletion
appear in Task history and the audit trail. Administrators and operators manage
templates; viewers can browse. Deployment SQLite backups include template metadata,
not guest archives; protect the chosen storage independently.

`scripts/template_check.py` is an opt-in root-only acceptance check on the
hypervisor using the deployed service. It creates uniquely named disposable VMs,
a temporary backup block and an operator/session. It checks both providers,
repeated creation, independent disk contents/identities, source deletion, template
deletion, checksum rejection and storage guards, then removes its own resources.
Do not substitute production VMs or disks into this check.

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
packages must already be installed. On Rocky Linux and AlmaLinux 8 or later,
missing rclone installs enable EPEL and use PowerTools (8) or CRB (9+) for that
package transaction. Other distributions may need rclone's repository prepared
manually. Package installation failures identify the required packages so they
can be installed before retrying the existing attachment.
Connection credentials travel over the guest agent's
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
uploads. Existing buckets use object-scoped credentials without bucket creation
checks. Cloudflare R2 uploads skip rclone's unsupported version-specific HEAD
request after upload; PUT checksum validation remains enabled, and backup
transfers still verify content by downloading it before publishing a manifest.
The 5 GiB cache target is an eviction setting, not a hard quota; open or
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


## Linked-host cluster management

Install VM Manager on each Linux host with existing Incus and libvirt. Each host
keeps its own VMs, disks, networks, ISO library, projects, tags, snapshots, backup
plans, monitoring and task history. The left resource tree shows the local manager and up to 32 explicitly linked
hosts as independently collapsible groups, each containing that host’s VMs.
Groups use the actual hostname, refresh on every page, and remember their collapse
state across navigation and host switches. The **Cluster** page manages host
membership, connection status and incoming link keys. VM identity includes
its owning host, so identical VM names on different hosts remain independent.

Deploy from clean integrated `main`, supplying the target SSH account and HTTPS
listen IP. The installer generates a certificate for that address and configures
the service accordingly; existing certificates are preserved. Use a stable IP or
DHCP reservation. Python 3.13 and Debian 13 are the established deployment target.
The installer does not initialize or replace hypervisors.

```sh
python3 scripts/deploy.py --host administrator@192.168.8.236
# A private host reachable through an SSH jump host:
python3 scripts/deploy.py --host root@10.100.0.119 --jump claude@192.168.8.235
# When the SSH name differs from the listen address:
python3 scripts/deploy.py --host administrator@other-host --address 192.168.8.236
```

1. Sign into the additional host as an administrator. Open **Cluster**, choose
   **Create link key**, name the connection and copy the key shown once.
2. On the manager you want to use as your central interface, open **Cluster** →
   **Link host**. Supply a name, HTTPS origin, and the additional host's key.
3. For the installer's self-signed HTTPS certificate, copy the public contents of
   `/opt/carrtech/vm-manager/tls/server.crt` from the additional host into the
   certificate field. For public HTTPS, leave the field blank. Certificate and
   hostname verification remain enabled; redirects and environment HTTP proxies
   are not followed. The central server must be able to reach the additional host.
4. Choose a VM under its host in the left resource tree, or use **Resources on**
   in the top bar to select a host. All existing VM, network, storage, ISO,
   template, backup, Linux patching, metrics and task controls then operate on that host.
   Graphical consoles, text terminals, ISO uploads and live task output pass
   through the central server; the browser needs access only to that server.

Switching hosts reloads the page, closing active consoles and task streams and
clearing resource selections. Host ownership stays in the URL, including viewer
pop-outs. Do not switch hosts during an upload. Access control and your own
account always refer to the manager you signed into; audit and task history refer
to the selected resource host. The resource tree covers all hosts regardless of
host-local project/tag filters; its search matches VM names across the cluster. Unavailable hosts are identified explicitly and
do not hide healthy inventories. An interrupted request is never automatically
replayed: inspect the owning host's task history before retrying a mutation.

Links are directional. To use another manager as a central interface too, create
the required links there. A central manager uses its own users and roles: viewers
can inspect resources, operators can operate them, and administrators can also
open root terminals. Only administrators can link/unlink hosts or create/revoke
keys. Link keys trust the other manager to assert users and roles, including
administrator access to resources; link only managers whose administrators you
trust. Peer credentials cannot administer accounts or other trust links, and
links do not recurse. Remote audit entries identify the incoming key name and
central username. Existing browser session/CSRF and console origin checks still
apply; central session/role checks continue throughout forwarded streams.

**Unlink** removes only that connection and retains remote VMs/data. **Revoke**
on the resource host withdraws an incoming key; established streams close at
their next authorization check (within 20 seconds). To rotate a key, create a
replacement on the resource host, unlink/relink it at the central manager, then
revoke the old key. Incoming keys are stored as SHA-256 digests; outbound link
credentials are stored in the mode-0600 manager SQLite database and its deployment
backups. Protect these backups as credentials. Keys never appear in inventory,
ordinary key listings, browser storage or audit logs.

This is federated management, not an Incus storage/compute cluster. It does not
provide live migration, shared disks, automatic scheduling or automatic failover.
A managed VM can host a second manager for testing, provided nested KVM is
available to run its own VMs. Its availability then depends on its parent host;
use independent physical hosts for independent failure domains.

## DevOps: Kubernetes clusters

DevOps is included and enabled by default. The server-owned feature registry in
`app/features.py` is the shared entitlement boundary for all DevOps tools. Set
`VM_MANAGER_DEVOPS_ENABLED=false` in the service's environment and restart
`vm-manager` to hide DevOps and reject its APIs, credentials, agent calls and
terminal connections, including linked-host forwarding. Apply this on every
manager where DevOps should be unavailable. Existing clusters, workloads and
deployment records are retained and continue running; re-enable with `true`.
This is the foundation for a future separately installed, licensed DevOps add-on;
licence validation, purchase, activation and package installation are not yet
implemented. A future licence provider can replace the bundled grant centrally.

Open **DevOps → Deploy Kubernetes cluster**. Enter a DNS-style cluster name,
choose **1, 3 or 5 masters** and **0–20 workers**, then select the VM Manager host,
vCPU count, memory, system disk, storage pool and network for **each VM**. Masters
require at least 2 vCPUs and 2 GiB RAM; workers require at least 1 vCPU and 1 GiB.
The form totals the requested resources before submission. Distribute HA masters
across independent physical hosts and use fast disks for etcd. Multiple VMs on
one physical host do not provide host failure tolerance.

Choose **Use existing networks** or **Create a network**. Creating a network
shows its settings form:

- **Private bridge**: network name, IPv4 gateway/subnet, DHCP and outbound NAT.
  Automatic provisioning requires DHCP. This option supports a single host.
- **Shared LAN**: network name and the parent interface on each chosen host.
  This creates managed Incus macvlan networks on those interfaces. The interfaces
  must already connect to the same LAN (or networks with working mutual routing),
  with DHCP and internet access. It does not create a tunnel, VLAN, router or
  cross-host overlay. Macvlan guests cannot directly reach their own parent host;
  the built-in terminal uses the Incus agent and remains available.

Existing managed Incus bridges and macvlan networks are supported. A trust link
between managers provides orchestration, not connectivity between guests.
Across hosts, private NAT networks are insufficient without routing configured
outside this feature. Nodes must reach masters on TCP 6443; etcd masters need
TCP 2379–2380; nodes need TCP 10250 and UDP 8472 for kubelet and Flannel VXLAN.
Keep these ports on trusted networks, especially UDP 8472. Every joining node is
checked for TCP connectivity to the first master. Completion requires every
requested Kubernetes node to report **Ready**. Check pod connectivity and your
application's ingress/storage requirements before running workloads.

The service provisions Ubuntu 24.04 cloud-image **virtual machines** through
Incus, waits for cloud-init and the guest agent, and installs the pinned
[K3s v1.35.9+k3s1 release](https://github.com/k3s-io/k3s/releases/tag/v1.35.9%2Bk3s1).
The amd64/arm64 binaries are verified against pinned SHA-256 checksums. Guests
need outbound HTTPS for the Ubuntu image, Ubuntu packages, GitHub release assets
and container registries. No host-level Kubernetes installation is performed.
K3s runs as a systemd service with embedded etcd, Flannel, CoreDNS, Traefik,
ServiceLB and local-path storage defaults. Masters can also run workloads.
Pod and service CIDRs are configurable and must not overlap each other or host
networks. Bridge node addresses receive Incus DHCP reservations; shared-LAN
addresses need reservations in your LAN DHCP server. Keep these addresses stable.

The DevOps page shows deployment milestones, per-node results and task output.
Once Ready, choose **Manage cluster** to open its dedicated management page
(`/#devops/<cluster-id>`) with live node and pod tables, all-namespace
or individual-namespace filtering, search and **Describe** details. The view
refreshes every 15 seconds while the resource list is visible; refresh failures
are explicit and retain the last successful results. Node readiness, roles,
addresses and Kubernetes versions are shown alongside pod phase/waiting reason,
container readiness, restarts, scheduling node, IP and controlling workload.
The page supports direct links, reload and browser Back/Forward. Descriptions,
confirmations and pod terminals stay within the page. Background manager polling
preserves filters and terminal connections; leaving the page closes its terminal
and stops its resource refreshes. **DevOps clusters** returns to the cluster list.

Viewers can list and describe resources. Operators/administrators can **Delete**
pods, **Restart** controller-managed pods and open a **Terminal** in a selected
running container using `/bin/sh` or `/bin/bash` (the image must include that
shell). Delete and restart require typing the pod name. Both use graceful deletion
with a Kubernetes UID precondition, so a refreshed pod with the same name is
not accidentally deleted. Restart requests deletion and lets an existing
ReplicaSet, StatefulSet, DaemonSet or ReplicationController create its replacement;
it does not claim the replacement is ready. Standalone and Job pods cannot be
restarted here, static mirror pods cannot be deleted here, and controller-owned
pods may be recreated after ordinary deletion. These operations can interrupt
workloads and lose temporary pod data. Terminal targets are checked again on
connect, preserve existing session/CSRF/origin checks and use the dedicated
non-root Kubernetes guest account. Describe, mutations and terminal opens are
audited without recording shell input/output. All requests use the owning
master's linked host when needed; the manager does not store kubeconfig copies.
Deployment authoring, Services and Ingress are also available as described below;
advanced storage authoring remains available through kubectl.

Once Ready, open **kubectl terminal** inside VM Manager. It uses the first
master's kubeconfig from a dedicated guest account without sudo; it grants
Kubernetes cluster administrator access. `kubectl get nodes` and
`kubectl get pods -A` work immediately. It includes the existing terminal's paste,
copy, download and full-screen controls. A lost connection never replays input.
The terminal session and kubeconfig download require an operator/administrator,
with session, CSRF, same-origin and ongoing permission checks. Credential reads
and terminal opens are audited; credentials and terminal input/output are not
written to task logs or inventory. The kubeconfig remains in the guest rather
than the manager's deployment database.

**Download kubeconfig** provides administrator credentials for external clients.
Its API endpoint targets the first master's address, which must be reachable
from your client. Multiple masters provide etcd/control-plane quorum; a highly
available external API endpoint needs a separately configured load balancer and
matching TLS SANs. The built-in terminal also depends on the first master.

Failed/interrupted deployments retain their VMs and progress. Inspect the task
and, for guest installation errors, `/var/lib/vmm-k3s/install.log` inside the VM,
then choose **Retry deployment**. Retry checks ownership and reuses existing
nodes; it refuses unrelated VMs with matching names. Restarts mark unfinished
operations interrupted and never claim success. Do not manually delete or
replace a master to repair a deployment; recovery of an existing etcd cluster
requires its datastore recovery procedure. **Delete cluster VMs** requires the
exact cluster name and permanently removes its owned VMs/disks/workloads;
managed snapshots are removed, while independent backups and networks are retained.
Active backups/patches, incomplete restores and attached external storage block
deletion. Node configuration backups are taken before deletion. Failed deletion can be retried. Cluster upgrades, scaling,
storage replication and Kubernetes datastore backups are
not automated by this service.

Every participating manager needs this DevOps release. Upgrade linked managers
with the established `scripts/deploy.py --host ...` procedure before selecting
them. Unreachable/older hosts are shown as unavailable in the deployment form.

`scripts/devops_check.py` is an opt-in acceptance check that creates a uniquely
named disposable three-master/one-worker cluster across two linked managers.
Run with the coordinating manager's Python environment and `PYTHONPATH`,
`--state /var/lib/vm-manager`, `--url https://<coordinator>:8443`,
`--certificate /opt/carrtech/vm-manager/tls/server.crt`, `--peer-id <linked-host-id>`,
and either `--local-network <name> --peer-network <name>` for existing mutually
reachable networks, or `--local-parent <interface> --peer-parent <interface>`
for a new shared LAN. `--local-pool`/`--peer-pool` select storage. The test uses a
temporary operator, verifies all nodes, downloaded kubeconfig authentication,
cross-host pod HTTP, service DNS and the proxied non-root kubectl terminal,
then removes its VMs and account. It retains networks for inspection/reuse.
Failures retain VMs for diagnosis by default; `--resume-id` retries a retained
acceptance deployment and `--cleanup-on-failure` opts into deletion on failure.
Use only hosts with sufficient free CPU, memory and guest-expanded disk space.

### Container deployment, Services and Ingress

**DevOps → Manage cluster** includes Deployments, Services and Ingresses alongside
nodes and pods. Operators can create a Deployment from a container image, set
replicas and resource requests, update its image/replica count, and create
Services and Ingress rules in an existing namespace. Deployment labels use
`app: <deployment-name>`; Service selectors use the same label. The built-in
Traefik ingress class is `traefik`. An optional TLS Secret must already exist in
the namespace. Private registry credentials, environment Secrets and persistent
volumes can be configured through kubectl. Creation never overwrites an existing
object. Image updates and deletions verify the current UID; graphical editing
and deletion are limited to objects labelled as managed by VM Manager. Deleting
a Deployment retains related Services, Ingresses and storage.

### Managed load balancer

Open **DevOps → Manage load balancer** (`/#devops/load-balancer`). An administrator
can **Launch load balancer** explicitly for hostname routing, but Kubernetes needs
no setup clicks: Ready DevOps clusters connect automatically, and the first
application `LoadBalancer` Service launches the shared edge on demand. The app manages digest-pinned Caddy
and HAProxy containers on this manager's host using the existing privileged
local broker, without granting the web service access to the Docker socket.
Docker must already be installed and running. Configuration changes are queued
as audited tasks, validated before reload, and reconciled after restarts. Caddy's
management API listens only on host loopback; HAProxy uses graceful master-worker
reloads. Existing services using a requested port are retained and the task
reports a conflict. The edge is one host, not a redundant VIP or HA pair.

Caddy shares **443** using hostname routing. Three publication methods are
available per hostname:

- **Let’s Encrypt:** automatic issuance and renewal, with a contact email.
  Configure public DNS and firewall/NAT forwarding so TCP 443 reaches this host.
  TLS-ALPN validation avoids taking over an existing service on port 80.
- **Supplied certificate:** paste a PEM certificate chain and matching private
  key. Expired certificates and hostname mismatches are rejected. Edit the route
  to replace the pair; leaving both fields blank retains the stored pair.
- **Cloudflare Tunnel:** public TLS terminates at Cloudflare; one shared tunnel
  carries traffic to the load balancer's loopback HTTP listener. Caddy makes all
  application-routing decisions. Cloudflare's tunnel configuration has one
  transport origin, without per-application routing rules.

Choose **VM Manager** as the backend to publish the app on another hostname with
Let’s Encrypt or a supplied certificate, independently of Cloudflare. This uses
the fixed existing HTTPS origin with certificate verification.

Routes can balance across IPv4:port HTTP upstreams or a Kubernetes HTTP Service
with NodePorts. To publish cluster Ingress rules, target **kube-system/traefik,
port 80**, and use the same hostname as the Ingress. The edge terminates HTTPS;
Traefik handles the Kubernetes Ingress paths. Node addresses and NodePorts must
be reachable from the edge host; macvlan guests cannot reach their own parent
host, and host-to-guest macvlan isolation also needs a separately configured
network path. Direct VM Manager access remains on **8443** for recovery.

On the established installation, launch creates protected VM Manager routes for
LAN HTTPS and the existing public hostname. It backs up the original tunnel
configuration, verifies the proxy origin, switches the existing connector to
Caddy, verifies public health, and restores the old origin if verification fails.
The dedicated tunnel is reused; unrelated applications' tunnels are untouched.
Do not stop the edge while its tunnel is pointing at it. To recover manually,
restore `/var/lib/vm-manager-edge/tunnel-origin.json` to
`/opt/carrtech/vm-manager/tunnel/config.yml` preserving its owner/mode, then restart
`vm-manager-tunnel-1`. The LAN 8443 endpoint remains available throughout.

For automatic tunnel/DNS management, use **Cloudflare connection** and supply
an API **token**, account ID and zone ID. Scope it to Account Cloudflare Tunnel
Edit and Zone DNS Edit/Zone Read. The app reuses the established tunnel, or
creates one remotely managed tunnel on a new installation. Additional hostname
CNAME records point to this same tunnel. Existing conflicting DNS records are
never overwritten; deletion removes only DNS records created and tracked by
this app. Existing records and the shared tunnel are retained. Token replacement
is supported; the connected account cannot be changed under an existing tunnel.
Cloudflare must have certificate coverage for the chosen hostname (deeper
subdomains may require additional Cloudflare certificate coverage).

The native Service controller automatically discovers Ready DevOps clusters.
Create a plain Kubernetes Service through kubectl, Helm or the graphical form:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  type: LoadBalancer
  selector:
    app: web
  ports:
    - port: 8080
      targetPort: 80
      protocol: TCP
```

No `loadBalancerClass`, Launch or Connect step is required. VM Manager installs a
cluster-local admission service automatically on the control-plane VMs before a
new cluster is marked Ready, and upgrades existing Ready clusters automatically.
A narrowly scoped admission webhook supplies `vmm.carrtech.dev/edge` while new
application LoadBalancer Services are created. Explicit classes belonging to
other providers, opt-outs, ClusterIP/NodePort Services and platform namespaces
`kube-system`, `kube-public` and `kube-node-lease` are excluded. Existing classless
Services retain their previous provider because Kubernetes forbids changing an
existing LoadBalancer Service's class. Recreate one when intentionally moving it
to the shared edge; existing resources are never deleted automatically.

The admission service uses a dedicated unprivileged guest account, an internal
TLS certificate stored in a private Kubernetes Secret and guest files, and a
Service/EndpointSlice targeting the control-plane VMs on TCP **19443**. Keep this
port reachable between control-plane nodes on the cluster's trusted network.
No new image pull or external credentials are needed. Admission is restricted to
new classless application LoadBalancer Services, so ordinary service creation
continues if admission is unavailable; affected LoadBalancer creation fails
explicitly instead of silently using the wrong provider.

The controller discovers Services through their owning masters, configures HAProxy
listeners to node IPs/NodePorts, and publishes the edge host's IPv4 address in
`status.loadBalancer` only after successful application. It uses the authenticated
VM Manager REST API and the Kubernetes API via the existing owned guest channel;
no cluster-admin kubeconfig or new bearer secret is copied into the manager.
Automatic discovery and reconciliation run every 15 seconds; first publication
can take additional passes while the runtime starts and K3s finishes handover.
Changes and deletion from ordinary kubectl clients are picked up automatically. A cluster connection requires the current
release on its owning manager, including linked hosts.

TCP external ports **1024–65535** are supported, with `externalTrafficPolicy:
Cluster` and allocated NodePorts. UDP, source-IP preservation, allocation of
additional public/VIP addresses and `allocateLoadBalancerNodePorts: false` are
not supported. Ports 80, 443, 8443, 18080 and 20191 are reserved; port conflicts
remain pending with an explanation on the load balancer screen. Share web HTTPS
through hostname routes. Administrators can **Pause automatic integration** for
a cluster; this opt-out
survives polling/restarts, removes TCP listeners and clears the controller’s
Service addresses. Resume restores automatic integration. Individual classless
Services can opt out using the annotation
`vmm.carrtech.dev/load-balancer: disabled` before they are claimed. An assigned
Kubernetes load-balancer class is immutable: moving an already claimed Service
to another provider requires recreating it. Hostname routes remain separately managed.
A temporarily unreachable cluster retains its last successful routes and reports
a degraded state; confirmed resource deletion withdraws its listeners.

The read-only inventory endpoint is `/api/devops/load-balancer`; launch,
reconcile, routes, cluster bindings and Cloudflare settings are under that prefix.
Mutations require an administrator, session/CSRF checks and the DevOps entitlement.
Operators can author cluster workloads; viewers can inspect resources and edge
status. Private keys and API/tunnel tokens are omitted from inventory and task
logs. Settings are stored in the private manager database; runtime certificates,
Caddy renewal state and the original tunnel configuration live in root-only
`/var/lib/vm-manager-edge`. Deployment takes an additional private `.edge.tar.gz`
backup alongside the SQLite backup. Keep both when recovering an edge installation.

`scripts/edge_check.py` is an opt-in live check on the manager host, requiring
`--state`, `--url`, `--certificate` and an explicitly selected `--cluster-id`.
Use a Ready cluster with room for one disposable nginx pod and a reachable Traefik
NodePort. Creating the test Service may start the managed edge and switch the
established VM Manager tunnel automatically; use only when this is intended.
The default check never calls Launch or Connect. It uses a plain, classless
`kubectl expose --type=LoadBalancer` request on a first master local to the
manager host, then verifies automatic discovery and adoption. It creates a
temporary administrator
and uniquely named Deployment, Service, Ingress and manual certificate route,
verifies native TCP traffic and trusted HTTPS/SNI through the Ingress, and removes
its resources/account. It retains the automatically connected cluster and
running edge,
without changing any integration opt-outs. `--port` selects a free TCP listener (default 18088).
