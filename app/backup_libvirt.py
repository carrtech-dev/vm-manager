"""Libvirt offline export, native live backup and restore.

The storage broker derives export destinations from its root-owned registry and
reads disk paths only from the hypervisor's existing inactive domain definition.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from .hypervisor import HypervisorError, Libvirt, name, run


def validate(root):
    if any(root.find('./devices/' + kind) is not None for kind in ('hostdev', 'filesystem', 'tpm')):
        raise HypervisorError('Passthrough, shared filesystems and TPM state require manual libvirt backup')
    if root.find('launchSecurity') is not None:
        raise HypervisorError('Encrypted guest state requires manual backup')
    disks = root.findall('./devices/disk')
    if not any(d.get('device') == 'disk' for d in disks):
        raise HypervisorError('VM has no supported disks')
    for disk in disks:
        if disk.get('device') == 'cdrom':
            continue
        driver, source = disk.find('driver'), disk.find('source')
        if (disk.get('device') != 'disk' or disk.get('type') != 'file' or
                source is None or not source.get('file') or disk.find('shareable') is not None or
                driver is None or driver.get('type') not in ('raw', 'qcow2')):
            raise HypervisorError('Only private raw/qcow2 file disks support automatic libvirt backup')


def live_export(domain, vm_name, root, target):
    # The distribution's binding matches the host libvirt ABI; keep it out of
    # the portable macOS development environment and PyPI lock file.
    sys.path.append('/usr/lib/python3/dist-packages')
    import libvirt

    xml = ET.Element('domainbackup', mode='push')
    disks_xml = ET.SubElement(xml, 'disks')
    disks = []
    for index, disk in enumerate(root.findall('./devices/disk')):
        device = disk.find('target').get('dev')
        if disk.get('device') == 'cdrom' or disk.find('readonly') is not None:
            ET.SubElement(disks_xml, 'disk', name=device, backup='no')
            continue
        filename = f'disk-{index}.qcow2'
        node = ET.SubElement(disks_xml, 'disk', name=device, backup='yes', type='file')
        ET.SubElement(node, 'driver', type='qcow2')
        ET.SubElement(node, 'target', file=str(target / filename))
        disks.append(dict(index=index, file=filename))
    # QEMU writes only to the chosen backup block. Grant traversal through its
    # private parents for this job, restoring each original ACL afterward.
    directories = [target.parent.parent.parent, target.parent.parent, target.parent, target]
    original_acls = [(directory, run('getfacl', '-p', str(directory))) for directory in directories]
    connection, guest, started, paused = None, None, False, False
    try:
        for directory in directories:
            run('setfacl', '-m', 'u:libvirt-qemu:' + ('rwx' if directory == target else '--x'), str(directory))
        connection = libvirt.open('qemu:///system')
        guest = connection.lookupByName(vm_name)
        # Consume stale completion statistics before starting our own job.
        guest.jobStats(libvirt.VIR_DOMAIN_JOB_STATS_COMPLETED)
        nvram = root.find('./os/nvram')
        try:
            if nvram is not None:
                if not nvram.text or not Path(nvram.text).is_file():
                    raise HypervisorError('Unsupported or missing NVRAM file')
                guest.suspend()
                paused = True
                shutil.copyfile(nvram.text, target / 'nvram.fd')
            guest.backupBegin(ET.tostring(xml, encoding='unicode'), None, 0)
            started = True
        finally:
            if paused and guest.isActive() and guest.state()[0] == libvirt.VIR_DOMAIN_PAUSED:
                guest.resume()
        deadline = time.monotonic() + 86400
        while True:
            stats = guest.jobStats(0)
            if stats.get('type', 0) == libvirt.VIR_DOMAIN_JOB_NONE:
                result = guest.jobStats(libvirt.VIR_DOMAIN_JOB_STATS_COMPLETED)
                if result.get('type') != libvirt.VIR_DOMAIN_JOB_COMPLETED:
                    raise HypervisorError('Native live backup did not complete successfully: ' + str(result))
                started = False
                break
            if time.monotonic() >= deadline:
                raise HypervisorError('Native live backup timed out')
            time.sleep(.2)
        return disks
    finally:
        try:
            if started:
                guest.abortJob()
        finally:
            if connection:
                connection.close()
            for directory, acl in reversed(original_acls):
                run('setfacl', '--restore=-', stdin=acl + '\n')


def export(block_path, backup_id, vm_name, live=False):
    domain = Libvirt()
    name(vm_name)
    if not live:
        domain.stopped(vm_name)
    root = domain.xml(vm_name)
    validate(root)
    if live:
        active = ET.fromstring(domain.cli('dumpxml', vm_name))
        if ET.tostring(active.find('devices')) != ET.tostring(root.find('devices')):
            # Runtime XML normally adds aliases/addresses; compare only disk
            # sources and targets to catch external uncommitted disk changes.
            def layout(xml):
                return [(d.find('target').get('dev'), d.find('source').get('file') if d.find('source') is not None else None)
                        for d in xml.findall('./devices/disk')]
            if layout(active) != layout(root):
                raise HypervisorError('Active and configured disks differ; shut down and restart before backup')
    base = Path(block_path) / 'vm-backups' / backup_id
    if base.resolve() != base.absolute() or not base.is_dir():
        raise HypervisorError('Invalid backup directory')
    target = base / 'libvirt'
    target.mkdir(mode=0o700)  # Exclusive creation; no client-supplied paths.
    account = pwd.getpwnam('vm-manager')
    os.chown(target, account.pw_uid, account.pw_gid)
    try:
        disks = live_export(domain, vm_name, root, target) if live else []
        for index, disk in enumerate(root.findall('./devices/disk')):
            if disk.get('device') == 'cdrom':
                continue
            if live and disk.find('readonly') is None:
                continue  # Writable disks were copied by the native backup job.
            filename = f'disk-{index}.qcow2'
            source = Path(disk.find('source').get('file'))
            if not source.is_file():
                raise HypervisorError('Disk file is unavailable: ' + str(source))
            # Flatten the entire backing chain into a self-contained disk.
            run('qemu-img', 'convert', '-f', disk.find('driver').get('type'), '-O', 'qcow2',
                str(source), str(target / filename), timeout=86400)
            disks.append(dict(index=index, file=filename))
        nvram = root.find('./os/nvram')
        if nvram is not None and not live:
            if not nvram.text or not Path(nvram.text).is_file():
                raise HypervisorError('Unsupported or missing NVRAM file')
            shutil.copyfile(nvram.text, target / 'nvram.fd')
        (target / 'domain.xml').write_text(ET.tostring(root, encoding='unicode'))
        if not live:
            domain.stopped(vm_name)
        return dict(disks=disks, nvram=nvram is not None)
    finally:
        for file in target.iterdir():
            file.chmod(0o600)
            os.chown(file, account.pw_uid, account.pw_gid)
        os.chown(target, account.pw_uid, account.pw_gid)


def restore(data, domain, path, manifest, vm_name):
    name(vm_name)
    if vm_name in domain.cli('list', '--all', '--name').splitlines():
        raise HypervisorError('A VM with this name already exists')
    root = ET.fromstring((path / 'domain.xml').read_text())
    validate(root)
    root.find('name').text = vm_name
    for tag in ('uuid', 'genid', 'metadata'):
        node = root.find(tag)
        if node is not None:
            root.remove(node)
    target = data / 'disks' / ('restore-' + uuid.uuid4().hex)
    target.mkdir(mode=0o755, parents=True)
    disks = root.findall('./devices/disk')
    for item in manifest['disks']:
        filename = item['file']
        if Path(filename).name != filename or not filename.endswith('.qcow2'):
            raise HypervisorError('Invalid disk archive')
        disk = disks[item['index']]
        output = target / filename
        run('qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', str(path / filename), str(output), timeout=86400)
        output.chmod(0o644)
        source = disk.find('source')
        source.attrib.clear()
        source.set('file', str(output))
        disk.find('driver').set('type', 'qcow2')
        for tag in ('backingStore', 'mirror', 'auth'):
            node = disk.find(tag)
            if node is not None:
                disk.remove(node)
    for disk in disks:
        if disk.get('device') == 'cdrom':
            source = disk.find('source')
            if source is not None:
                disk.remove(source)
    if manifest['nvram']:
        output = target / 'nvram.fd'
        shutil.copyfile(path / 'nvram.fd', output)
        output.chmod(0o644)
        root.find('./os/nvram').text = str(output)
    for nic in root.findall('./devices/interface'):
        for tag in ('mac', 'target'):
            node = nic.find(tag)
            if node is not None:
                nic.remove(node)
    for graphics in root.findall('./devices/graphics'):
        if graphics.get('type') in ('vnc', 'spice'):
            graphics.set('autoport', 'yes')
            graphics.set('port', '-1')
            graphics.set('listen', '127.0.0.1')
            for listen in graphics.findall('listen'):
                listen.set('address', '127.0.0.1')
    domain.define(root)
