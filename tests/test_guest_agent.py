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
        'nic0': dict(type='nic', network='lan'),
        'cdrom': dict(type='disk', source='/isos/installer.iso', readonly='true'),
    })
    return SimpleNamespace(vm=lambda _: copy.deepcopy(vm),
                           get=lambda _: dict(api_extensions=['agent_config_drive']),
                           update=lambda _, fn: fn(vm), state=vm)


def test_prepare_preserves_devices_and_is_repeatable(incus):
    original = copy.deepcopy(incus.state['devices'])
    guest_agent.prepare(incus, 'demo')
    assert incus.state['devices'] == original | {
        'agent': dict(type='disk', source='agent:config', readonly='true', **{'boot.priority': '0'})}
    guest_agent.prepare(incus, 'demo')
    assert len(incus.state['devices']) == 4


def test_prepare_repairs_local_media_without_replacing_other_devices(incus):
    incus.state['devices']['incus-agent-installer'] = dict(type='disk', source='agent:config')
    incus.state['devices']['agent1'] = dict(type='disk', source='agent:config')
    original = copy.deepcopy(incus.state['devices'])
    guest_agent.prepare(incus, 'demo')
    assert incus.state['devices']['agent']['source'] == 'agent:config'
    assert 'incus-agent-installer' not in incus.state['devices']
    assert 'agent1' not in incus.state['devices']
    for key in ('root', 'nic0', 'cdrom'):
        assert incus.state['devices'][key] == original[key]


def test_prepare_reuses_canonical_inherited_media_and_preserves_other_profile_media(incus):
    incus.state['expanded_devices'] = incus.state['devices'] | {'tools': dict(type='disk', source='agent:config')}
    inherited = copy.deepcopy(incus.state['expanded_devices'])
    guest_agent.prepare(incus, 'demo')
    assert incus.state['devices']['agent']['source'] == 'agent:config'
    assert incus.state['expanded_devices'] == inherited
    incus.state['devices'].pop('agent')
    incus.state['expanded_devices']['agent'] = dict(type='disk', source='agent:config')
    guest_agent.prepare(incus, 'demo')
    assert len(incus.state['devices']) == 3


@pytest.mark.parametrize('inherited', [False, True])
def test_prepare_rejects_canonical_name_conflict_without_mutation(incus, inherited):
    key = 'expanded_devices' if inherited else 'devices'
    incus.state.setdefault(key, {})['agent'] = dict(type='nic', network='lan')
    incus.state['devices']['legacy'] = dict(type='disk', source='agent:config')
    original = copy.deepcopy(incus.state)
    with pytest.raises(HypervisorError, match="name 'agent' is used"):
        guest_agent.prepare(incus, 'demo')
    assert incus.state == original


def test_probe_reports_legacy_cd_repair_instead_of_repeating_install_instructions(incus, monkeypatch):
    incus.state['devices']['incus-agent-installer'] = dict(type='disk', source='agent:config')
    monkeypatch.setattr(guest_agent.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
    result = guest_agent.status(incus, 'demo')
    assert result['installer_attached'] and result['installer_needs_repair']
    assert 'incompatible device ID' in result['message']
    guest_agent.prepare(incus, 'demo')
    assert not guest_agent.status(incus, 'demo')['installer_needs_repair']


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
