"""Trusted, short guest RPCs through the existing hypervisor agents."""
import base64
import json
import subprocess
import sys
import time

from .hypervisor import HypervisorError, Incus, name


def execute(vm_id, script, body):
    provider, _, vm_name = vm_id.partition(':')
    name(vm_name)
    if provider not in ('incus', 'libvirt'):
        raise HypervisorError('Unsupported VM provider')
    try:
        if provider == 'incus':
            vm = Incus().vm(vm_name)
            if vm.get('type') != 'virtual-machine' or vm['status'].lower() != 'running':
                raise HypervisorError('VM is stopped; start it before checking or patching Linux')
            result = subprocess.run(['incus', '--force-local', '--project', 'default', 'exec', vm_name,
                '--mode=non-interactive', '--', '/usr/bin/python3', '-c', script],
                input=json.dumps(body), capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise HypervisorError('Linux guest agent command failed; Python 3 and systemd are required')
            return json.loads(result.stdout)
        sys.path.append('/usr/lib/python3/dist-packages')
        import libvirt
        import libvirt_qemu
        connection = libvirt.open('qemu:///system')
        try:
            domain = connection.lookupByName(vm_name)
            if not domain.isActive():
                raise HypervisorError('VM is stopped; start it before checking or patching Linux')
            def call(command):
                return json.loads(libvirt_qemu.qemuAgentCommand(domain, json.dumps(command), 10, 0))['return']
            process = call({'execute': 'guest-exec', 'arguments': {'path': '/usr/bin/python3',
                'arg': ['-c', script], 'input-data': base64.b64encode(json.dumps(body).encode()).decode(),
                'capture-output': True}})
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                result = call({'execute': 'guest-exec-status', 'arguments': {'pid': process['pid']}})
                if result.get('exited'):
                    if result.get('exitcode', 1) or result.get('out-truncated'):
                        raise HypervisorError('Linux guest command failed or returned too much output')
                    return json.loads(base64.b64decode(result.get('out-data', '')).decode())
                time.sleep(.3)
            raise HypervisorError('Guest agent response timed out; reconnect to inspect the operation')
        finally:
            connection.close()
    except HypervisorError:
        raise
    except Exception as exc:
        raise HypervisorError('Guest unavailable; check power, guest agent and Python 3') from exc
