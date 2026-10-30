"""Concurrency, ordering and shared-resource regression checks."""
import threading

import pytest

from app.task_queue import ResourceExecutor


def test_busy_vm_backlog_does_not_occupy_workers_and_preserves_order():
    queue = ResourceExecutor(max_workers=2)
    release, entered = threading.Event(), threading.Event()
    calls = []
    def first():
        entered.set()
        assert release.wait(5)
        calls.append(0)
    try:
        first_job = queue.submit(first, resources={'incus:one'})
        assert entered.wait(2)
        backlog = [queue.submit(lambda n=n: calls.append(n), resources={'incus:one'}) for n in range(1, 10)]
        other = queue.submit(lambda: 'independent', resources={'incus:two'})
        assert other.result(timeout=2) == 'independent'
        assert not calls
        assert all(not f.running() for f in backlog)
        release.set()
        first_job.result(timeout=2)
        for future in backlog:
            future.result(timeout=2)
        assert calls == list(range(10))
    finally:
        release.set()
        queue.shutdown()


def test_worker_limit_and_failed_job_releases_its_vm():
    queue = ResourceExecutor(max_workers=4)
    release = threading.Event()
    barrier = threading.Barrier(5)
    def hold():
        barrier.wait(timeout=3)
        assert release.wait(5)
        raise ValueError('injected failure')
    try:
        busy = [queue.submit(hold, resources={f'incus:{n}'}) for n in range(4)]
        barrier.wait(timeout=3)
        fifth = queue.submit(lambda: 'fifth', resources={'incus:fifth'})
        same = queue.submit(lambda: 'same', resources={'incus:0'})
        assert not fifth.running() and not same.running()
        release.set()
        for future in busy:
            with pytest.raises(ValueError, match='injected'):
                future.result(timeout=3)
        assert fifth.result(timeout=3) == 'fifth'
        assert same.result(timeout=3) == 'same'
    finally:
        release.set()
        queue.shutdown()


def test_restore_reserves_source_destination_and_keeps_conflicting_fifo():
    queue = ResourceExecutor()
    release, entered = threading.Event(), threading.Event()
    calls = []
    def hold():
        entered.set()
        assert release.wait(5)
    try:
        queue.submit(hold, resources={'incus:destination'})
        assert entered.wait(2)
        restore = queue.submit(lambda: calls.append('restore'), resources={'incus:source', 'incus:destination'})
        delete = queue.submit(lambda: calls.append('delete'), resources={'incus:source'})
        start = queue.submit(lambda: calls.append('start'), resources={'incus:destination'})
        queue.submit(lambda: None, resources={'libvirt:other'}).result(timeout=2)
        assert not calls
        release.set()
        for future in (restore, delete, start):
            future.result(timeout=2)
        assert calls[0] == 'restore'
        assert set(calls[1:]) == {'delete', 'start'}
    finally:
        release.set()
        queue.shutdown()


def test_host_barrier_waits_for_older_jobs_and_blocks_newer_jobs():
    queue = ResourceExecutor()
    release, entered = threading.Event(), threading.Event()
    calls = []
    def hold():
        entered.set()
        assert release.wait(5)
        calls.append('vm')
    try:
        queue.submit(hold, resources={'incus:one'})
        assert entered.wait(2)
        host = queue.submit(lambda: calls.append('host'))
        newer = queue.submit(lambda: calls.append('newer'), resources={'incus:two'})
        assert not host.running() and not newer.running()
        release.set()
        newer.result(timeout=2)
        assert calls == ['vm', 'host', 'newer']
    finally:
        release.set()
        queue.shutdown()


def test_shutdown_drains_queued_jobs_and_cancelled_job_does_not_block():
    queue = ResourceExecutor(max_workers=1)
    release, entered = threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(5)
    try:
        queue.submit(hold, resources={'incus:one'})
        assert entered.wait(2)
        cancelled = queue.submit(lambda: pytest.fail('cancelled job ran'), resources={'incus:one'})
        assert cancelled.cancel()
        last = queue.submit(lambda: 'drained', resources={'incus:one'})
        queue.shutdown(wait=False)
        with pytest.raises(RuntimeError, match='shutdown'):
            queue.submit(lambda: None)
        release.set()
        assert last.result(timeout=2) == 'drained'
    finally:
        release.set()
        queue.shutdown()
