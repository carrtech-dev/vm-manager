"""Real pipe streaming, bounded retention and task/thread isolation."""
import asyncio
import subprocess
import sys
import threading

import httpx
import pytest

from app.hypervisor import Incus, run
from app.security import Store
from app.task_output import Recorder, capture, emit, stream_command, MAX_ENTRIES, CHUNK_SIZE


def test_output_arrives_before_process_exits_and_preserves_streams():
    arrived = threading.Event()
    output, result = [], []
    # A file-backed release avoids relying on a sleep to prove early delivery.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as directory:
        flag = Path(directory) / 'release'
        script = ("import os,time,sys; from pathlib import Path; "
                  "os.write(1, b'first\\n'); os.write(2, b'warning\\n'); "
                  f"flag=Path({str(flag)!r})\n"
                  "while not flag.exists(): time.sleep(.01)\n"
                  "print('last')")
        def sink(text):
            output.append(text)
            if 'first' in ''.join(output) and 'warning' in ''.join(output):
                arrived.set()
        def worker():
            with capture(sink):
                result.append(stream_command((sys.executable, '-u', '-c', script), 5, None))
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            assert arrived.wait(3), output
            assert thread.is_alive() and not result
        finally:
            flag.touch()
            thread.join(5)
        assert result[0].stdout == 'first\nlast\n'
        assert result[0].stderr == 'warning\n'


def test_command_timeout_and_stdin_do_not_deadlock():
    with capture(lambda text: None):
        result = stream_command((sys.executable, '-c', 'import sys; print(sys.stdin.read())'), 5, 'x' * 100000)
        assert result.stdout.strip() == 'x' * 100000
        with pytest.raises(subprocess.TimeoutExpired):
            stream_command((sys.executable, '-c', 'import time; time.sleep(30)'), .1, None)


def test_only_mutation_output_is_captured(monkeypatch):
    from app import hypervisor
    calls, messages = [], []
    def streamed(args, timeout, stdin):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, 'Started', '')
    monkeypatch.setattr(hypervisor, 'stream_command', streamed)
    monkeypatch.setattr(hypervisor.subprocess, 'run', lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, '<secret/>', ''))
    with capture(messages.append):
        assert run('virsh', '-c', 'qemu:///system', 'dumpxml', 'demo') == '<secret/>'
        run('virsh', '-c', 'qemu:///system', 'start', 'demo', stdin='private input')
    assert len(calls) == 1 and calls[0][3] == 'start'
    assert 'secret' not in ''.join(messages) and 'private' not in ''.join(messages)
    assert run('virsh', '-c', 'qemu:///system', 'start', 'demo') == '<secret/>'
    assert len(calls) == 1


def test_retention_persists_and_concurrent_task_contexts_are_isolated(tmp_path):
    store = Store(tmp_path)
    with store.db() as db:
        for task_id in ('a', 'b'):
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)', (task_id, 'test', '', 'operator', 'running', 0, None, ''))
    barrier = threading.Barrier(2)
    def worker(task_id):
        with capture(Recorder(store, task_id)):
            barrier.wait(timeout=3)
            emit(task_id * (MAX_ENTRIES + 8) * CHUNK_SIZE)
        emit('must not be recorded')
    threads = [threading.Thread(target=worker, args=(task_id,)) for task_id in ('a', 'b')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    # Reopening the store preserves logs and marks running work interrupted.
    reopened = Store(tmp_path)
    with reopened.db() as db:
        for task_id in ('a', 'b'):
            rows = db.execute('SELECT * FROM task_output WHERE task_id=? ORDER BY seq', (task_id,)).fetchall()
            assert len(rows) == MAX_ENTRIES
            assert rows[0]['seq'] == 9
            assert all(row['text'] == task_id * CHUNK_SIZE for row in rows)
            assert db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0] == 'interrupted'


def test_incus_reports_async_host_progress_without_configuration(monkeypatch):
    from app import hypervisor
    messages, waits = [], []
    statuses = iter([(105, 'Pending', '0%'), (103, 'Running', '50%'), (200, 'Success', '100%')])
    def handler(request):
        if request.method == 'PUT':
            return httpx.Response(202, json={'type': 'async', 'operation': '/1.0/operations/test?project=default'})
        if '/wait' in request.url.path:
            return httpx.Response(500, json={'type': 'error', 'error': 'context deadline exceeded'})
        waits.append(str(request.url))
        code, status, progress = next(statuses)
        return httpx.Response(200, json={'type': 'sync', 'metadata': {
            'status_code': code, 'status': status, 'metadata': {
                'download_progress': progress, 'config': 'guest-password-secret'}}})
    factory = httpx.Client
    monkeypatch.setattr(hypervisor.httpx, 'Client', lambda **kwargs:
                        factory(transport=httpx.MockTransport(handler), base_url='http://incus'))
    monkeypatch.setattr(hypervisor.time, 'sleep', lambda _: None)
    with capture(messages.append):
        Incus().request('PUT', '/1.0/instances/demo/state', {'action': 'start', 'password': 'private'})
    assert len(waits) == 3 and all('/wait' not in path and 'project=default' in path for path in waits)
    assert '50%' in ''.join(messages) and '100%' in ''.join(messages)
    assert 'secret' not in ''.join(messages) and 'private' not in ''.join(messages)


def test_stream_rechecks_access_and_disconnects_without_further_reads(monkeypatch):
    import app.main as main
    class Request:
        disconnected = False
        async def is_disconnected(self):
            return self.disconnected
    request = Request()
    allowed = True
    def session(request):
        if not allowed:
            raise main.HTTPException(401, 'Expired')
        return {'role': 'viewer'}
    reads = []
    monkeypatch.setattr(main, 'session', session)
    monkeypatch.setattr(main, 'output_batch', lambda *args:
                        (reads.append(args) or {'status': 'running'}, [{'seq': 1, 'at': 0, 'text': 'first'}]))
    async def check():
        nonlocal allowed
        events = main.task_output_events(request, 'id', 0)
        assert 'event: output' in await anext(events)
        allowed = False
        assert 'event: denied' in await anext(events)
        with pytest.raises(StopAsyncIteration):
            await anext(events)
        request.disconnected = True
        events = main.task_output_events(request, 'id', 1)
        with pytest.raises(StopAsyncIteration):
            await anext(events)
    asyncio.run(check())
    assert len(reads) == 1
