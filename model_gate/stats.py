"""In-memory request statistics: per-model totals, recent ring, live requests."""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
from collections import deque

USAGE_TAIL_BYTES = 8 * 1024
RECENT_LIMIT = 200

# ds4-server пишет в stderr (server_log, без фильтрации уровней):
#   ds4-server: chat ctx=... prefill chunk 1234/5678 (21.7%) chunk=850.12 t/s avg=830.55 t/s 1.483s
#   ds4-server: chat ctx=... prompt done 1.483s
#   ds4-server: chat ctx=... gen=12 ... decoding chunk=41.20 t/s avg=40.10 t/s 12.345s
DS4_PREFILL_RE = re.compile(r"prefill chunk (\d+)/(\d+) \([\d.]+%\) chunk=([\d.]+) t/s avg=([\d.]+) t/s")
DS4_PROMPT_DONE_RE = re.compile(r"prompt done ([\d.]+)s")
DS4_DECODE_RE = re.compile(r"decoding chunk=([\d.]+) t/s avg=([\d.]+) t/s")


def parse_usage(buffer: bytes) -> dict | None:
    """Достать последний "usage":{...} из сырого ответа (JSON или SSE)."""
    text = buffer.decode("utf-8", errors="ignore")
    index = text.rfind('"usage"')
    if index < 0:
        return None
    brace = text.find("{", index)
    if brace < 0:
        return None
    depth = 0
    for position in range(brace, min(len(text), brace + 4096)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                try:
                    usage = json.loads(text[brace:position + 1])
                except json.JSONDecodeError:
                    return None
                return usage if isinstance(usage, dict) else None
    return None


def usage_tokens(usage: dict) -> tuple[int, int, int]:
    """(prompt, completion, cached) из OpenAI-подобного usage-блока."""
    details = usage.get("prompt_tokens_details") or {}
    return (
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        int(details.get("cached_tokens") or 0),
    )


class InFlight:
    """Живая запись обрабатываемого запроса; обновляется из потока копирования."""

    def __init__(self, request_id: int, model: str, backend: str, stream: bool, context_chars: int):
        self.id = request_id
        self.model = model
        self.backend = backend
        self.stream = stream
        self.context_chars = context_chars
        self.started_monotonic = time.monotonic()
        self.started_wall = time.time()
        self.lock = threading.Lock()
        self.bytes = 0
        self.sse_events = 0
        self.first_byte_at: float | None = None
        self.usage: dict | None = None
        self._tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        with self.lock:
            if self.first_byte_at is None:
                self.first_byte_at = time.monotonic()
            self.bytes += len(chunk)
            self.sse_events += chunk.count(b"data:")
            self._tail.extend(chunk)
            if len(self._tail) > USAGE_TAIL_BYTES:
                del self._tail[:-USAGE_TAIL_BYTES]

    def parse_usage(self) -> dict | None:
        with self.lock:
            tail = bytes(self._tail)
        self.usage = parse_usage(tail)
        return self.usage

    def snapshot(self) -> dict:
        with self.lock:
            elapsed = time.monotonic() - self.started_monotonic
            ttft = (self.first_byte_at - self.started_monotonic) if self.first_byte_at else None
            return {
                "id": self.id,
                "model": self.model,
                "backend": self.backend,
                "stream": self.stream,
                "context_chars": self.context_chars,
                "elapsed_s": round(elapsed, 2),
                "ttft_s": round(ttft, 3) if ttft is not None else None,
                "bytes": self.bytes,
                "sse_events": self.sse_events,
                # живой прирост: SSE-событие ~ один токен у ds4/omlx
                "approx_completion_tokens": self.sse_events if self.stream else None,
            }


class StatsCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.started_wall = time.time()
        self.models: dict[str, dict] = {}
        self.recent: deque[dict] = deque(maxlen=RECENT_LIMIT)
        self.inflight: dict[int, InFlight] = {}
        self.backend_metrics: dict[str, dict] = {}

    def record_backend_log(self, backend: str, line: str) -> None:
        """Парсит stderr ds4-server: скорость prefill/decode по каждому backend-у."""
        match = DS4_PREFILL_RE.search(line)
        if match:
            tokens, instant, avg = int(match.group(2)), float(match.group(3)), float(match.group(4))
            with self._lock:
                metrics = self.backend_metrics.setdefault(backend, {})
                metrics["prefill_last_tps"] = avg
                metrics["prefill_peak_tps"] = max(metrics.get("prefill_peak_tps", 0.0), instant)
                metrics["last_prompt_tokens"] = tokens
                metrics["ts"] = time.time()
            return
        match = DS4_PROMPT_DONE_RE.search(line)
        if match:
            seconds = float(match.group(1))
            with self._lock:
                metrics = self.backend_metrics.setdefault(backend, {})
                tokens = metrics.get("last_prompt_tokens", 0)
                if seconds > 0 and tokens:
                    metrics["prefill_avg_tps"] = round(tokens / seconds, 1)
                metrics["prompt_done_s"] = seconds
                metrics["ts"] = time.time()
            return
        match = DS4_DECODE_RE.search(line)
        if match:
            with self._lock:
                metrics = self.backend_metrics.setdefault(backend, {})
                metrics["gen_last_tps"] = float(match.group(1))
                metrics["gen_avg_tps"] = float(match.group(2))
                metrics["ts"] = time.time()
            return
        if "prompt start" in line and "ds4-server" in line:
            with self._lock:
                self.backend_metrics.setdefault(backend, {})["prefill_peak_tps"] = 0.0

    def start(self, model: str, backend: str, stream: bool, context_chars: int) -> InFlight:
        with self._lock:
            inflight = InFlight(next(self._ids), model, backend, stream, context_chars)
            self.inflight[inflight.id] = inflight
        return inflight

    def finish(
        self,
        inflight: InFlight,
        *,
        status: int,
        disconnected: bool = False,
        error: str | None = None,
    ) -> None:
        elapsed = time.monotonic() - inflight.started_monotonic
        usage = inflight.usage or inflight.parse_usage() or {}
        prompt, completion, cached = usage_tokens(usage)
        prefill_tps = usage.get("prompt_tokens_per_second")
        if not prefill_tps and prompt and usage.get("prompt_eval_duration"):
            prefill_tps = round(prompt / usage["prompt_eval_duration"], 1)
        if not prefill_tps:
            # ds4 не кладёт prefill в usage — берём из его stderr-метрик бэкенда,
            # если они обновлялись уже после старта этого запроса
            with self._lock:
                metrics = dict(self.backend_metrics.get(inflight.backend, {}))
            if metrics.get("prefill_avg_tps") and metrics.get("ts", 0) >= inflight.started_wall - 1:
                prefill_tps = metrics["prefill_avg_tps"]
        key = f"{inflight.backend}/{inflight.model}"
        record = {
            "ts": round(inflight.started_wall, 3),
            "model": inflight.model,
            "backend": inflight.backend,
            "stream": inflight.stream,
            "status": status,
            "disconnected": disconnected,
            "error": error,
            "elapsed_s": round(elapsed, 3),
            "ttft_s": round(inflight.first_byte_at - inflight.started_monotonic, 3) if inflight.first_byte_at else None,
            "context_chars": inflight.context_chars,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": cached,
            "prefill_tps": prefill_tps,
            "tps": round(completion / elapsed, 2) if completion and elapsed > 0 else None,
        }
        with self._lock:
            self.inflight.pop(inflight.id, None)
            model = self.models.setdefault(key, {
                "requests": 0, "errors": 0, "disconnected": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                "total_elapsed_s": 0.0, "last_elapsed_s": None, "last_ts": None,
                "last_prompt_tokens": None, "last_completion_tokens": None,
            })
            model["requests"] += 1
            model["errors"] += 1 if status >= 400 or error else 0
            model["disconnected"] += 1 if disconnected else 0
            model["prompt_tokens"] += prompt
            model["completion_tokens"] += completion
            model["cached_tokens"] += cached
            model["total_elapsed_s"] = round(model["total_elapsed_s"] + elapsed, 3)
            model["last_elapsed_s"] = record["elapsed_s"]
            model["last_ts"] = record["ts"]
            if prompt:
                model["last_prompt_tokens"] = prompt
            if completion:
                model["last_completion_tokens"] = completion
            if prefill_tps:
                model["last_prefill_tps"] = round(float(prefill_tps), 1)
            self.recent.appendleft(record)

    def drop(self, inflight: InFlight) -> None:
        with self._lock:
            self.inflight.pop(inflight.id, None)

    def snapshot(self) -> dict:
        with self._lock:
            models = {key: dict(value) for key, value in self.models.items()}
            recent = list(self.recent)
            for key, model in models.items():
                elapsed = model["total_elapsed_s"]
                model["avg_tps"] = round(model["completion_tokens"] / elapsed, 2) if elapsed > 0 and model["completion_tokens"] else None
                model["cache_hit_rate"] = (
                    round(100 * model["cached_tokens"] / model["prompt_tokens"], 1)
                    if model["prompt_tokens"] else None
                )
            return {
                "since": round(self.started_wall, 3),
                "uptime_s": round(time.time() - self.started_wall, 1),
                "models": models,
                "recent": recent,
                "inflight": [entry.snapshot() for entry in self.inflight.values()],
                "backend_metrics": {name: dict(value) for name, value in self.backend_metrics.items()},
            }
