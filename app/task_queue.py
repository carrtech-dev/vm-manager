"""Bounded execution with FIFO ordering for overlapping resource sets.

An unkeyed job is an exclusive host operation. Waiting jobs never occupy worker
threads, so a busy VM cannot prevent unrelated VMs from using spare capacity.
"""

import threading
from concurrent.futures import Future, ThreadPoolExecutor


class ResourceExecutor:
    def __init__(self, max_workers=4, thread_name_prefix='hypervisor'):
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self.limit = max_workers
        self.condition = threading.Condition()
        self.pending = []
        self.active = {}
        self.closed = False

    @staticmethod
    def conflicts(left, right):
        return left is None or right is None or not left.isdisjoint(right)

    def submit(self, fn, *args, resources=None, **kwargs):
        keys = frozenset(resources) if resources else None
        future = Future()
        with self.condition:
            if self.closed:
                raise RuntimeError('Cannot submit after shutdown')
            self.pending.append((future, keys, fn, args, kwargs))
            self._dispatch()
        return future

    def _dispatch(self):
        waiting = []
        remaining = []
        for job in self.pending:
            future, keys, *_ = job
            if future.cancelled():
                continue
            if (len(self.active) >= self.limit or
                    any(self.conflicts(keys, other) for other in self.active.values()) or
                    any(self.conflicts(keys, other) for other in waiting)):
                remaining.append(job)
                waiting.append(keys)
            else:
                self.active[future] = keys
                self.pool.submit(self._execute, job)
        self.pending = remaining

    def _execute(self, job):
        future, _, fn, args, kwargs = job
        try:
            if future.set_running_or_notify_cancel():
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            with self.condition:
                del self.active[future]
                self._dispatch()
                self.condition.notify_all()

    def shutdown(self, wait=True):
        # Drain our pending queue before closing the underlying worker pool.
        # A background closer preserves the usual nonblocking shutdown option.
        with self.condition:
            self.closed = True
        def drain():
            with self.condition:
                self.condition.wait_for(lambda: not self.pending and not self.active)
            self.pool.shutdown(wait=True)
        if wait:
            drain()
        else:
            threading.Thread(target=drain, name='task-queue-shutdown').start()
