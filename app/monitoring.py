"""Persistent bounded time series. Collection does not depend on open browsers."""

import calendar
from datetime import datetime, timezone
import math
import json
from pathlib import Path
import subprocess
import time


def configured_disks(vm):
    return {key: device for key, device in vm['devices'].items()
            if device.get('type') == 'disk' and device.get('kind', 'disk') == 'disk'
            and key != 'cdrom'
            and device.get('source') != 'agent:config'
            and not device.get('source', '').lower().endswith('.iso')
            and (device.get('pool'), device.get('source')) not in vm.get('iso_sources', set())}


def vm_disks(vm, libvirt):
    devices = configured_disks(vm)
    values = {}
    source = 'Guest filesystems · mounted space used'
    unavailable = 'VM stopped' if vm['status'] != 'running' else 'Guest disk usage unavailable · agent and mounted filesystem required'
    if devices and vm['status'] == 'running':
        try:
            if vm['provider'] == 'incus':
                script = Path(__file__).with_name('guest_monitor.py').read_text()
                result = subprocess.run([
                    'incus', '--force-local', '--project', 'default', 'exec', vm['name'],
                    '--mode=non-interactive', '--', '/usr/bin/python3', '-c', script,
                ], capture_output=True, text=True, timeout=5, check=True)
                raw = json.loads(result.stdout)
                values = {key: raw['incus_' + key] for key in devices if 'incus_' + key in raw}
            else:
                # domfsinfo resolves guest agent addresses to hypervisor disk targets.
                # Ask QGA for sizes and match by serial; never assume guest /dev names
                # have the same ordering as host disk targets.
                root = libvirt.xml(vm['name'])
                serials = {disk.findtext('serial'): disk.find('target').get('dev')
                           for disk in root.findall('./devices/disk')
                           if disk.findtext('serial') and disk.find('target') is not None}
                command = json.dumps({'execute': 'guest-get-fsinfo'})
                filesystems = json.loads(libvirt.cli('qemu-agent-command', vm['name'],
                                                     command, '--timeout', '2'))['return']
                # virsh's native fsinfo reports host target names for each mount.
                mapping = {}
                lines = libvirt.cli('domfsinfo', vm['name']).splitlines()
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 4 and not line.lstrip().startswith('-'):
                        mapping[parts[0]] = parts[-1].split(',')
                seen = set()
                for fs in filesystems:
                    mount = fs.get('mountpoint')
                    filesystem = fs.get('name', mount)
                    targets = set(mapping.get(mount, [])) & devices.keys()
                    if not targets:
                        targets = {serials[d['serial']] for d in fs.get('disk', [])
                                   if d.get('serial') in serials} & devices.keys()
                    if (len(targets) != 1 or filesystem in seen or len(fs.get('disk', [])) != 1
                            or fs.get('used-bytes') is None or not fs.get('total-bytes')):
                        continue
                    seen.add(filesystem)
                    target = next(iter(targets))
                    value = values.setdefault(target, [0, 0])
                    value[0] += fs['used-bytes']
                    value[1] += fs.get('total-bytes-privileged', fs['total-bytes'])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass
        except Exception:
            # Hypervisor errors are isolated to disk metrics, including missing agents.
            pass
    return [dict(metric='disk', series=key, label='Disk ' + key,
                 value=values.get(key, [None, None])[0], total=values.get(key, [None, None])[1],
                 source=source if key in values else unavailable) for key in devices]

RANGES = {
    '5m': (300, 10), '1h': (3600, 10), '12h': (43200, 60),
    '1d': (86400, 120), '1w': (604800, 900),
    '1mo': (31 * 86400, 3600), '6mo': (184 * 86400, 21600),
    '12mo': (366 * 86400, 43200),
}


def months_ago(now, months):
    date = datetime.fromtimestamp(now, timezone.utc)
    year, month = divmod(date.year * 12 + date.month - 1 - months, 12)
    return date.replace(year=year, month=month + 1,
                        day=min(date.day, calendar.monthrange(year, month + 1)[1])).timestamp()


class History:
    def __init__(self, store):
        self.store = store
        self.pruned = 0
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS monitoring(
                    resource TEXT NOT NULL, metric TEXT NOT NULL, series TEXT NOT NULL,
                    resolution INTEGER NOT NULL, at INTEGER NOT NULL,
                    label TEXT NOT NULL, source TEXT NOT NULL,
                    sum REAL NOT NULL, count INTEGER NOT NULL,
                    low REAL, high REAL, total_sum REAL NOT NULL, total_count INTEGER NOT NULL,
                    PRIMARY KEY(resource,metric,series,resolution,at));
                CREATE INDEX IF NOT EXISTS monitoring_expiry ON monitoring(resolution,at);
                CREATE INDEX IF NOT EXISTS monitoring_query ON monitoring(resource,resolution,at);
                CREATE TABLE IF NOT EXISTS monitoring_latest(
                    resource TEXT NOT NULL, metric TEXT NOT NULL, series TEXT NOT NULL,
                    at REAL NOT NULL, label TEXT NOT NULL, source TEXT NOT NULL,
                    value REAL, total REAL, PRIMARY KEY(resource,metric,series));
            ''')

    def write(self, resource, samples, now=None):
        now = time.time() if now is None else now
        with self.store.db() as db:
            for sample in samples:
                value, total = sample.get('value'), sample.get('total')
                valid = value is not None and math.isfinite(value) and value >= 0
                valid_total = total is not None and math.isfinite(total) and total > 0
                db.execute('''INSERT INTO monitoring_latest VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(resource,metric,series) DO UPDATE SET
                        at=excluded.at,label=excluded.label,source=excluded.source,
                        value=excluded.value,total=excluded.total
                    WHERE excluded.at>=monitoring_latest.at''',
                    (resource, sample['metric'], sample.get('series', ''), now,
                     sample['label'], sample['source'], value if valid else None,
                     total if valid_total else None))
                for resolution in (10, 300, 3600):
                    db.execute('''INSERT INTO monitoring VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(resource,metric,series,resolution,at) DO UPDATE SET
                            sum=monitoring.sum+excluded.sum, count=monitoring.count+excluded.count,
                            low=CASE WHEN monitoring.low IS NULL THEN excluded.low
                                WHEN excluded.low IS NULL THEN monitoring.low ELSE min(monitoring.low,excluded.low) END,
                            high=CASE WHEN monitoring.high IS NULL THEN excluded.high
                                WHEN excluded.high IS NULL THEN monitoring.high ELSE max(monitoring.high,excluded.high) END,
                            total_sum=monitoring.total_sum+excluded.total_sum,
                            total_count=monitoring.total_count+excluded.total_count,
                            label=excluded.label,
                            source=CASE WHEN monitoring.source=excluded.source THEN excluded.source
                                ELSE 'Mixed measurement sources' END''',
                        (resource, sample['metric'], sample.get('series', ''), resolution,
                         int(now // resolution) * resolution, sample['label'], sample['source'],
                         value if valid else 0, int(valid), value if valid else None,
                         value if valid else None, total if valid_total else 0, int(valid_total)))
        # Reuse freed pages; storage remains bounded rather than growing every year.
        if now - self.pruned >= 3600:
            with self.store.db() as db:
                for resolution, cutoff in ((10, now - 2 * 86400), (300, now - 32 * 86400),
                                           (3600, months_ago(now, 12))):
                    db.execute('DELETE FROM monitoring WHERE resolution=? AND at<?', (resolution, cutoff))
                db.execute('DELETE FROM monitoring_latest WHERE at<?', (months_ago(now, 12),))
            self.pruned = now

    def read(self, resource, window, now=None):
        now = time.time() if now is None else now
        seconds, step = RANGES[window]
        start = months_ago(now, int(window[:-2])) if window.endswith('mo') else now - seconds
        resolution = 10 if seconds <= 86400 else 300 if window == '1w' else 3600
        first = int(start // step) * step
        with self.store.db() as db:
            rows = db.execute('''SELECT metric,series,max(label) label,
                CASE WHEN count(DISTINCT source)=1 THEN max(source) ELSE 'Mixed measurement sources' END source,
                cast(at / ? as integer) * ? bucket,
                sum(sum)/nullif(sum(count),0) value, min(low) low, max(high) high,
                sum(total_sum)/nullif(sum(total_count),0) total
                FROM monitoring WHERE resource=? AND resolution=? AND at>=? AND at<=?
                GROUP BY metric,series,bucket ORDER BY metric,series,bucket''',
                (step, step, resource, resolution, first, now)).fetchall()
            latest = db.execute('SELECT * FROM monitoring_latest WHERE resource=?', (resource,)).fetchall()
        series = {}
        for row in rows:
            key = (row['metric'], row['series'])
            item = series.setdefault(key, dict(metric=key[0], id=key[1], label=row['label'],
                                               source=row['source'], points=[]))
            if item['source'] != row['source']:
                item['source'] = 'Mixed measurement sources'
            item['points'].append([row['bucket'], row['value'], row['total'], row['low'], row['high']])
        for row in latest:
            key = (row['metric'], row['series'])
            if key in series:
                series[key]['latest'] = dict(at=row['at'], value=row['value'], total=row['total'], source=row['source'])
        return dict(resource=resource, range=window, start=start, end=now, step=step,
                    series=list(series.values()), interval=10)
