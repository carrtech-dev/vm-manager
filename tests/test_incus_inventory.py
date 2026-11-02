import pytest

from app.hypervisor import Incus


@pytest.mark.parametrize('expanded', [None, {}])
def test_inventory_tolerates_transient_null_fields_during_copy(monkeypatch, expanded):
    incus = Incus()
    root = {'type': 'disk', 'path': '/', 'pool': 'pool'}
    monkeypatch.setattr(incus, 'get', lambda _: [{
        'name': 'copy-in-progress', 'type': 'virtual-machine', 'status': 'Creating',
        'expanded_config': expanded, 'config': {'limits.cpu': '2'},
        'expanded_devices': expanded, 'devices': {'root': root}, 'state': None,
    }])
    vm = incus.vms()[0]
    assert vm['cpu'] == '2' and vm['devices'] == {'root': root}
    assert vm['status'] == 'creating'


def test_incus_socket_autodetects_rpm_location_and_respects_override(monkeypatch):
    from pathlib import Path
    from app.hypervisor import Incus
    monkeypatch.delenv('INCUS_SOCKET', raising=False)
    monkeypatch.setattr(Path, 'exists', lambda self: str(self) == '/run/incus/unix.socket')
    assert Incus().socket == '/run/incus/unix.socket'
    monkeypatch.setenv('INCUS_SOCKET', '/custom/incus.sock')
    assert Incus().socket == '/custom/incus.sock'
    monkeypatch.delenv('INCUS_SOCKET')
    monkeypatch.setattr(Path, 'exists', lambda self: str(self) == '/var/lib/incus/unix.socket')
    assert Incus().socket == '/var/lib/incus/unix.socket'
