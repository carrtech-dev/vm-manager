"""Durable Linux operations, maintenance windows and bounded patch batches."""
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import backups, storage
from .guest_exec import execute
from .hypervisor import HypervisorError, name
from .power import PowerInterrupted
from .task_output import emit

ACTIVE = ('pending', 'dispatched', 'running', 'reconnecting')


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    vm_ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator('vm_ids')
    @classmethod
    def valid_vms(cls, values):
        for value in values:
            provider, sep, vm_name = value.partition(':')
            if not sep or provider not in ('incus', 'libvirt'):
                raise ValueError('Choose Incus or libvirt VMs')
            try:
                name(vm_name)
            except HypervisorError as exc:
                raise ValueError(str(exc)) from exc
        return list(dict.fromkeys(values))


class Request(Selection):
    reboot: Literal['never', 'if-required'] = 'never'
    concurrency: int = Field(default=1, ge=1, le=4)
    stop_on_failure: bool = True


class Schedule(Request):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    frequency: Literal['daily', 'weekly'] = 'weekly'
    time: str = Field(default='02:00', pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    timezone: str = Field(default='Europe/London', max_length=100)
    weekday: int = Field(default=6, ge=0, le=6)
    window_minutes: int = Field(default=120, ge=15, le=1440)

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        return backups.Plan.valid_zone(value)

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Enter a schedule name without control characters')
        return value


def initialize(store):
    with store.db() as db:
        # A guest transaction survives this service. Reattach using its original
        # token; never turn an interrupted launch into a fresh upgrade.
        db.execute("UPDATE patch_runs SET status='reconnecting' WHERE status IN ('dispatched','running')")


def catalog(store):
    with store.db() as db:
        return {
            'guests': {r['vm_id']: dict(json.loads(r['result']), checked=r['checked']) for r in db.execute('SELECT * FROM patch_guests')},
            'schedules': [dict(json.loads(r['settings']), id=r['id'], next_run=r['next_run'], last_message=r['last_message'])
                          for r in db.execute('SELECT * FROM patch_schedules ORDER BY id')],
            'runs': [dict(r, result={k: v for k, v in json.loads(r['result']).items() if k not in ('output', 'packages')}) for r in db.execute(
                'SELECT r.*,b.created,b.username,b.schedule_id FROM patch_runs r JOIN patch_batches b ON b.id=r.batch_id ORDER BY b.created DESC,r.rowid LIMIT 300')],
        }


def dispatch(request):
    """Broker boundary: fixed operations, validated VM IDs, trusted guest source."""
    body = request.get('body', {})
    if not isinstance(body, dict) or set(body) - {'action', 'run_id'}:
        raise HypervisorError('Invalid patch operation')
    if body.get('action') not in ('probe', 'launch', 'status', 'reboot'):
        raise HypervisorError('Unsupported patch operation')
    vm_id = Selection(vm_ids=[request.get('vm_id', '')]).vm_ids[0]
    if body['action'] != 'probe' and not re.fullmatch(r'[0-9a-f]{32}', body.get('run_id', '')):
        raise HypervisorError('Invalid patch reference')
    script = Path(__file__).with_name('guest_patch.py').read_text()
    return execute(vm_id, script, dict(body, source=script))


def guest(vm_id, action, run_id=None):
    return storage.broker('patch_guest', vm_id=vm_id, body=dict(action=action, **({'run_id': run_id} if run_id else {})))


class Manager:
    def __init__(self, store, submit):
        self.store, self.submit = store, submit
        self.lock = threading.RLock()

    def probe(self, vm_id):
        try:
            result = guest(vm_id, 'probe')
        except HypervisorError as exc:
            result = dict(ready=False, message=str(exc))
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO patch_guests VALUES(?,?,?)', (vm_id, time.time(), json.dumps(result)))
        emit(result.get('os', '') + ': ' + result.get('message', 'Linux patching ready') + '\n')
        return result

    def require_ready(self, vm_ids):
        with self.store.db() as db:
            guests = {r['vm_id']: json.loads(r['result']) for r in db.execute('SELECT * FROM patch_guests')}
        if any(not guests.get(v, {}).get('ready') for v in vm_ids):
            raise HypervisorError('Check Linux readiness for every selected VM before patching or scheduling')

    def create(self, spec, username, deadline=None, schedule_id=None, schedule_next=None):
        batch_id = uuid.uuid4().hex
        with self.lock, self.store.db() as db:
            try:
                db.execute('INSERT INTO patch_batches VALUES(?,?,?,?,?,?)',
                           (batch_id, spec.model_dump_json(), username, time.time(), deadline, schedule_id))
                for vm_id in spec.vm_ids:
                    db.execute('INSERT INTO patch_runs(id,batch_id,vm_id,status) VALUES(?,?,?,?)',
                               (uuid.uuid4().hex, batch_id, vm_id, 'pending'))
                if schedule_id:
                    # Claim the calendar slot in the same durable transaction
                    # as its batch, including crashes before worker dispatch.
                    db.execute('UPDATE patch_schedules SET next_run=? WHERE id=?', (schedule_next, schedule_id))
            except sqlite3.IntegrityError as exc:
                raise HypervisorError('A selected VM already has an active patch run; inspect Operations before retrying') from exc
            self.store.audit(username, 'patch.create', batch_id, spec.model_dump_json(), db=db)
        return batch_id

    def finish(self, run_id, status, result):
        with self.store.db() as db:
            db.execute('UPDATE patch_runs SET status=?,result=?,next_check=? WHERE id=?',
                       (status, json.dumps(result), time.time() + 60 if status == 'reconnecting' else 0, run_id))

    def run(self, run_id):
        with self.store.db() as db:
            row = dict(db.execute('SELECT r.*,b.settings,b.deadline FROM patch_runs r JOIN patch_batches b ON b.id=r.batch_id WHERE r.id=?', (run_id,)).fetchone())
        if not row['attempted'] and row['deadline'] and time.time() >= row['deadline']:
            self.finish(run_id, 'skipped', {'message': 'Maintenance window expired before the VM became available'})
            return 'Skipped: maintenance window expired'
        spec = Request.model_validate(json.loads(row['settings']))
        vm_id = row['vm_id']
        with self.store.db() as db:
            db.execute("UPDATE patch_runs SET status='running' WHERE id=?", (run_id,))
        try:
            if not row['attempted']:
                # An unreachable/stopped VM is skipped without a launch attempt.
                readiness = self.probe(vm_id)
                if not readiness.get('ready'):
                    self.finish(run_id, 'skipped', readiness)
                    return 'Skipped: ' + readiness.get('message', 'Linux guest unavailable')
                if row['deadline'] and time.time() >= row['deadline']:
                    self.finish(run_id, 'skipped', {'message': 'Maintenance window expired during readiness checks'})
                    return 'Skipped: maintenance window expired'
                with self.store.db() as db:
                    db.execute('UPDATE patch_runs SET attempted=1 WHERE id=?', (run_id,))
                result = guest(vm_id, 'launch', run_id)
            else:
                result = guest(vm_id, 'status', run_id)
            started = time.monotonic()
            previous = None
            while True:
                message = result.get('message', result.get('status', 'Waiting for guest'))
                if message != previous:
                    emit(vm_id + ': ' + message + '\n')
                    previous = message
                if result.get('status') == 'unavailable':
                    raise HypervisorError(message + '; reconnecting to determine guest transaction state')
                if result.get('status') in ('missing', 'interrupted'):
                    self.finish(run_id, 'interrupted', result)
                    raise HypervisorError(message + '; inspect guest package state before starting another patch')
                if result.get('status') == 'failed':
                    self.finish(run_id, 'failed', result)
                    emit(result.get('output', '') + '\n')
                    raise HypervisorError(message)
                if result.get('status') == 'completed':
                    if spec.reboot == 'if-required' and result.get('reboot_required') is True:
                        if not result.get('reboot_requested'):
                            result = guest(vm_id, 'reboot', run_id)
                        if result.get('reboot_error'):
                            self.finish(run_id, 'failed', result)
                            raise HypervisorError(result['reboot_error'])
                        if result.get('boot_id_now') == result.get('boot_id'):
                            if time.time() - result.get('reboot_requested_at', time.time()) > 900:
                                result['message'] = 'Reboot was requested but not verified within 15 minutes; inspect the guest'
                                self.finish(run_id, 'failed', result)
                                raise HypervisorError(result['message'])
                            emit('Waiting for the guest to reboot and its agent to reconnect.\n') if previous != 'reboot' else None
                            previous = 'reboot'
                        else:
                            result['rebooted'] = True
                            result['reboot_required'] = False
                            self.finish(run_id, 'completed', result)
                            emit(result.get('output', '') + '\n')
                            return message + '; reboot verified'
                    else:
                        self.finish(run_id, 'completed', result)
                        emit(result.get('output', '') + '\n')
                        return message + ('; reboot required' if result.get('reboot_required') else '; reboot requirement unknown' if result.get('reboot_required') is None else '')
                self.finish(run_id, 'running', result)
                if time.monotonic() - started > 6 * 3600:
                    raise HypervisorError('Guest transaction still active after six hours; reconnecting without repeating it')
                time.sleep(5)
                try:
                    result = guest(vm_id, 'status', run_id)
                except HypervisorError:
                    # Reboots and agent package updates can temporarily remove
                    # the connection. Preserve the same transaction token.
                    raise HypervisorError('Guest disconnected; reconnecting to the existing transaction')
        except Exception as exc:
            with self.store.db() as db:
                current = db.execute('SELECT status FROM patch_runs WHERE id=?', (run_id,)).fetchone()[0]
            if current == 'running':
                self.finish(run_id, 'reconnecting', {'message': str(exc)})
                raise PowerInterrupted(str(exc) + '; the original guest transaction will be checked again') from exc
            raise

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            with self.store.db() as db:
                schedules = [dict(r) for r in db.execute('SELECT * FROM patch_schedules WHERE next_run<=?', (now,))]
            for row in schedules:
                plan = Schedule.model_validate_json(row['settings'])
                if not plan.enabled:
                    continue
                deadline = row['next_run'] + plan.window_minutes * 60
                message = 'Skipped: maintenance window was missed'
                if now < deadline:
                    try:
                        batch = self.create(Request.model_validate(plan.model_dump(include=set(Request.model_fields))), 'scheduler', deadline, row['id'], backups.next_run(plan, now))
                        message = 'Queued patch batch ' + batch
                    except HypervisorError as exc:
                        message = str(exc)
                with self.store.db() as db:
                    db.execute('UPDATE patch_schedules SET next_run=?,last_message=? WHERE id=?',
                               (backups.next_run(plan, now), message, row['id']))
                    self.store.audit('scheduler', 'patch.schedule', row['id'], message, db=db)
            with self.store.db() as db:
                batches = [dict(r) for r in db.execute('SELECT * FROM patch_batches WHERE id IN (SELECT batch_id FROM patch_runs WHERE status IN (\'pending\',\'reconnecting\')) ORDER BY created')]
            for batch in batches:
                spec = Request.model_validate_json(batch['settings'])
                with self.store.db() as db:
                    runs = [dict(r) for r in db.execute('SELECT * FROM patch_runs WHERE batch_id=? ORDER BY rowid', (batch['id'],))]
                    stop = spec.stop_on_failure and any(r['status'] in ('failed', 'interrupted') for r in runs)
                    slots = spec.concurrency - sum(r['status'] in ('dispatched', 'running') for r in runs)
                    pending = []
                    for run in runs:
                        if run['status'] == 'pending' and (stop or batch['deadline'] and now >= batch['deadline']):
                            db.execute("UPDATE patch_runs SET status='skipped',result=? WHERE id=?", (json.dumps({'message': 'Batch stopped after a failure' if stop else 'Maintenance window expired'}), run['id']))
                        elif run['status'] in ('pending', 'reconnecting') and run['next_check'] <= now:
                            pending.append(run)
                    # An uncertain transaction must be reconciled before moving
                    # on to the remaining VMs in this batch.
                    if any(r['status'] == 'reconnecting' for r in runs):
                        pending = [r for r in pending if r['status'] == 'reconnecting']
                    for run in pending[:max(0, slots)]:
                        db.execute("UPDATE patch_runs SET status='dispatched' WHERE id=?", (run['id'],))
                for run in pending[:max(0, slots)]:
                    try:
                        queued = self.submit({'username': batch['username']}, 'patch.run', run['vm_id'],
                                             lambda rid=run['id']: self.run(rid), resources={run['vm_id']})
                        with self.store.db() as db:
                            db.execute('UPDATE patch_runs SET task_id=? WHERE id=?', (queued['task_id'], run['id']))
                    except Exception as exc:
                        self.finish(run['id'], 'reconnecting' if run['attempted'] else 'pending', {'message': str(exc)})

    def save_schedule(self, spec, username, schedule_id=None):
        schedule_id = schedule_id or uuid.uuid4().hex
        if spec.enabled:
            self.require_ready(spec.vm_ids)
        with self.lock, self.store.db() as db:
            db.execute('INSERT INTO patch_schedules(id,settings,next_run) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET settings=excluded.settings,next_run=excluded.next_run',
                       (schedule_id, spec.model_dump_json(), backups.next_run(spec, time.time()) if spec.enabled else None))
            self.store.audit(username, 'patch.schedule.save', schedule_id, spec.model_dump_json(), db=db)
        return {'id': schedule_id}
