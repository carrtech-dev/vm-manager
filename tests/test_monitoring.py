import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.monitoring import History, RANGES, months_ago, vm_disks
from app.security import Store
from app.guest_monitor import collect


@pytest.fixture
def history(tmp_path):
    return History(Store(tmp_path))


def sample(value, **extra):
    return dict(metric='cpu', label='CPU usage', source='Host CPU', value=value, total=100, **extra)


def test_weighted_rollups_preserve_extremes_and_missing_values(history):
    now = 1800000000
    for at, value in [(now, 10), (now + 10, 50), (now + 20, None), (now + 300, 90)]:
        history.write('host', [sample(value)], at)
    data = history.read('host', '1w', now + 400)
    points = data['series'][0]['points']
    assert len(points) == 1
    assert points[0][1:] == [50, 100, 10, 90]
    # Resource histories cannot bleed into one another.
    assert history.read('incus:other', '1w', now + 400)['series'] == []


def test_retention_and_reopen(history):
    now = datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
    history.write('host', [sample(25)], now - 40 * 86400)
    history.write('host', [sample(30)], months_ago(now, 12) - 7200)
    history.write('host', [sample(75)], now)
    with history.store.db() as db:
        rows = db.execute('SELECT resolution,min(at) oldest FROM monitoring GROUP BY resolution').fetchall()
        assert all(r['oldest'] >= {10: now-2*86400, 300: now-32*86400, 3600: months_ago(now,12)}[r['resolution']] for r in rows)
    reopened = History(history.store)
    assert len(reopened.read('host', '12mo', now)['series'][0]['points']) == 2
    assert months_ago(datetime(2024, 3, 31, tzinfo=timezone.utc).timestamp(), 1) == datetime(2024, 2, 29, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize('window', RANGES)
def test_all_ranges_bounded_and_nulls_are_not_zero(history, window):
    now = 1800000000
    history.write('host', [sample(None)], now)
    result = history.read('host', window, now)
    assert result['series'][0]['points'][0][1] is None
    assert (result['end'] - result['start']) / result['step'] <= 750


def test_guest_probe_maps_nested_lvm_and_deduplicates_bind_mounts(monkeypatch):
    tree = {'blockdevices': [dict(type='disk', serial='incus_root', **{'maj:min':'8:0'}, children=[
        dict(type='part', **{'maj:min':'8:1'}, children=[dict(type='lvm', **{'maj:min':'253:0'}, mountpoints=['/', '/bind'])])]) ]}
    monkeypatch.setattr('app.guest_monitor.os.stat', lambda _: SimpleNamespace(st_dev=1))
    usage = SimpleNamespace(f_blocks=100, f_bfree=40, f_frsize=1024)
    assert collect(tree, lambda _: usage) == {'incus_root': [60*1024, 100*1024]}
    # Adding a second backing disk makes this shared filesystem ambiguous.
    tree['blockdevices'].append(dict(type='disk', serial='incus_data', **{'maj:min':'8:16'}, children=tree['blockdevices'][0]['children']))
    assert collect(tree, lambda _: usage) == {}


def test_each_configured_disk_gets_a_series_even_without_agent(monkeypatch):
    vm = dict(id='incus:demo', name='demo', provider='incus', status='running', devices={
        'root': dict(type='disk', path='/'), 'extra': dict(type='disk', source='data'),
        'readonly': dict(type='disk', source='archive', readonly='true'),
        'cdrom': dict(type='disk', source='/installer.iso'), 'agent': dict(type='disk', source='agent:config')})
    monkeypatch.setattr('app.monitoring.subprocess.run', lambda *a, **k: SimpleNamespace(stdout=json.dumps({'incus_root':[20,100]})))
    series = vm_disks(vm, None)
    assert [s['series'] for s in series] == ['root', 'extra', 'readonly']
    assert series[0]['value'] == 20
    assert series[1]['value'] is None
    assert 'agent' in series[1]['source']
    vm['status'] = 'stopped'
    assert all(s['value'] is None and s['source'] == 'VM stopped' for s in vm_disks(vm, None))


def test_libvirt_maps_host_targets_without_assuming_guest_disk_order():
    import xml.etree.ElementTree as ET
    vm = dict(name='demo', provider='libvirt', status='running', devices={'vda':dict(type='disk',kind='disk')})
    fs = dict(mountpoint='/', **{'used-bytes':20, 'total-bytes':100, 'disk':[{'dev':'/dev/sdb'}]})
    driver = SimpleNamespace(xml=lambda _: ET.fromstring('<domain><devices/></domain>'),
                             cli=lambda *a: json.dumps({'return':[fs]}) if a[0]=='qemu-agent-command' else ' / sdb2 ext4 vda')
    assert vm_disks(vm, driver)[0]['value'] == 20
    fs['disk'].append({'dev':'/dev/sdc'})
    assert vm_disks(vm, driver)[0]['value'] is None


def test_missing_hypervisors_log_once_keep_host_history_and_report_recovery(monkeypatch, caplog):
    import app.main as main
    import httpx
    def absent():
        raise httpx.ConnectError('[Errno 2] No such file or directory')
    monkeypatch.setattr(main, 'monitor_backend_errors', {})
    monkeypatch.setattr(main.monitor_incus, 'vms', absent)
    monkeypatch.setattr(main.monitor_incus, 'isos', absent)
    monkeypatch.setattr(main.monitor_incus, 'pools', absent)
    monkeypatch.setattr(main.monitor_libvirt, 'vms', lambda: [])
    written = []
    monkeypatch.setattr(main.history, 'write', lambda resource, samples: written.append((resource, samples)))
    with caplog.at_level('INFO'):
        main.monitor_tick()
        main.monitor_tick()
        warnings = [r for r in caplog.records if r.levelname == 'WARNING']
        assert len(warnings) == 2
        assert all(r.exc_info is None for r in warnings)
        assert len(written) == 2 and all(resource == 'host' for resource, _ in written)
        assert all(any(s['metric'] == 'cpu' for s in samples) for _, samples in written)
        monkeypatch.setattr(main.monitor_incus, 'vms', lambda: [])
        monkeypatch.setattr(main.monitor_incus, 'isos', lambda: [])
        main.monitor_tick()
        assert sum('connection restored' in r.message for r in caplog.records) == 2
        monkeypatch.setattr(main.monitor_incus, 'vms', absent)
        main.monitor_tick()
        assert len([r for r in caplog.records if r.levelname == 'WARNING']) == 3
