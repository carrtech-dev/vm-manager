"""Offline libvirt disk export and restore. Privilege is used only for export.

The storage broker derives export destinations from its root-owned registry and
reads disk paths only from the hypervisor's existing inactive domain definition.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
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


def export(block_path, backup_id, vm_name):
    domain = Libvirt()
    domain.stopped(name(vm_name))
    root = domain.xml(vm_name)
    validate(root)
    base = Path(block_path) / 'vm-backups' / backup_id
    if base.resolve() != base.absolute() or not base.is_dir():
        raise HypervisorError('Invalid backup directory')
    target = base / 'libvirt'
    target.mkdir(mode=0o700)  # Exclusive creation; no client-supplied paths.
    account = pwd.getpwnam('vm-manager')
    os.chown(target, account.pw_uid, account.pw_gid)
    try:
        disks = []
        for index, disk in enumerate(root.findall('./devices/disk')):
            if disk.get('device') == 'cdrom':
                continue
            filename = f'disk-{index}.qcow2'
            source = Path(disk.find('source').get('file'))
            if not source.is_file():
                raise HypervisorError('Disk file is unavailable: ' + str(source))
            # Flatten the entire backing chain into a self-contained disk.
            run('qemu-img', 'convert', '-f', disk.find('driver').get('type'), '-O', 'qcow2',
                str(source), str(target / filename), timeout=86400)
            disks.append(dict(index=index, file=filename))
        nvram = root.find('./os/nvram')
        if nvram is not None:
            if not nvram.text or not Path(nvram.text).is_file():
                raise HypervisorError('Unsupported or missing NVRAM file')
            shutil.copyfile(nvram.text, target / 'nvram.fd')
        (target / 'domain.xml').write_text(ET.tostring(root, encoding='unicode'))
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
