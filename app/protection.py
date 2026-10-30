"""Power policy shared by user-requested snapshot and backup operations."""

from contextlib import contextmanager

from .hypervisor import HypervisorError
from .power import PowerInterrupted


class ProtectionPower:
    def __init__(self, incus, libvirt, requests, power_action):
        self.incus, self.libvirt = incus, libvirt
        self.requests, self.power_action = requests, power_action

    def status(self, provider, vm_name):
        return (self.incus.vm(vm_name)['status'].lower() if provider == 'incus'
                else self.libvirt.cli('domstate', vm_name).strip())

    @contextmanager
    def operation(self, vm_id, mode, cancelled=None):
        provider, vm_name = vm_id.split(':', 1)
        status = self.status(provider, vm_name)
        if mode not in ('live', 'shutdown', 'stopped'):
            raise HypervisorError('Choose live capture, shutdown and restart, or require stopped VM')
        if status not in ('running', 'stopped', 'shut off'):
            raise HypervisorError('Wait until the VM is running or stopped')
        if status == 'running' and mode == 'stopped':
            raise HypervisorError('VM must be stopped for the selected capture mode')
        restart = status == 'running' and mode == 'shutdown'
        error = None
        try:
            if restart:
                self.power_action(vm_id, 'shutdown', cancelled)
            self.requests.check(cancelled)
            yield status == 'running' and mode == 'live'
            self.requests.check(cancelled)
        except Exception as exc:
            error = exc
            raise
        finally:
            if restart:
                # Do not apply pending hardware, or undo an emergency Force stop.
                try:
                    with self.requests.transition(vm_id, cancelled):
                        if self.status(provider, vm_name) in ('stopped', 'shut off'):
                            driver = self.incus if provider == 'incus' else self.libvirt
                            driver.action(vm_name, 'start')
                except PowerInterrupted:
                    raise
                except Exception as exc:
                    raise HypervisorError((str(error) + '; ' if error else '') +
                                          'Automatic restart failed: ' + str(exc)) from exc

    @contextmanager
    def pause_incus(self, vm_name, cancelled=None):
        vm_id = 'incus:' + vm_name
        paused = False
        try:
            with self.requests.transition(vm_id, cancelled):
                if self.status('incus', vm_name) != 'running':
                    raise HypervisorError('VM power changed before live capture')
                # Mark intent before the call: even a failed wait may have paused it.
                paused = True
                self.incus.request('PUT', '/1.0/instances/' + vm_name + '/state',
                                   {'action': 'freeze', 'timeout': 60})
            yield
        finally:
            if paused:
                with self.requests.transition(vm_id, cancelled):
                    # Incus 6.0 can keep reporting Running in instance metadata
                    # after QMP stop. Always send cont for our own pause, unless
                    # it was stopped or an emergency stop cancelled the request.
                    if self.status('incus', vm_name) in ('frozen', 'running'):
                        self.incus.request('PUT', '/1.0/instances/' + vm_name + '/state',
                                           {'action': 'unfreeze', 'timeout': 60})
