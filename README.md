# CarrTech VM Manager

A self-hosted virtualization console for `ctlnxdbn001` (`192.168.8.235`). Manages Incus virtual machines and discovers existing libvirt machines through `qemu:///system`. Incus containers are intentionally excluded from the VM list.

## Open the manager

After deployment, open **https://192.168.8.235:8443** on the local network. The installer generates a server certificate for this address. Trust that certificate on your devices to avoid browser certificate warnings.

The first run creates `admin` with a random password in `/var/lib/vm-manager/initial-admin.txt` (readable by the service account and root). Retrieve it over your existing SSH connection:

```sh
ssh claude@192.168.8.235 'sudo cat /var/lib/vm-manager/initial-admin.txt'
```

Change the password through **Account** after signing in; the initial credentials file is removed when the password is changed. Account data and sessions are stored in SQLite; no external identity provider is required.

## Workflow

1. Create a **Network**. NAT provides outbound connectivity; isolated networks disable IP forwarding. DHCP and a non-overlapping subnet can be configured automatically.
2. Upload an operating-system `.iso` in **ISO library**. Existing Incus ISO volumes are also discovered. Files are validated as ISO9660/UDF, hashed with SHA-256, and stored persistently. Maximum size is 32 GiB, subject to available host space.
3. Select **Create VM**, choose a network and storage pool, set CPU, memory, root disk and optional extra disks, and attach the installation ISO.
4. Start the VM and open **Console** to install the operating system. Incus uses the host's SPICE console; libvirt uses its local VNC display.
5. Eject its ISO through **Hardware**. For a running VM this is saved as a pending change; use **Restart** in VM Manager to apply it and boot from disk.
6. CPU, memory, disks, network adapters, CD-ROMs, Secure Boot and autostart can be changed through the VM workspace. While running, hardware changes are saved persistently and shown as **Current → After restart** in the Hardware tab. Use **Restart** in VM Manager to shut down gracefully, apply the pending changes and start again, or shut down and use **Start**. Rebooting only inside the guest does not apply these pending changes. Stopped VMs apply edits immediately. Pending changes can be discarded before application. A failed change leaves the VM stopped with remaining changes retained; inspect the task log before retrying. Disk expansion does not expand the guest filesystem; complete that inside the guest.

VM summaries refresh CPU and memory usage every five seconds, separately from configured resource limits. CPU percentage is normalized across allocated vCPUs. Memory uses guest usage when reported; otherwise it is explicitly labelled as host VM resident memory, which includes VM overhead and is not guest application usage. Missing measurements are shown as unavailable.

Console WebSockets send ping/pong keep-alives every 20 seconds. Idle input channels stay connected, permissions are rechecked every 20 seconds, and the browser reconnects broken display/input channels. The normal 12-hour sign-in expiry still applies.

Normal shutdown asks the guest to power off. Force stop is separately confirmed and may lose unsaved guest data. Deleting a VM requires its exact name and a stopped VM. Incus deletion removes its root volume and attached extra volumes created by this application. Detached volumes are retained. Deleting an existing libvirt definition retains its disk files. VM configurations are backed up before operations in `/var/lib/vm-manager/config-backups`.

Networks and ISO media that are still referenced cannot be deleted. Existing libvirt networks are listed for visibility; new networks are managed by Incus and can also be attached to libvirt VMs as bridges. Existing Incus ISO volumes can be used directly by Incus VMs; libvirt VMs use uploaded ISO files. The app does not modify host physical interfaces.

## Permissions

| Role | Inventory and tasks | VM, network and ISO operations | User administration and audit |
| --- | --- | --- | --- |
| Administrator | Yes | Yes | Yes |
| Operator | Yes | Yes | No |
| Viewer | Yes | No | No |

Console access requires the ability to operate VMs. Roles are checked on every API request and throughout console sessions. Disabled users lose access immediately. Users can change their own password; administrators can reset passwords, change roles and disable accounts. The last enabled administrator and the current administrator's own account are protected from lockout.

Authentication uses salted scrypt password hashes and 12-hour, opaque, server-side sessions in HttpOnly/SameSite cookies. HTTPS cookies are Secure. Mutations require a session CSRF token and same-origin requests. Login failures are throttled. All management tasks and account changes are audited. Task failures are visible in the task log; an interrupted task is never reported as completed.

## Development and tests

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.lock
npm ci
.venv/bin/python -m pytest
npm run check
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8088
```

The local frontend loads even without hypervisors; provider connection failures are shown explicitly after login. Production requires Linux with existing Incus and libvirt. The installer provisions the app's service account, Python environment, SPICE browser client, certificate and systemd service. It does not initialize or replace either hypervisor.

`scripts/live_check.py` is an opt-in acceptance test for the target host. It creates uniquely named disposable networks, a VM, disks and an ISO, checks real hypervisor operations and the SPICE handshake, and removes its resources. Run it with a disposable `VM_MANAGER_DATA` directory. Do not point it at production account data: it resets the `admin` password in its selected test database.

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

The service runs as `vm-manager` in the existing `incus-admin` and `libvirt` groups. Its filesystem writes are restricted to its persistent state directory. Hypervisor group membership is powerful; protect administrator and operator accounts accordingly. This is a single-host LAN control plane, not an internet-facing multi-tenant service.

Frontend display software is bundled locally: noVNC (MPL-2.0, license in `app/static/vendor/novnc`), Inter (OFL, license in `app/static/fonts`) and the system `spice-html5` package (LGPL-3.0+).
