import copy
import subprocess
from types import SimpleNamespace

import pytest

from app import guest_agent
from app.hypervisor import HypervisorError


@pytest.fixture
def incus():
    vm = dict(type='virtual-machine', status='Running', devices={
        'root': dict(type='disk', path='/', pool='pool'),
        'agent': dict(type='nic', network='lan'),
        'cdrom': dict(type='disk', source='/isos/installer.iso', readonly='true'),
    })
    return SimpleNamespace(vm=lambda _: copy.deepcopy(vm),
                           get=lambda _: dict(api_extensions=['agent_config_drive']),
                           update=lambda _, fn: fn(vm), state=vm)


def test_prepare_preserves_devices_and_is_repeatable(incus):
    original = copy.deepcopy(incus.state['devices'])
    guest_agent.prepare(incus, 'demo')
    assert incus.state['devices'] == original | {
        'agent1': dict(type='disk', source='agent:config', readonly='true', **{'boot.priority': '0'})}
    guest_agent.prepare(incus, 'demo')
    assert len(incus.state['devices']) == 4


def test_prepare_reuses_inherited_media_and_avoids_profile_names(incus):
    incus.state['expanded_devices'] = incus.state['devices'] | {'agent1': dict(type='nic')}
    guest_agent.prepare(incus, 'demo')
    assert incus.state['devices']['agent2']['source'] == 'agent:config'
    incus.state['devices'].pop('agent2')
    incus.state['expanded_devices']['tools'] = dict(type='disk', source='agent:config')
    guest_agent.prepare(incus, 'demo')
    assert len(incus.state['devices']) == 3


@pytest.mark.parametrize('change', [dict(type='container'), dict(status='Frozen')])
def test_prepare_rejects_unsupported_instances_before_mutation(incus, change):
    incus.state.update(change)
    original = copy.deepcopy(incus.state)
    with pytest.raises(HypervisorError):
        guest_agent.prepare(incus, 'demo')
    assert incus.state == original


def test_prepare_requires_host_support(incus):
    incus.get = lambda _: {}
    with pytest.raises(HypervisorError, match='does not support'):
        guest_agent.prepare(incus, 'demo')


@pytest.mark.parametrize('code,output,connected,ready', [
    (0, 'vmm-agent-connected\n', True, True),
    (1, 'vmm-agent-connected\n', True, False),
    (1, '', False, False),
])
def test_probe_distinguishes_connection_from_mount_requirements(incus, monkeypatch, code, output, connected, ready):
    def run(args, **kwargs):
        assert args[:7] == ['incus', '--force-local', '--project', 'default', 'exec', 'demo', '--']
        assert kwargs['timeout'] == 10
        return SimpleNamespace(returncode=code, stdout=output, stderr='private host error')
    monkeypatch.setattr(guest_agent.subprocess, 'run', run)
    result = guest_agent.status(incus, 'demo')
    assert result['connected'] == connected and result['mount_ready'] == ready
    assert 'private' not in result['message']


def test_probe_does_not_execute_for_stopped_vm_and_bounds_unavailable_agent(incus, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr(guest_agent.subprocess, 'run', timeout)
    assert 'timed out' in guest_agent.status(incus, 'demo')['message']
    incus.state['status'] = 'Stopped'
    assert 'Start' in guest_agent.status(incus, 'demo')['message']
