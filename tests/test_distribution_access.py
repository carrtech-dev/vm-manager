"""Firewall ownership and address discovery for first-install web access."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def access(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'packaging/vm_manager_access.py'
    spec = importlib.util.spec_from_file_location('package_access', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'CONFIG', tmp_path)
    monkeypatch.setattr(module, 'RECORD', tmp_path / 'network-access.json')
    return module


@pytest.fixture
def firewalld(access, monkeypatch):
    opened = {('public', True)}  # Existing administrator rule must survive uninstall.
    calls = []
    def command(*args, check=True):
        calls.append(args)
        code, text = 0, ''
        if '--get-active-zones' in args:
            text = 'public\n  interfaces: eth0\ninternal\n  interfaces: eth1\ndrop\n  interfaces: eth2\n'
        elif any(a.startswith('--zone=') for a in args):
            zone = next(a.split('=', 1)[1] for a in args if a.startswith('--zone='))
            key = (zone, '--permanent' in args)
            if '--query-port=8443/tcp' in args:
                code = 0 if key in opened else 1
            elif '--add-port=8443/tcp' in args:
                opened.add(key)
            elif '--remove-port=8443/tcp' in args:
                opened.discard(key)
        return SimpleNamespace(returncode=code, stdout=text, stderr='')
    monkeypatch.setattr(access, 'command', command)
    monkeypatch.setattr(access.shutil, 'which', lambda name: '/bin/' + name if name == 'firewall-cmd' else None)
    return opened, calls


def test_firewall_runtime_permanent_idempotence_and_preserve_admin_rules(access, firewalld):
    opened, calls = firewalld
    values = {'VM_MANAGER_HOST': '0.0.0.0'}
    access.configure_firewall(values)
    assert opened == {('public', True), ('public', False), ('internal', True), ('internal', False)}
    assert len(access.records()) == 3
    assert access.RECORD.stat().st_mode & 0o777 == 0o600
    count = len([call for call in calls if '--add-port=8443/tcp' in call])
    access.configure_firewall(values)
    assert len([call for call in calls if '--add-port=8443/tcp' in call]) == count
    assert not any('--reload' in call for call in calls)
    access.remove_firewall()
    assert opened == {('public', True)}
    assert not access.RECORD.exists()


def test_explicit_zones_and_opt_out_remove_only_owned_rules(access, firewalld):
    opened, calls = firewalld
    values = {'VM_MANAGER_HOST': '0.0.0.0', 'VM_MANAGER_FIREWALL_ZONES': 'internal'}
    access.configure_firewall(values)
    assert ('public', False) not in opened
    values['VM_MANAGER_MANAGE_FIREWALL'] = 'no'
    access.configure_firewall(values)
    assert opened == {('public', True)}


def test_ufw_existing_rule_is_not_owned_or_removed(access, monkeypatch):
    calls = []
    def command(*args, check=True):
        calls.append(args)
        text = "Status: active\n" if args == ('ufw', 'status') else "ufw allow 'VMManager'\n"
        return SimpleNamespace(returncode=0, stdout=text, stderr='')
    monkeypatch.setattr(access, 'command', command)
    monkeypatch.setattr(access.shutil, 'which', lambda name: name == 'ufw')
    access.configure_firewall({'VM_MANAGER_HOST': '0.0.0.0'})
    access.remove_firewall()
    assert not access.RECORD.exists()
    assert not any('delete' in call or call == ('ufw', 'allow', 'VMManager') for call in calls)


def test_discover_actual_lan_addresses_ipv6_urls_and_certificate_names(access, monkeypatch):
    interfaces = [
        {'ifname': 'docker0', 'addr_info': [{'local': '172.17.0.1'}]},
        {'ifname': 'eth1', 'addr_info': [{'local': '10.2.0.4'}]},
        {'ifname': 'eth0', 'addr_info': [{'local': '192.168.8.10'}, {'local': 'fd00::10'}, {'local': 'fe80::1'}]},
    ]
    monkeypatch.setattr(access.shutil, 'which', lambda name: name == 'ip')
    monkeypatch.setattr(access, 'command', lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps([{'dev': 'eth0'}] if 'route' in args else interfaces)))
    assert access.urls({'VM_MANAGER_HOST': '0.0.0.0'}) == ['https://192.168.8.10:8443', 'https://10.2.0.4:8443']
    assert access.urls({'VM_MANAGER_HOST': '::'})[1] == 'https://[fd00::10]:8443'
    assert 'IP:192.168.8.10' in access.certificate_names()
    assert 'IP:fd00::10' in access.certificate_names()
    assert '172.17.0.1' not in access.certificate_names()


def test_config_is_parsed_without_executing_shell(access):
    (access.CONFIG / 'vm-manager.conf').write_text('VM_MANAGER_HOST=0.0.0.0\nVM_MANAGER_FIREWALL_ZONES="public internal"\nVM_MANAGER_MANAGE_FIREWALL=no\n')
    assert access.settings()['VM_MANAGER_FIREWALL_ZONES'] == 'public internal'
    assert access.settings()['VM_MANAGER_MANAGE_FIREWALL'] == 'no'


def test_existing_scoped_firewalld_policy_is_not_broadened(access, firewalld, monkeypatch):
    opened, calls = firewalld
    original = access.command
    def command(*args, **kwargs):
        if '--zone=public' in args and '--list-rich-rules' in args:
            return SimpleNamespace(returncode=0, stdout='rule family="ipv4" source address="192.168.8.0/24" port port="8443" protocol="tcp" accept', stderr='')
        return original(*args, **kwargs)
    monkeypatch.setattr(access, 'command', command)
    access.configure_firewall({'VM_MANAGER_HOST': '0.0.0.0'})
    assert ('public', False) not in opened
    assert not any(r.get('zone') == 'public' for r in access.records())


def test_new_ufw_rule_is_recorded_and_removed(access, monkeypatch):
    calls = []
    def command(*args, check=True):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='Status: active\n' if args == ('ufw', 'status') else '', stderr='')
    monkeypatch.setattr(access, 'command', command)
    monkeypatch.setattr(access.shutil, 'which', lambda name: name == 'ufw')
    access.configure_firewall({'VM_MANAGER_HOST': '0.0.0.0'})
    assert access.records() == [{'manager': 'ufw'}]
    access.remove_firewall()
    assert ('ufw', 'allow', 'VMManager') in calls
    assert ('ufw', '--force', 'delete', 'allow', 'VMManager') in calls


def test_published_url_is_shown_first_and_invalid_url_rejected(access, monkeypatch):
    monkeypatch.setattr(access, 'addresses', lambda: ['10.100.0.184'])
    values = {'VM_MANAGER_HOST': '0.0.0.0', 'VM_MANAGER_PUBLIC_URL': 'https://192.168.8.235:9443/'}
    assert access.urls(values) == ['https://192.168.8.235:9443', 'https://10.100.0.184:8443']
    values['VM_MANAGER_PUBLIC_URL'] = 'http://example.com'
    with pytest.raises(ValueError):
        access.urls(values)
