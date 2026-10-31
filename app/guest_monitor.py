"""Read-only Linux filesystem probe, executed inside Incus VMs with a guest agent."""

import json
import os
import signal
import subprocess


def collect(tree, stat=os.statvfs):
    owners, nodes = {}, {}

    def visit(node, serial):
        serial = node.get('serial') or serial
        key = node['maj:min']
        owners.setdefault(key, set()).add(serial)
        nodes[key] = node
        for child in node.get('children', []):
            visit(child, serial)

    for node in tree['blockdevices']:
        if node.get('type') == 'disk':
            visit(node, node.get('serial'))
    filesystem_owners = {}
    for key, node in nodes.items():
        if node.get('uuid'):
            filesystem_owners.setdefault(node['uuid'], set()).update(owners[key])
    totals, seen = {}, set()
    for key, node in nodes.items():
        if len(owners[key]) != 1:
            continue  # A spanning filesystem cannot be attributed to one disk.
        serial = next(iter(owners[key]))
        if not serial:
            continue
        filesystem = node.get('uuid') or key
        if filesystem in seen or len(filesystem_owners.get(filesystem, {serial})) != 1:
            continue
        for mount in node.get('mountpoints') or [node.get('mountpoint')]:
            if not mount or not mount.startswith('/'):
                continue
            try:
                usage = stat(mount)
                seen.add(filesystem)
                value = totals.setdefault(serial, [0, 0])
                value[0] += (usage.f_blocks - usage.f_bfree) * usage.f_frsize
                value[1] += usage.f_blocks * usage.f_frsize
                break  # Bind mounts and Btrfs subvolumes share filesystem space.
            except OSError:
                pass
    return totals


if __name__ == '__main__':
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(4)
    tree = json.loads(subprocess.check_output(
        ['lsblk', '-b', '-J', '-o', 'NAME,TYPE,SERIAL,MOUNTPOINTS,MAJ:MIN,UUID'], timeout=2))
    print(json.dumps(collect(tree)))
