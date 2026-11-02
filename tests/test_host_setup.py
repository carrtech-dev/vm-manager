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
    monkeypatch.setattr(setup, 'command', lambda *a, **kw: '')
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


def test_start_uses_dedicated_unit_and_retains_recovery_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    monkeypatch.setattr(setup, 'worker_active', lambda: False)
    monkeypatch.setattr(setup, 'validate_host', lambda p: None)
    monkeypatch.setattr(setup, 'failed_scriptlet_packages', lambda previous: ['fontconfig'])
    calls = []
    monkeypatch.setattr(setup, 'command', lambda *args, **kw: calls.append(args))
    result = setup.start(plan().model_dump())
    assert result['status'] == 'running'
    assert calls == [('systemctl', 'start', 'vm-manager-setup.service')]
    assert setup.load()['repair_packages'] == ['fontconfig']
    assert setup.load()['plan']['confirmed'] is True
    monkeypatch.setattr(setup, 'worker_active', lambda: True)
    with pytest.raises(HypervisorError, match='already running'):
        setup.start(plan().model_dump())


def test_status_reloads_worker_result_before_marking_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    setup.save({'status': 'running'})
    def finished():
        setup.save({'status': 'completed'})
        return False
    monkeypatch.setattr(setup, 'worker_active', finished)
    assert setup.status()['status'] == 'completed'
    assert setup.load()['status'] == 'completed'


def test_worker_start_failure_remains_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    monkeypatch.setattr(setup, 'worker_active', lambda: False)
    monkeypatch.setattr(setup, 'validate_host', lambda p: None)
    def fail(*args, **kwargs):
        raise HypervisorError('Unit could not be started')
    monkeypatch.setattr(setup, 'command', fail)
    with pytest.raises(HypervisorError, match='Unit could not'):
        setup.start(plan().model_dump())
    assert setup.load()['status'] == 'failed'
    assert setup.load()['plan']['network_address'] == '10.210.0.1/24'


def test_scriptlet_recovery_is_bounded_to_failed_setup_and_safe_names(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    monkeypatch.setattr(setup, 'DNF_LOG', tmp_path / 'dnf.rpm.log')
    record = {'status': 'failed', 'started': 1793628000, 'error': 'dnf failed: Transaction failed'}  # 14:00 UTC
    setup.save(record)
    os.utime(setup.RECORD, (1793628060, 1793628060))
    setup.DNF_LOG.write_text(
        '2026-11-02T13:59:59+0000 ERROR Error in POSTIN scriptlet in rpm package unrelated-before\n'
        '2026-11-02T14:00:10+0000 ERROR Error in POSTIN scriptlet in rpm package fontconfig\n'
        '2026-11-02T14:00:20+0000 ERROR Error in PREIN scriptlet in rpm package incus\n'
        '2026-11-02T14:00:25+0000 ERROR Error in POSTIN scriptlet in rpm package --bad\n'
        '2026-11-02T14:00:30+0000 ERROR Error in POSTIN scriptlet in rpm package fontconfig;evil\n'
        '2026-11-02T14:01:01+0000 ERROR Error in POSTIN scriptlet in rpm package unrelated-after\n')
    assert setup.failed_scriptlet_packages(record) == ['fontconfig', 'incus']


def test_retry_reinstalls_failed_post_scripts_and_installs_missing_pre_scripts_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    setup.save({'status': 'running', 'repair_packages': ['fontconfig', 'incus']})
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        return 'fontconfig\nother\n' if args[0] == 'rpm' else ''
    monkeypatch.setattr(setup, 'command', command)
    setup.repair_dependencies()
    assert ('dnf', '-y', 'install', 'incus') in calls
    assert calls[-1] == ('dnf', '-y', 'reinstall', 'fontconfig')
    assert setup.load()['repair_packages'] == []
    assert setup.load()['repaired_packages'] == ['fontconfig', 'incus']


def test_failed_repair_keeps_pending_packages_for_next_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'RECORD', tmp_path / 'setup.json')
    setup.save({'status': 'running', 'repair_packages': ['fontconfig']})
    def command(*args, **kwargs):
        if args[0] == 'rpm':
            return 'fontconfig\n'
        raise HypervisorError('repository temporarily unavailable')
    monkeypatch.setattr(setup, 'command', command)
    with pytest.raises(HypervisorError, match='temporarily unavailable'):
        setup.repair_dependencies()
    assert setup.load()['repair_packages'] == ['fontconfig']
