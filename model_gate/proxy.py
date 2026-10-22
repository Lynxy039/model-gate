"""OpenAI-compatible reverse proxy with model-class admission control."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .gate import AdmissionGate, AdmissionPolicy
from .router import ModelRouter
from .stats import InFlight, StatsCollector


LOGGER = logging.getLogger(__name__)

BACKEND_V1_RE = re.compile(r"^/([^/]+)/v1/(.*)$")
# алиас единой точки входа: Pi шлёт model="auto", гейт подставляет выбранную в панели
AUTO_ALIAS = "auto"


def _model_entry(backend: Backend, model: str) -> dict:
    # Backend metadata (max_model_len, supported_parameters, ...) passes through
    # as-is; id/object/owned_by are authoritative from the gate.
    return {**backend.model_meta.get(model, {}), "id": model, "object": "model", "owned_by": backend.name}


def _context_chars(payload: dict) -> int:
    context = payload.get("messages", payload.get("prompt", ""))
    return len(json.dumps(context, ensure_ascii=False))


@dataclass
class Backend:
    name: str
    base_url: str
    max_concurrency: int
    model_policies: dict[str, dict] = field(default_factory=dict)
    model_commands: dict[str, list[str]] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    model_meta: dict[str, dict] = field(default_factory=dict)
    prefixes: list[str] = field(default_factory=list)
    discover_models: bool = False
    model_list_path: str = "/v1/models"
    load_url: str | None = None
    unload_url: str | None = None
    unload_all_url: str | None = None
    requires_unload: bool = True
    settle_seconds: float = 0
    unload_settle_seconds: float = 0
    load_settle_seconds: float = 0
    ready_path: str = "/v1/models"
    model_status_path: str | None = "/v1/models/status"
    autostart_command: list[str] | None = None
    startup_timeout: float = 120
    startup_poll_seconds: float = 1
    stop_on_proxy_exit: bool = True
    inject_stream_usage: bool = True

    @classmethod
    def from_config(cls, name: str, config: dict) -> "Backend":
        return cls(
            name=name,
            base_url=config["base_url"].rstrip("/"),
            max_concurrency=int(config.get("max_concurrency", 1)),
            model_policies=dict(config.get("model_policies", {})),
            model_commands={model: list(command) for model, command in config.get("model_commands", {}).items()},
            models=list(config.get("models", [])),
            prefixes=list(config.get("prefixes", [])),
            discover_models=bool(config.get("discover_models", False)),
            model_list_path=config.get("model_list_path", "/v1/models"),
            load_url=config.get("load_url"),
            unload_url=config.get("unload_url"),
            unload_all_url=config.get("unload_all_url"),
            requires_unload=bool(config.get("requires_unload", True)),
            settle_seconds=float(config.get("settle_seconds", 0)),
            unload_settle_seconds=float(config.get("unload_settle_seconds", 0)),
            load_settle_seconds=float(config.get("load_settle_seconds", 0)),
            ready_path=config.get("ready_path", "/v1/models"),
            model_status_path=config.get("model_status_path", "/v1/models/status"),
            autostart_command=config.get("autostart_command"),
            startup_timeout=float(config.get("startup_timeout", 120)),
            startup_poll_seconds=float(config.get("startup_poll_seconds", 1)),
            stop_on_proxy_exit=bool(config.get("stop_on_proxy_exit", True)),
            inject_stream_usage=bool(config.get("inject_stream_usage", True)),
        )

    def admin_url(self, template: str, model: str | None = None) -> str:
        return template.replace("{model}", quote(model or "", safe=""))

    def command_for(self, model: str) -> list[str] | None:
        return self.model_commands.get(model, self.autostart_command)

    def admission_policy(self, model: str) -> AdmissionPolicy:
        config = self.model_policies.get(model, {})
        return AdmissionPolicy(
            max_concurrency=int(config.get("max_concurrency", self.max_concurrency)),
            sharing_group=config.get("sharing_group"),
        )


class LifecycleError(RuntimeError):
    pass


class ProcessManager:
    """Starts optional backend servers and waits for their readiness endpoint."""

    def __init__(self, backends: dict[str, Backend], timeout: float = 5, log_sink=None):
        self.backends = backends
        self.timeout = timeout
        self.log_sink = log_sink
        self._processes: dict[str, subprocess.Popen] = {}
        self._started: dict[str, float] = {}
        self._commands: dict[str, list[str]] = {}
        self._models: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def _pump_log(self, backend_name: str, process: subprocess.Popen) -> None:
        # stderr потомка идёт и в консоль gate (как при ручном запуске), и в
        # парсер метрик (prefill/decode t/s у ds4)
        if process.stderr is None:
            return
        try:
            for line in process.stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
                if self.log_sink:
                    try:
                        self.log_sink(backend_name, line)
                    except Exception:
                        pass
        except (OSError, TypeError, ValueError):
            return  # pipe закрылся или мок в тестах

    def ensure_ready(self, backend_name: str, model: str | None = None) -> None:
        backend = self.backends[backend_name]
        command = backend.command_for(model) if model is not None else backend.autostart_command
        if not command:
            return
        with self._lock:
            restarting = False
            process = self._processes.get(backend_name)
            if process is not None and process.poll() is not None:
                self._processes.pop(backend_name, None)
                self._commands.pop(backend_name, None)
                self._models.pop(backend_name, None)
                process = None
            if process is not None and self._commands.get(backend_name) != command:
                self._stop(process)
                restarting = True
                self._processes.pop(backend_name, None)
                self._commands.pop(backend_name, None)
                self._models.pop(backend_name, None)
                process = None
            if process is None and not restarting and self._ready(backend):
                return
            if process is None:
                try:
                    process = subprocess.Popen(
                        command,
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except OSError as error:
                    raise LifecycleError(
                        f"failed to start {backend_name}: {error}"
                    ) from error
                self._processes[backend_name] = process
                self._started[backend_name] = time.time()
                self._commands[backend_name] = command
                self._models[backend_name] = model
                threading.Thread(
                    target=self._pump_log, args=(backend_name, process), daemon=True
                ).start()

            deadline = time.monotonic() + backend.startup_timeout
            while time.monotonic() < deadline:
                if self._ready(backend):
                    return
                if process.poll() is not None:
                    raise LifecycleError(
                        f"{backend_name} exited during startup with code {process.returncode}"
                    )
                time.sleep(backend.startup_poll_seconds)
            raise LifecycleError(
                f"{backend_name} did not become ready within {backend.startup_timeout:g}s"
            )

    def _stop(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                name: {"running": True, "pid": process.pid, "model": self._models.get(name),
                      "started_ts": self._started.get(name)}
                for name, process in self._processes.items()
                if process.poll() is None
            }

    def stop(self, backend_name: str) -> bool:
        with self._lock:
            process = self._processes.pop(backend_name, None)
            self._started.pop(backend_name, None)
            self._commands.pop(backend_name, None)
            self._models.pop(backend_name, None)
        if process is None or process.poll() is not None:
            return False
        self._stop(process)
        return True

    def stop_all(self) -> None:
        with self._lock:
            processes = [
                (name, process)
                for name, process in self._processes.items()
                if self.backends[name].stop_on_proxy_exit
            ]
            for name, _ in processes:
                self._processes.pop(name, None)
                self._commands.pop(name, None)
                self._models.pop(name, None)
        for _, process in processes:
            if process.poll() is None:
                process.terminate()

    def is_ready(self, backend_name: str, timeout: float | None = None) -> bool:
        return self._ready(self.backends[backend_name], timeout)

    def _ready(self, backend: Backend, timeout: float | None = None) -> bool:
        request = Request(backend.base_url + backend.ready_path, method="GET")
        try:
            with urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:
                return 200 <= response.status < 300
        except (HTTPError, URLError, TimeoutError, OSError):
            return False


class Lifecycle:
    def __init__(
        self,
        backends: dict[str, Backend],
        require_unload: bool = True,
        timeout: float = 30,
        sleep_fn=time.sleep,
    ):
        self.backends = backends
        self.require_unload = require_unload
        self.timeout = timeout
        self.sleep = sleep_fn
        self._models: dict[str, set[str]] = {name: set() for name in backends}
        self._lock = threading.Lock()

    def activate(self, target: str, model: str, sharing_group: str | None) -> None:
        """Make one model available without disturbing peers in its light group."""
        with self._lock:
            unload = [
                (backend_name, loaded_model)
                for backend_name, models in self._models.items()
                for loaded_model in models
                if (backend_name, loaded_model) != (target, model)
                and (
                    sharing_group is None
                    or self.backends[backend_name].admission_policy(loaded_model).sharing_group != sharing_group
                )
            ]
            self._unload(unload, target)
            backend = self.backends[target]
            if model in self._models[target] and self._is_loaded(backend, model):
                return
            self._models[target].discard(model)
            if backend.load_url:
                self._post(backend.admin_url(backend.load_url, model))
                if backend.load_settle_seconds:
                    self.sleep(backend.load_settle_seconds)
            self._models[target].add(model)

    def _is_loaded(self, backend: Backend, model: str) -> bool:
        if not backend.model_status_path:
            return True
        request = Request(backend.base_url + backend.model_status_path, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = json.loads(response.read())
            return any(entry.get("id") == model and entry.get("loaded") for entry in status.get("models", []))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            return False

    def _unload(self, entries: list[tuple[str, str]], target: str) -> None:
        by_backend: dict[str, list[str]] = {}
        for backend, model in entries:
            by_backend.setdefault(backend, []).append(model)
        for backend_name, models in by_backend.items():
            backend = self.backends[backend_name]
            if backend.settle_seconds:
                self.sleep(backend.settle_seconds)
            unloaded = False
            if backend.unload_all_url:
                self._post(backend.admin_url(backend.unload_all_url))
                unloaded = True
            elif backend.unload_url:
                for model in models:
                    # ponytail: skip unload for models the backend already dropped (e.g. OMLX TTL)
                    if self._is_loaded(backend, model):
                        self._post(backend.admin_url(backend.unload_url, model))
                        unloaded = True
            elif backend.requires_unload and self.require_unload:
                raise LifecycleError(
                    f"refusing switch {backend_name}->{target}: no unload hook configured for {backend_name}"
                )
            self._models[backend_name].difference_update(models)
            if unloaded and backend.unload_settle_seconds:
                self.sleep(backend.unload_settle_seconds)

    def snapshot(self) -> dict[str, list[str]]:
        with self._lock:
            return {name: sorted(models) for name, models in self._models.items()}

    def _post(self, url: str) -> None:
        request = Request(
            url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise LifecycleError(f"lifecycle endpoint returned HTTP {response.status}: {url}")
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise LifecycleError(f"lifecycle endpoint failed: {url}: {error}") from error


class GateServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # keep-alive соединение сбросил клиент (abort/отмена Pi) — штатная
        # ситуация, а не ошибка прокси; не засоряем лог трейсбеками сокета
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, address, handler, config: dict):
        self.backends = {
            name: Backend.from_config(name, backend_config)
            for name, backend_config in config["backends"].items()
        }
        self._model_refresh_lock = threading.Lock()
        self.model_discovery_timeout = float(config.get("model_discovery_timeout", 3))
        self.router = ModelRouter({name: backend.__dict__ for name, backend in self.backends.items()})
        self.gate = AdmissionGate({
            (name, model): backend.admission_policy(model)
            for name, backend in self.backends.items()
            for model in set(backend.models) | set(backend.model_policies)
        })
        self.lifecycle = Lifecycle(
            self.backends,
            require_unload=config.get("require_unload", True),
            timeout=float(config.get("lifecycle_timeout", 30)),
        )
        self.stats = StatsCollector()
        self.processes = ProcessManager(self.backends, log_sink=self.stats.record_backend_log)
        # модель по умолчанию для алиаса "auto": выбор из панели (POST /admin/select)
        # переживает рестарт гейта через файл, иначе — auto_model из конфига
        self._selected_lock = threading.Lock()
        self._selected_file = Path("/tmp/model-gate.selected")
        self.selected = self._load_selected() or str(config.get("auto_model", "")) or None
        self._last_discovery = 0.0
        self.max_body_bytes = int(config.get("max_body_bytes", 64 * 1024 * 1024))
        self.upstream_timeout = float(config.get("upstream_timeout", 600))
        self.failure_cooldown = float(config.get("lifecycle_failure_cooldown", 5))
        self.state_lock = threading.Lock()
        # живые запросы для отмены из панели: id InFlight -> активный handler
        self.active_requests: dict[int, "ProxyHandler"] = {}
        self._active_lock = threading.Lock()
        self._blocked_until = 0.0
        self._blocked_reason = ""
        super().__init__(address, handler)
        self.refresh_models()

    def resolve_auto(self) -> str:
        with self._selected_lock:
            return self.selected or ""

    def select_model(self, model: str) -> None:
        with self._selected_lock:
            self.selected = model
            try:
                self._selected_file.write_text(model)
            except OSError:
                pass  # персистентность — удобство, не обязательное условие

    def _load_selected(self) -> str:
        try:
            return self._selected_file.read_text().strip()
        except OSError:
            return ""

    def refresh_models(self, only: str | None = None) -> None:
        # ponytail: serial refresh — up to len(backends) * model_discovery_timeout
        # while backends are down; GET /v1/models blocks on it (pi's discovery
        # poll times out at 5s). Parallelize with ThreadPoolExecutor if that bites.
        with self._model_refresh_lock:
            for name, backend in self.backends.items():
                if only is not None and name != only:
                    continue
                if not backend.discover_models:
                    continue
                request = Request(backend.base_url + backend.model_list_path, method="GET")
                try:
                    with urlopen(request, timeout=self.model_discovery_timeout) as response:
                        payload = json.loads(response.read())
                    items = payload if isinstance(payload, list) else payload.get("data", payload.get("models", []))
                    discovered = {
                        item["id"]: {k: v for k, v in item.items() if k not in ("id", "object")}
                        for item in items
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
                    backend.models = sorted(set(backend.models) | set(discovered))
                    backend.model_meta.update(discovered)
                    LOGGER.info("discovered models backend=%s count=%d", name, len(discovered))
                except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, AttributeError, TypeError):
                    LOGGER.warning("model discovery unavailable backend=%s", name)
            self.router = ModelRouter({name: backend.__dict__ for name, backend in self.backends.items()})

    def admin_status(self) -> dict[str, object]:
        # discovery жил за счёт клиентских опросов /v1/models; теперь наружу
        # торчит только auto — обновляем список моделей попутно, не чаще раза в 15с
        if time.monotonic() - self._last_discovery > 15:
            self._last_discovery = time.monotonic()
            self.refresh_models()
        admission = self.gate.snapshot()
        processes = self.processes.snapshot()
        loaded = self.lifecycle.snapshot()
        active = admission["models"]
        backends = []
        for name, backend in self.backends.items():
            process = processes.get(name)
            models = sorted(set(backend.models) | set(backend.model_commands) | set(backend.model_policies))
            backends.append({
                "name": name,
                "ready": self.processes.is_ready(name, timeout=0.2),
                "process": process,
                "models": [{
                    "id": model,
                    "active": active.get(f"{name}/{model}", 0),
                    "loaded": model in loaded[name],
                    "running": bool(process and process["model"] == model),
                } for model in models],
            })
        return {**admission, "selected": self.selected, "backends": backends}

    def lifecycle_available(self) -> tuple[bool, str]:
        with self.state_lock:
            if time.monotonic() < self._blocked_until:
                return False, self._blocked_reason
            return True, ""

    def block_lifecycle(self, reason: str) -> None:
        with self.state_lock:
            self._blocked_until = time.monotonic() + self.failure_cooldown
            self._blocked_reason = reason

    def clear_lifecycle_failure(self) -> None:
        with self.state_lock:
            self._blocked_until = 0.0
            self._blocked_reason = ""


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/admin/status":
            if self._admin_allowed():
                self._json(200, self.server.admin_status())
            return
        if self.path == "/admin/stats":
            if self._admin_allowed():
                self._json(200, self.server.stats.snapshot())
            return
        if self.path == "/healthz":
            self._json(200, {"ok": True, **self.server.gate.snapshot()})
            return
        if self.path == "/status":
            self._json(200, self.server.gate.snapshot())
            return
        if self.path == "/v1/models":
            # фасад: наружу торчит только алиас auto — выбор модели живёт в панели
            # (mgpan), а не в клиентах; реальные модели видны в /<backend>/v1/models
            selected = self.server.resolve_auto()
            self._json(200, {
                "object": "list",
                "data": [{
                    "id": AUTO_ALIAS, "object": "model", "owned_by": "model-gate",
                    "max_context_length": 200000,
                    "selected": selected,
                }],
            })
            return
        match = BACKEND_V1_RE.match(self.path)
        if match and match.group(2) == "models":
            name = match.group(1)
            backend = self.server.backends.get(name)
            if backend is None:
                self._json(404, {"error": f"unknown backend: {name}"})
                return
            self.server.refresh_models(only=name)
            self._json(200, {
                "object": "list",
                "data": [_model_entry(backend, model) for model in sorted(backend.models)],
            })
            return
        self._json(404, {"error": "only GET /v1/models, GET /<backend>/v1/models and POST /v1/* are proxied"})

    def do_POST(self) -> None:
        if self.path == "/admin/stop":
            self._stop_backend()
            return
        if self.path == "/admin/cancel":
            self._cancel_request()
            return
        if self.path == "/admin/select":
            self._select_model()
            return
        # /<backend>/v1/* is a per-backend view: strip the prefix, routing is
        # still by model name (the prefix is for discovery, not access control).
        view_backend: str | None = None
        match = BACKEND_V1_RE.match(self.path)
        if match:
            view_backend = match.group(1)
            self.path = f"/v1/{match.group(2)}"
        if not self.path.startswith("/v1/"):
            self._json(404, {"error": "only /v1/* is proxied"})
            return

        started = time.monotonic()
        try:
            body = self._read_body()
            payload = json.loads(body)
            model = payload.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError("JSON field 'model' is required")
            if model == AUTO_ALIAS:
                # единая точка входа Pi: алиас резолвится в модель, выбранную в панели
                resolved = self.server.resolve_auto()
                if not resolved:
                    raise ValueError("no model selected for 'auto' (pick one in mgpan)")
                payload["model"] = resolved
                body = json.dumps(payload, ensure_ascii=False).encode()
                model = resolved
            try:
                backend_name = self.server.router.backend_for(model)
            except ValueError:
                self.server.refresh_models(only=view_backend)
                backend_name = self.server.router.backend_for(model)
            available, reason = self.server.lifecycle_available()
            if not available:
                LOGGER.warning(
                    "request rejected model=%s status=503 reason=%s",
                    model,
                    reason,
                )
                self._json(503, {"error": f"model gate is cooling down: {reason}"})
                return
            lease = self.server.gate.acquire(backend_name, model)
        except (ValueError, json.JSONDecodeError) as error:
            LOGGER.warning("request rejected status=400 error=%s", error)
            self._json(400, {"error": str(error)})
            return

        stream = bool(payload.get("stream", False))
        context_chars = _context_chars(payload)
        backend = self.server.backends[backend_name]
        if stream and self.path == "/v1/chat/completions" and backend.inject_stream_usage:
            # ds4 и omlx отдают точный usage в последнем SSE-чанке только по
            # запросу; просим его сами, клиенты (OpenAI-совместимые) его игнорируют
            options = payload.setdefault("stream_options", {})
            if isinstance(options, dict) and not options.get("include_usage"):
                options["include_usage"] = True
                body = json.dumps(payload, ensure_ascii=False).encode()
        inflight = self.server.stats.start(model, backend_name, stream, context_chars)
        with self.server._active_lock:
            self.server.active_requests[inflight.id] = self
        try:
            if lease.initialize:
                self.server.lifecycle.activate(backend_name, model, lease.sharing_group)
                self.server.processes.ensure_ready(backend_name, model)
                self.server.clear_lifecycle_failure()
                lease.mark_ready()
            status = self._forward(backend, body, inflight)
            self.server.stats.finish(inflight, status=status)
            LOGGER.info(
                "request model=%s backend=%s stream=%s context_chars=%d status=%d elapsed=%.3fs",
                model,
                backend_name,
                stream,
                context_chars,
                status,
                time.monotonic() - started,
            )
        except LifecycleError as error:
            self.server.stats.finish(inflight, status=503, error=str(error))
            self.server.block_lifecycle(str(error))
            LOGGER.error("request model=%s backend=%s status=503 error=%s", model, backend_name, error)
            self._json(503, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            self.server.stats.finish(inflight, status=0, disconnected=True)
            LOGGER.info(
                "request disconnected model=%s backend=%s stream=%s elapsed=%.3fs",
                model,
                backend_name,
                stream,
                time.monotonic() - started,
            )
        except (HTTPError, URLError, OSError) as error:
            self.server.stats.finish(inflight, status=502, error=str(error))
            LOGGER.error("request model=%s backend=%s status=502 error=%s", model, backend_name, error)
            self._json(502, {"error": f"upstream request failed: {error}"})
        except Exception as error:  # keep the proxy fail-closed on malformed upstreams
            self.server.stats.finish(inflight, status=502, error=str(error))
            LOGGER.exception("request model=%s backend=%s status=502", model, backend_name)
            self._json(502, {"error": f"proxy request failed: {error}"})
        finally:
            with self.server._active_lock:
                for key, handler in list(self.server.active_requests.items()):
                    if handler is self:
                        self.server.active_requests.pop(key, None)
            lease.release()

    def _admin_allowed(self) -> bool:
        if self.client_address[0] in {"127.0.0.1", "::1"}:
            return True
        self._json(403, {"error": "admin API is local only"})
        return False

    def cancel(self) -> None:
        # рвём оба конца: клиентское соединение и апстрим — генерация у
        # ds4/omlx оборвётся на ближайшей записи, поток копирования упадёт в
        # штатный except и запрос засчитается как disconnected
        upstream = getattr(self, "_upstream", None)
        if upstream is not None:
            try:
                upstream.close()
            except OSError:
                pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.close_connection = True

    def _select_model(self) -> None:
        if not self._admin_allowed():
            return
        try:
            model = json.loads(self._read_body()).get("model")
            if not isinstance(model, str) or not model:
                raise ValueError("known JSON field 'model' is required")
            self.server.router.backend_for(model)  # неизвестная модель -> ValueError
        except (ValueError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
            return
        self.server.select_model(model)
        LOGGER.info("auto alias now points to model=%s", model)
        self._json(200, {"ok": True, "selected": model})

    def _stop_backend(self) -> None:
        if not self._admin_allowed():
            return
        try:
            backend = json.loads(self._read_body()).get("backend")
            if not isinstance(backend, str) or backend not in self.server.backends:
                raise ValueError("known JSON field 'backend' is required")
        except (ValueError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
            return
        if not self.server.processes.stop(backend):
            self._json(404, {"error": f"no managed process for backend: {backend}"})
            return
        self._json(200, {"ok": True, "backend": backend})

    def _cancel_request(self) -> None:
        if not self._admin_allowed():
            return
        try:
            request_id = int(json.loads(self._read_body()).get("id"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._json(400, {"error": f"JSON field 'id' is required: {error}"})
            return
        with self.server._active_lock:
            handler = self.server.active_requests.get(request_id)
        if handler is None:
            self._json(404, {"error": f"no in-flight request: {request_id}"})
            return
        handler.cancel()
        LOGGER.info("request cancelled id=%d by admin", request_id)
        self._json(200, {"ok": True, "cancelled": request_id})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > self.server.max_body_bytes:
            raise ValueError("invalid or oversized Content-Length")
        return self.rfile.read(length)

    def _forward(self, backend: Backend, body: bytes, inflight: InFlight | None = None) -> int:
        request_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection", "transfer-encoding"}
        }
        request_headers["Content-Length"] = str(len(body))
        request = Request(
            backend.base_url + self.path,
            data=body,
            method="POST",
            headers=request_headers,
        )
        with urlopen(request, timeout=self.server.upstream_timeout) as response:
            self._upstream = response  # для отмены из панели (POST /admin/cancel)
            try:
                return self._relay(response, inflight)
            finally:
                self._upstream = None

    def _relay(self, response, inflight: "InFlight | None") -> int:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            self.send_response(response.status)
            self._copy_headers(response.headers, skip={"content-length", "connection", "transfer-encoding"})
            self.send_header("Content-Length", content_length)
            self.end_headers()
            self._copy_stream(response, inflight)
            return response.status

        self.send_response(response.status)
        self._copy_headers(response.headers, skip={"content-length", "connection", "transfer-encoding"})
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in iter(lambda: response.read1(64 * 1024), b""):
            if inflight:
                inflight.feed(chunk)
            self.wfile.write(f"{len(chunk):X}\r\n".encode())
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        if inflight:
            inflight.parse_usage()
        return response.status

    def _copy_stream(self, response, inflight: InFlight | None = None) -> None:
        for chunk in iter(lambda: response.read1(64 * 1024), b""):
            if inflight:
                inflight.feed(chunk)
            self.wfile.write(chunk)
            self.wfile.flush()
        if inflight:
            inflight.parse_usage()

    def _copy_headers(self, headers, skip: set[str]) -> None:
        for key, value in headers.items():
            if key.lower() not in skip:
                self.send_header(key, value)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(config_path: str | Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = json.loads(Path(config_path).read_text())
    listen = config.get("listen", "127.0.0.1:9000")
    host, port = listen.rsplit(":", 1)
    server = GateServer((host, int(port)), ProxyHandler, config)

    def terminate(signum, frame):
        # SIGTERM должен пройти через finally: иначе stop_all не отработает и
        # ds4 останется сиротой, а новый гейт его лишь "усыновит" без stderr
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    print(f"model-gate listening on http://{listen}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.processes.stop_all()
        server.server_close()
