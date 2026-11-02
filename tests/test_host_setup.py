"""First-run provisioning validates operator intent and preserves host resources."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import host_setup as setup
from app.hypervisor import HypervisorError


def plan(**kwargs):
    return setup.SetupPlan(confirmed=True, network_address='10.210.0.1/24', **kwargs)


@pytest.mark.parametrize('values', [
    {'confirmed': False}, {'network_address': ''}, {'network_address': '10.210.0.0/24'},
    {'network_address': '10.210.0.255/24'}, {'network_address': '8.8.8.1/24'},
    {'network_address': '127.0.0.1/24'}, {'network_address': '10.210.0.1/32'},
    {'bind_address': '0.0.0.0\nEVIL=yes'}, {'pool': '--bad'},
    {'public_url': 'https://admin:password@example.com'}, {'public_url': 'https://host/x'},
    {'public_url': 'https://host\nEVIL=yes'}, {'command': 'rm -rf /'},
])
def test_plan_rejects_unsafe_or_unconfirmed_values(values):
    with pytest.raises((ValidationError, HypervisorError)):
        setup.SetupPlan.model_validate({'confirmed': True, 'network_address': '10.210.0.1/24', **values})


def test_cluster_mode_needs_no_vm_subnet():
    assert setup.SetupPlan(mode='cluster', confirmed=True).network_address == ''


def test_subnet_overlap_and_nonlocal_listen_address_block_before_install(monkeypatch):
    monkeypatch.setattr(setup.psutil, 'net_if_addrs', lambda: {'eth0': [SimpleNamespace(family=2, address='10.210.0.15', netmask='255.255.255.0')]})
    with pytest.raises(HypervisorError, match='overlaps eth0'):
        setup.validate_host(plan())
    with pytest.raises(HypervisorError, match='must belong'):
        setup.validate_host(plan(bind_address='192.168.9.99'))
    monkeypatch.setattr(setup, 'interfaces', lambda: [])
    monkeypatch.setattr(setup, 'command', lambda *a: json.dumps([{'dst': '10.210.0.0/16', 'dev': 'vpn0'}]))
    with pytest.raises(HypervisorError, match='host route'):
        setup.validate_host(plan())


def test_config_preserves_unrelated_options_and_keeps_backup(tmp_path, monkeypatch):
    config = tmp_path / 'vm-manager.conf'
    previous = '# Local options\nVM_MANAGER_HOST=0.0.0.0\nVM_MANAGER_FIREWALL_ZONES="internal public"\nCUSTOM_OPTION=keep\n'
    config.write_text(previous)
    config.chmod(0o640)
    monkeypatch.setattr(setup, 'CONFIG', config)
    calls = []
    monkeypatch.setattr(setup, 'access', lambda: SimpleNamespace(settings=lambda: {}, configure_firewall=lambda values: calls.append(values)))
    setup.configure(plan(bind_address='127.0.0.1', public_url='https://manager.example.com'))
    content = config.read_text()
    assert 'VM_MANAGER_HOST=127.0.0.1' in content
    assert 'VM_MANAGER_PUBLIC_URL=https://manager.example.com' in content
    assert 'VM_MANAGER_FIREWALL_ZONES="internal public"' in content
    assert 'CUSTOM_OPTION=keep' in content
    assert config.stat().st_mode & 0o777 == 0o640
    backup = next(tmp_path.glob('*.before-setup-*'))
    assert backup.read_text() == previous
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(calls) == 1


def test_existing_resources_preserved_and_owned_retry_is_idempotent(monkeypatch):
    calls = []
    fake = SimpleNamespace(pools=lambda: [{'name': 'vm-manager', 'description': 'Existing administrator pool'}],
                           networks=lambda: [{'name': 'vmmbr0', 'type': 'bridge', 'description': 'Existing network', 'config': {}}],
                           request=lambda *a: calls.append(a))
    monkeypatch.setattr(setup, 'Incus', lambda: fake)
    monkeypatch.setattr(setup, 'progress', lambda *a: None)
    monkeypatch.setattr(setup, 'command', lambda *a: calls.append(a))
    with pytest.raises(HypervisorError, match='already exists'):
        setup.provision(plan())
    assert not calls
    setup.provision(plan(create_pool=False, create_network=False))
    assert all(call[0] == 'runuser' for call in calls)
    fake.pools = lambda: [{'name': 'vm-manager', 'description': 'VM Manager first-run setup'}]
    fake.networks = lambda: [{'name': 'vmmbr0', 'type': 'bridge', 'description': 'VM Manager first-run setup', 'config': {'ipv4.address': '10.210.0.1/24'}}]
    calls.clear()
    setup.provision(plan())
    assert all(call[0] == 'runuser' for call in calls)


def test_cluster_setup_skips_hypervisor_install_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    monkeypatch.setattr(setup, 'validate_host', lambda p: None)
    monkeypatch.setattr(setup, 'install_components', lambda p: pytest.fail('cluster mode must not install hypervisors'))
    monkeypatch.setattr(setup, 'provision', lambda p: pytest.fail('cluster mode must not create resources'))
    monkeypatch.setattr(setup, 'configure', lambda p: None)
    monkeypatch.setattr(setup, 'command', lambda *a: '')
    setup.save({'status': 'running', 'plan': {'mode': 'cluster'}})
    setup.execute(setup.SetupPlan(mode='cluster', confirmed=True))
    assert setup.status()['status'] == 'completed'
    assert not setup.status()['required']
    assert setup.RECORD.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(setup, 'access', lambda: SimpleNamespace(settings=lambda: {}))
    monkeypatch.setattr(setup, 'discover', lambda: {})
    monkeypatch.setattr(setup, 'os_info', lambda: {'ID': 'rocky', 'VERSION_ID': '10.2'})
    setup.save({'status': 'running', 'steps': []})
    assert setup.status()['status'] == 'failed'
    assert 'interrupted' in setup.status()['error']


def test_broker_rejects_nonpackage_setup_and_duplicate_completed_setup(tmp_path, monkeypatch):
    monkeypatch.delenv('VM_MANAGER_FIRST_RUN', raising=False)
    with pytest.raises(HypervisorError, match='native package'):
        setup.dispatch('setup_start', plan().model_dump())
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    setup.save({'status': 'completed'})
    with pytest.raises(HypervisorError, match='already running or completed'):
        setup.start(plan().model_dump())


def test_working_hypervisor_is_reused_without_package_or_repository_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, 'os_info', lambda: {'ID': 'rocky', 'VERSION_ID': '10.2'})
    monkeypatch.setattr(setup, 'discover', lambda: {'incus': True, 'incus_vm': True, 'libvirt': True})
    monkeypatch.setattr(setup, 'Incus', lambda: SimpleNamespace(get=lambda path: {'environment': {'driver': 'lxc | qemu'}}))
    monkeypatch.setattr(setup, 'progress', lambda message: None)
    monkeypatch.setattr(setup, 'command', lambda *args: calls.append(args))
    setup.install_components(plan())
    assert not any(args[0] in ('dnf', 'apt-get', 'rpm') for args in calls)
    assert any(args[:4] == ('systemctl', 'enable', '--now', 'incus.service') for args in calls)


def test_incompatible_hypervisor_with_existing_instances_is_preserved(monkeypatch):
    monkeypatch.setattr(setup, 'os_info', lambda: {'ID': 'rocky', 'VERSION_ID': '10.2'})
    monkeypatch.setattr(setup, 'discover', lambda: {'incus': True, 'incus_vm': False, 'libvirt': False})
    monkeypatch.setattr(setup, 'Incus', lambda: SimpleNamespace(get=lambda path: ['/1.0/instances/existing']))
    monkeypatch.setattr(setup, 'command', lambda *args: pytest.fail('existing hypervisor must not be changed'))
    with pytest.raises(HypervisorError, match='will not replace it or migrate its database'):
        setup.install_components(plan())
