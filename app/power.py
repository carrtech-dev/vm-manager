"""Coordinate emergency stops with queued and in-flight power requests."""

import threading
import weakref
from contextlib import contextmanager

from .hypervisor import HypervisorError


class PowerInterrupted(HypervisorError):
    pass


class PowerRequests:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = {}
        self.transitions = {}

    def register(self, vm_id, action):
        cancelled = threading.Event()
        with self.lock:
            requests = self.requests.setdefault(vm_id, weakref.WeakSet())
            if action == "stop":
                for previous in requests:
                    previous.set()
            requests.add(cancelled)
            self.transitions.setdefault(vm_id, threading.Lock())
        return cancelled

    @staticmethod
    def check(cancelled):
        if cancelled is not None and cancelled.is_set():
            raise PowerInterrupted(
                "Superseded by Force stop. Unapplied hardware changes remain saved."
            )

    @contextmanager
    def transition(self, vm_id, cancelled):
        # Never hold this lock while waiting for graceful shutdown: a boot
        # menu may never respond. Serialize start/apply and force stop only.
        with self.lock:
            lock = self.transitions.setdefault(vm_id, threading.Lock())
        with lock:
            self.check(cancelled)
            yield
