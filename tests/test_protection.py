from types import SimpleNamespace

import pytest

from app.hypervisor import HypervisorError
from app.power import PowerInterrupted, PowerRequests
from app.protection import ProtectionPower


@pytest.fixture
def power():
    state = {'status': 'Running', 'starts': 0, 'shutdowns': 0}
    def action(vm, operation):
        assert operation == 'start'
        state.update(status='Running', starts=state['starts'] + 1)
    incus = SimpleNamespace(vm=lambda _: state, action=action)
    def shutdown(*args):
        state.update(status='Stopped', shutdowns=state['shutdowns'] + 1)
    return ProtectionPower(incus, None, PowerRequests(), shutdown), state


@pytest.mark.parametrize('mode', ['live', 'shutdown', 'stopped'])
def test_stopped_vm_stays_stopped_in_every_mode(power, mode):
    manager, state = power
    state['status'] = 'Stopped'
    with manager.operation('incus:demo', mode) as live:
        assert not live
    assert state == {'status': 'Stopped', 'starts': 0, 'shutdowns': 0}


@pytest.mark.parametrize('fail', [False, True])
def test_shutdown_restart_preserves_initial_power_after_success_or_failure(power, fail):
    manager, state = power
    try:
        with manager.operation('incus:demo', 'shutdown') as live:
            assert not live and state['status'] == 'Stopped'
            if fail:
                raise OSError('Storage full')
    except OSError:
        assert fail
    assert state == {'status': 'Running', 'starts': 1, 'shutdowns': 1}


def test_live_does_not_request_shutdown_and_stopped_mode_fails_before_capture(power):
    manager, state = power
    with manager.operation('incus:demo', 'live') as live:
        assert live and state['status'] == 'Running'
    with pytest.raises(HypervisorError, match='must be stopped'):
        with manager.operation('incus:demo', 'stopped'):
            pytest.fail('Must not capture')
    assert not state['shutdowns'] and not state['starts']


def test_shutdown_timeout_does_not_capture_or_force_stop(power):
    manager, state = power
    def timeout(*args):
        raise HypervisorError('Guest shutdown timed out')
    manager.power_action = timeout
    with pytest.raises(HypervisorError, match='timed out'):
        with manager.operation('incus:demo', 'shutdown'):
            pytest.fail('Must not capture')
    assert state['status'] == 'Running' and not state['starts']


def test_force_stop_cancels_automatic_restart(power):
    manager, state = power
    cancelled = manager.requests.register('incus:demo', 'snapshot')
    with pytest.raises(PowerInterrupted):
        with manager.operation('incus:demo', 'shutdown', cancelled):
            manager.requests.register('incus:demo', 'stop')
    assert state['status'] == 'Stopped' and not state['starts']


def test_restart_failure_reports_original_capture_failure(power):
    manager, state = power
    def fail(*args):
        raise HypervisorError('Cannot start')
    manager.incus.action = fail
    with pytest.raises(HypervisorError, match='Storage full; Automatic restart failed: Cannot start'):
        with manager.operation('incus:demo', 'shutdown'):
            raise HypervisorError('Storage full')
