"""Admission control: в гейте одновременно живёт ровно одна модель."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time


ModelKey = tuple[str, str]


@dataclass(frozen=True)
class AdmissionPolicy:
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")


class Lease:
    def __init__(self, gate: "AdmissionGate", key: ModelKey, policy: AdmissionPolicy, initialize: bool):
        self._gate = gate
        self.backend, self.model = key
        self.max_concurrency = policy.max_concurrency
        self.initialize = initialize
        self._released = False

    def mark_ready(self) -> None:
        self._gate._mark_ready((self.backend, self.model))

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._gate._release((self.backend, self.model), self.initialize)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class AdmissionGate:
    """Пропускает запросы только одной модели сразу.

    Локальные серверы не держат две модель в памяти, поэтому «одна модель» —
    свойство допуска, а не настройка конфига: запрос к другой модели ждёт, пока
    не освободятся все активные лизы. Запросы к уже активной модели проходят
    в пределах её ``max_concurrency``. Ожидающие видны в ``snapshot()`` как
    ``waiting`` — панель показывает их как очередь.
    """

    def __init__(self, policies: dict[ModelKey, AdmissionPolicy], default: AdmissionPolicy | None = None):
        self._policies = policies
        self._default = default or AdmissionPolicy()
        self._condition = threading.Condition()
        self._active: dict[ModelKey, int] = {}
        self._ready: set[ModelKey] = set()
        self._waiting = 0
        self._maintenance = False

    @contextmanager
    def maintenance(self):
        """Не допускаем новые запросы во время остановки и удаления KV."""
        with self._condition:
            if self._maintenance or self._active:
                raise RuntimeError("gate is busy; cancel active requests before maintenance")
            self._maintenance = True
        try:
            yield
        finally:
            with self._condition:
                self._maintenance = False
                self._condition.notify_all()

    def acquire(self, backend: str, model: str, policy: AdmissionPolicy | None = None,
                timeout: float = 600) -> Lease:
        key = backend, model
        policy = policy or self._policies.get(key, self._default)
        deadline = time.monotonic() + timeout
        with self._condition:
            self._waiting += 1
            try:
                while not self._admissible(key, policy):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("admission queue timed out")
                    self._condition.wait(remaining)
                initialize = key not in self._active
                self._active[key] = self._active.get(key, 0) + 1
                return Lease(self, key, policy, initialize)
            finally:
                self._waiting -= 1

    def _admissible(self, key: ModelKey, policy: AdmissionPolicy) -> bool:
        if self._maintenance:
            return False
        own_active = self._active.get(key, 0)
        if own_active >= policy.max_concurrency:
            return False
        if own_active and key not in self._ready:
            return False
        # активны только лизы этой же модели (или никого) — чужая модель = ждём
        return set(self._active) <= {key}

    def _mark_ready(self, key: ModelKey) -> None:
        with self._condition:
            if key not in self._active:
                raise RuntimeError("invalid lease initialization")
            self._ready.add(key)
            self._condition.notify_all()

    def _release(self, key: ModelKey, initialize: bool) -> None:
        with self._condition:
            active = self._active.get(key, 0)
            if active == 0:
                raise RuntimeError("invalid or duplicated lease release")
            if active == 1:
                del self._active[key]
                self._ready.discard(key)
            else:
                self._active[key] = active - 1
            self._condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "active": sum(self._active.values()),
                "models": {f"{backend}/{model}": count for (backend, model), count in self._active.items()},
                "waiting": self._waiting,
            }
