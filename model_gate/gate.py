"""FIFO admission: one model at a time, cancellable queue and explicit halt."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import itertools
import threading
import time
from typing import Callable

ModelKey = tuple[str, str]


class RequestCancelled(RuntimeError):
    pass


class GateHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class AdmissionPolicy:
    max_concurrency: int = 1

    def __post_init__(self):
        if self.max_concurrency < 1:
            raise ValueError('max_concurrency must be positive')


@dataclass(eq=False)
class Waiting:
    id: int
    key: ModelKey
    since: float
    cancelled: bool = False


class Lease:
    def __init__(self, gate, key, policy, initialize):
        self._gate = gate
        self.backend, self.model = key
        self.max_concurrency = policy.max_concurrency
        self.initialize = initialize
        self._released = False

    def mark_ready(self):
        self._gate._mark_ready((self.backend, self.model))

    def release(self):
        if not self._released:
            self._released = True
            self._gate._release((self.backend, self.model))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class AdmissionGate:
    def __init__(self, policies, default=None):
        self._policies = policies
        self._default = default or AdmissionPolicy()
        self._condition = threading.Condition()
        self._active = {}
        self._ready = set()
        self._queue: deque[Waiting] = deque()
        self._ids = itertools.count(-1, -1)
        self._maintenance = False
        self._halted = False

    def halt(self):
        with self._condition:
            self._halted = True
            for entry in self._queue:
                entry.cancelled = True
            self._condition.notify_all()

    def resume(self):
        with self._condition:
            self._halted = False
            self._condition.notify_all()

    def cancel(self, request_id):
        with self._condition:
            for entry in self._queue:
                if entry.id == request_id:
                    entry.cancelled = True
                    self._condition.notify_all()
                    return True
        return False

    @contextmanager
    def maintenance(self):
        with self._condition:
            if self._maintenance or self._active:
                raise RuntimeError('gate is busy; cancel active requests before maintenance')
            self._maintenance = True
        try:
            yield
        finally:
            with self._condition:
                self._maintenance = False
                self._condition.notify_all()

    def acquire(self, backend, model, policy=None, timeout=600, *, request_id=None,
                cancelled: Callable[[], bool] | None = None):
        key = backend, model
        policy = policy or self._policies.get(key, self._default)
        deadline = time.monotonic() + timeout
        with self._condition:
            entry = Waiting(next(self._ids) if request_id is None else request_id, key, time.monotonic())
            self._queue.append(entry)
            self._condition.notify_all()
            try:
                while True:
                    if entry.cancelled or (cancelled and cancelled()):
                        raise RequestCancelled('request cancelled or client disconnected')
                    if self._halted:
                        raise GateHalted('gate is halted; resume explicitly before inference')
                    if self._queue[0] is entry and self._admissible(key, policy):
                        initialize = key not in self._active
                        self._active[key] = self._active.get(key, 0) + 1
                        return Lease(self, key, policy, initialize)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('admission queue timed out')
                    self._condition.wait(min(remaining, 0.2))
            finally:
                self._queue.remove(entry)
                self._condition.notify_all()

    def _admissible(self, key, policy):
        own = self._active.get(key, 0)
        return (not self._maintenance and own < policy.max_concurrency
                and (not own or key in self._ready) and set(self._active) <= {key})

    def _mark_ready(self, key):
        with self._condition:
            if key not in self._active:
                raise RuntimeError('invalid lease initialization')
            self._ready.add(key)
            self._condition.notify_all()

    def _release(self, key):
        with self._condition:
            active = self._active.get(key, 0)
            if not active:
                raise RuntimeError('invalid or duplicated lease release')
            if active == 1:
                del self._active[key]
                self._ready.discard(key)
            else:
                self._active[key] -= 1
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {
                'active': sum(self._active.values()),
                'models': {f'{b}/{m}': count for (b, m), count in self._active.items()},
                'waiting': len(self._queue), 'halted': self._halted,
                'queue': [{'id': e.id, 'backend': e.key[0], 'model': e.key[1],
                           'age_s': round(time.monotonic() - e.since, 2)} for e in self._queue],
            }
