"""Bootstrap media and a bounded, read-only probe for Incus guest integration."""

import subprocess

from .hypervisor import HypervisorError, name


def instance(incus, vm_name):
    name(vm_name)
    vm = incus.vm(vm_name)
    if vm.get('type') != 'virtual-machine':
        raise HypervisorError('Incus agent installation is available for virtual machines only')
    return vm


def prepare(incus, vm_name):
    vm = instance(incus, vm_name)
    if vm.get('status', '').lower() not in ('running', 'stopped'):
        raise HypervisorError('Wait until the VM is running or stopped before preparing its agent installer')
    if 'agent_config_drive' not in incus.get('/1.0').get('api_extensions', []):
        raise HypervisorError('This Incus host does not support the agent installer CD')

    def attach(current):
        devices = current.get('expanded_devices') or current.get('devices') or {}
        if any(d.get('type') == 'disk' and d.get('source') == 'agent:config' for d in devices.values()):
            return
        # Never replace an existing device, including one inherited from a profile.
        used = set(devices) | set(current.get('devices') or {})
        key = next(('agent' + str(i or '') for i in range(100)
                    if 'agent' + str(i or '') not in used), None)
        if key is None:
            raise HypervisorError('No free device name for the Incus agent installer')
        current.setdefault('devices', {})[key] = {
            'type': 'disk', 'source': 'agent:config', 'readonly': 'true',
            'boot.priority': '0',
        }

    incus.update(vm_name, attach)
    return 'Optional agent installer CD ready. Run the command in the VM console, then use Check agent. Keep the CD attached only if the guest relies on it at boot; it does not need to stay mounted inside Linux.'


def status(incus, vm_name):
    vm = instance(incus, vm_name)
    devices = vm.get('expanded_devices') or vm.get('devices') or {}
    result = dict(installer_attached=any(d.get('type') == 'disk' and d.get('source') == 'agent:config'
                                        for d in devices.values()),
                  running=vm.get('status', '').lower() == 'running',
                  connected=False, mount_ready=False)
    if not result['running']:
        return result | dict(message='Start the VM to install or check its agent.')
    try:
        probe = subprocess.run([
            'incus', '--force-local', '--project', 'default', 'exec', vm_name, '--',
            '/bin/sh', '-c',
            'printf "vmm-agent-connected\\n"; test -x /usr/bin/python3 && test -d /run/systemd/system',
        ], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return result | dict(message='Agent check timed out or could not run. Wait for the VM to finish booting and try again.')
    if probe.stdout.strip() != 'vmm-agent-connected':
        return result | dict(message='Agent is not responding. Complete installation in the console, check the guest clock, then try again.')
    result['connected'] = True
    result['mount_ready'] = probe.returncode == 0
    return result | dict(message=('Incus agent connected. Python 3 and systemd are available for automatic storage mounting.'
                                 if result['mount_ready'] else
                                 'Incus agent connected. Automatic storage mounting also needs /usr/bin/python3 and a running systemd in a Linux guest.'))
