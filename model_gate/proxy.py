"""OpenAI-compatible reverse proxy with model-class admission control."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .gate import AdmissionGate, AdmissionPolicy, GateHalted, RequestCancelled
from .paths import Paths, atomic_write, default_config, instance_lock, private_dir
from .router import ModelRouter
from .stats import InFlight, StatsCollector


LOGGER = logging.getLogger(__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # не пересылаем промпт/Authorization на сторонний URL


urlopen = build_opener(NoRedirect()).open
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}

BACKEND_V1_RE = re.compile(r"^/([^/]+)/v1/(.*)$")
# алиас единой точки входа: Pi шлёт model="local-auto", гейт подставляет
# выбранную в панели модель
AUTO_ALIAS = "local-auto"


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
    inject_stream_usage: bool = True
    kind: str = 'http'
    auto_port: bool = False
    working_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    unload_timeout: float = 120

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
            inject_stream_usage=bool(config.get("inject_stream_usage", True)),
            kind=config.get('kind', 'omlx' if name == 'omlx' else 'http'), auto_port=bool(config.get('auto_port', False)),
            working_dir=config.get('working_dir'), env=config.get('env', {}),
            unload_timeout=float(config.get('unload_timeout', 120)),
        )

    def admin_url(self, template: str, model: str | None = None) -> str:
        return template.replace("{model}", quote(model or "", safe=""))

    def command_for(self, model: str) -> list[str] | None:
        return self.model_commands.get(model, self.autostart_command)

    def admission_policy(self, model: str) -> AdmissionPolicy:
        config = self.model_policies.get(model, {})
        return AdmissionPolicy(max_concurrency=int(config.get("max_concurrency", self.max_concurrency)))


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
        self._closing = False
        self._last_snapshot = {}

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

    def ensure_ready(self, backend_name: str, model: str | None = None, cancelled=lambda: False) -> None:
        backend = self.backends[backend_name]
        command = backend.command_for(model) if model is not None else backend.autostart_command
        if not command:
            return
        template = command
        with self._lock:
            if self._closing:
                raise LifecycleError("gate is shutting down")
            restarting = False
            process = self._processes.get(backend_name)
            if backend.auto_port and (process is None or process.poll() is not None):
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    backend.base_url = f'http://127.0.0.1:{probe.getsockname()[1]}'
            port = str(urlsplit(backend.base_url).port or 80)
            command = [part.replace('{port}', port) for part in template]
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
                raise LifecycleError(f'{backend_name} is running outside gate; stop it before using managed commands')
            if process is None:
                try:
                    if '--kv-disk-dir' in command:
                        private_dir(Path(command[command.index('--kv-disk-dir') + 1]))
                    options = {}
                    if backend.working_dir:
                        options['cwd'] = backend.working_dir
                    if backend.env:
                        options['env'] = {**os.environ, **backend.env}
                    process = subprocess.Popen(
                        command,
                        **options,
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

        # Медленное ожидание readiness не должно блокировать snapshot панели.
        deadline = time.monotonic() + backend.startup_timeout
        while time.monotonic() < deadline:
            if self._closing or cancelled():
                self.stop(backend_name)
                raise LifecycleError("backend startup cancelled")
            if process.poll() is not None:
                self.stop(backend_name)
                raise LifecycleError(f'{backend_name} exited during startup with code {process.returncode}')
            if self._ready(backend):
                if backend.auto_port and sys.platform == 'darwin':
                    owned = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(process.pid),
                                            '-iTCP:' + port, '-sTCP:LISTEN'],
                                           capture_output=True, text=True, timeout=3)
                    if owned.returncode != 0:
                        self.stop(backend_name)
                        raise LifecycleError('auto port is not owned by the launched process')
                return
            time.sleep(backend.startup_poll_seconds)
        self.stop(backend_name)
        raise LifecycleError(
            f"{backend_name} did not become ready within {backend.startup_timeout:g}s"
        )

    def _stop(self, process: subprocess.Popen, timeout=120) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def snapshot(self) -> dict[str, dict[str, object]]:
        if not self._lock.acquire(blocking=False):
            return dict(self._last_snapshot)
        try:
            self._last_snapshot = {
                name: {'running': True, 'pid': process.pid, 'model': self._models.get(name),
                       'started_ts': self._started.get(name), 'base_url': self.backends[name].base_url}
                for name, process in self._processes.items() if process.poll() is None
            }
            return dict(self._last_snapshot)
        finally:
            self._lock.release()

    def stop(self, backend_name: str) -> bool:
        with self._lock:
            process = self._processes.get(backend_name)
            if process is None:
                return False
            # До фактического выхода процесса запрещаем ensure_ready запускать новый.
            self._stop(process, self.backends[backend_name].unload_timeout)
            self._processes.pop(backend_name, None)
            self._started.pop(backend_name, None)
            self._commands.pop(backend_name, None)
            self._models.pop(backend_name, None)
            return True

    def stop_all(self) -> None:
        with self._lock:
            self._closing = True
            processes = [
                (name, process)
                for name, process in self._processes.items()
            ]
        for name, _ in processes:
            self.stop(name)

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
        processes: ProcessManager | None = None,
    ):
        self.backends = backends
        self.processes = processes
        self.require_unload = require_unload
        self.timeout = timeout
        self.sleep = sleep_fn
        self._models: dict[str, set[str]] = {name: set() for name in backends}
        self._lock = threading.Lock()

    def activate(self, target: str, model: str) -> None:
        """Сделать доступной одну модель; всё остальное выгружается.

        Одновременной работы нескольких моделей гейт не допускает (см.
        AdmissionGate), поэтому выгружать надо не «конкурентов по группе»,
        а любой ранее загруженный лиз.
        """
        with self._lock:
            unload = [
                (backend_name, loaded_model)
                for backend_name, models in self._models.items()
                for loaded_model in models
                if (backend_name, loaded_model) != (target, model)
            ]
            self._unload(unload, target)
            backend = self.backends[target]
            if model in self._models[target] and (not backend.load_url or self._is_loaded(backend, model)):
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
            entries = status["models"]
            if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
                raise ValueError("invalid model status response")
            return any(entry.get("id") == model and entry.get("loaded") for entry in entries)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, AttributeError, TypeError) as error:
            raise LifecycleError(f"cannot verify loaded model on {backend.name}: {error}") from error

    def _unload(self, entries: list[tuple[str, str]], target: str) -> None:
        by_backend: dict[str, list[str]] = {}
        for backend, model in entries:
            by_backend.setdefault(backend, []).append(model)
        for backend_name, models in by_backend.items():
            backend = self.backends[backend_name]
            if backend.settle_seconds:
                self.sleep(backend.settle_seconds)
            unloaded = False
            if backend.autostart_command or backend.model_commands:
                if self.processes is None:
                    raise LifecycleError(f"no process manager for {backend_name}; refusing switch")
                stopped = self.processes.stop(backend_name)
                if not stopped and self.processes.is_ready(backend_name):
                    raise LifecycleError(f"cannot stop unmanaged backend {backend_name}; refusing switch")
                unloaded = True
            elif backend.unload_all_url:
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

    def unload_backend(self, backend: str) -> None:
        with self._lock:
            self._unload([(backend, model) for model in self._models[backend]], backend)

    def forget(self, backend: str) -> None:
        with self._lock:
            self._models[backend].clear()

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
            for model in set(backend.models) | set(backend.model_policies) | set(backend.model_commands)
        })
        self.stats = StatsCollector()
        self.processes = ProcessManager(self.backends, log_sink=self.stats.record_backend_log)
        self.lifecycle = Lifecycle(
            self.backends,
            processes=self.processes,
            require_unload=config.get("require_unload", True),
            timeout=float(config.get("lifecycle_timeout", 30)),
        )
        # модель по умолчанию для алиаса local-auto: выбор из панели (POST /admin/select)
        # переживает рестарт гейта через файл, иначе — auto_model из конфига
        self._selected_lock = threading.Lock()
        self.paths = Paths(Path(config.get('_config_path', default_config())))
        self.boot_id = uuid.uuid4().hex
        self._selected_file = self.paths.state / 'selected'
        self.restart_requested = False
        self.selected = self._load_selected() or str(config.get("auto_model", "")) or None
        if self.selected:
            try:
                self.router.backend_for(self.selected)
            except ValueError:
                self.selected = str(config.get("auto_model", "")) or None
        self._last_discovery = 0.0
        self.max_body_bytes = int(config.get("max_body_bytes", 64 * 1024 * 1024))
        self.tailscale_readonly_users = {user.strip().casefold() for user in config.get('tailscale_readonly_users', [])}
        self.upstream_timeout = float(config.get("upstream_timeout", 600))
        self.queue_timeout = float(config.get("queue_timeout", 600))
        self.failure_cooldown = float(config.get("lifecycle_failure_cooldown", 5))
        self.state_lock = threading.Lock()
        # живые запросы для отмены из панели: id InFlight -> активный handler
        self.active_requests: dict[int, "ProxyHandler"] = {}
        self._active_lock = threading.Lock()
        self._blocked_until = 0.0
        self._blocked_reason = ""
        from .control import Controller
        self.controller = Controller(self.gate, self.lifecycle, self.processes, self.backends, self.cancel_all)
        super().__init__(address, handler)
        self.refresh_models()

    def resolve_auto(self) -> str:
        with self._selected_lock:
            return self.selected or ""

    def auto_context(self) -> int:
        """Реальный контекст выбранной модели: метаданные discovery, для ds4 —
        --ctx из команды запуска; неизвестно — 200000 как в конфиге Pi."""
        model = self.resolve_auto()
        try:
            backend = self.backends[self.router.backend_for(model)]
        except (KeyError, ValueError):
            return 200000
        meta = backend.model_meta.get(model, {})
        for key in ("max_model_len", "context_length", "max_context_length", "context"):
            value = meta.get(key)
            if isinstance(value, int) and value > 0:
                return value
        command = backend.command_for(model) or []
        if "--ctx" in command:
            try:
                return int(command[command.index("--ctx") + 1])
            except (IndexError, ValueError):
                pass
        return 200000

    def select_model(self, model: str) -> None:
        with self._selected_lock:
            self.selected = model
            try:
                atomic_write(self._selected_file, model)
            except OSError:
                pass  # персистентность — удобство, не обязательное условие

    def _load_selected(self) -> str:
        try:
            return self._selected_file.read_text().strip()
        except OSError:
            return ""

    def refresh_models(self, only: str | None = None) -> None:
        # shortcut: discovery последовательный; его фоновый запуск не тормозит панель,
        # но явный GET /<backend>/v1/models ждёт свой сетевой timeout.
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
            threading.Thread(target=self.refresh_models, daemon=True).start()
        admission = self.gate.snapshot()
        processes = self.processes.snapshot()
        control = self.controller.snapshot()
        active = admission["models"]
        backends = []
        for name, backend in self.backends.items():
            process = processes.get(name)
            models = sorted(set(backend.models) | set(backend.model_commands) | set(backend.model_policies))
            backends.append({
                "name": name,
                "ready": self.processes.is_ready(name, timeout=0.2),
                'process': process, 'kind': backend.kind, 'base_url': backend.base_url,
                **control['states'][name],
                'capabilities': {'stop': bool(backend.command_for('') or backend.model_commands or backend.unload_url or backend.unload_all_url),
                                 'clear_kv': any('--kv-disk-dir' in cmd for cmd in [*backend.model_commands.values(), backend.autostart_command or []])},
                "models": [{
                    "id": model,
                    "active": active.get(f"{name}/{model}", 0),
                    'loaded': control['states'][name]['phase'] == 'ready' and control['states'][name]['model'] == model,
                    "running": bool(process and process["model"] == model),
                } for model in models],
            })
        return {**admission, 'selected': self.selected, 'backends': backends, 'events': control['events']}

    def cancel_all(self):
        with self._active_lock:
            requests = list(self.active_requests.values())
        for handler in requests:
            handler.cancel()

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

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)  # заголовки и тело не должны держать поток бесконечно
        self._response_started = False

    def do_GET(self) -> None:
        if self.path == "/admin/status":
            if self._admin_allowed():
                self._json(200, self.server.admin_status())
            return
        if self.path == '/admin/telemetry':
            if self._admin_allowed():
                from .telemetry import collect
                self._json(200, collect(self.server.backends, self.server.processes.snapshot(), urlopen))
            return
        if self.path == "/admin/stats":
            if self._admin_allowed():
                self._json(200, self.server.stats.snapshot())
            return
        if self.path == "/healthz":
            self._json(200, {'ok': True, 'instance_id': self.server.paths.identity,
                             'boot_id': self.server.boot_id, **self.server.gate.snapshot()})
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
                    "max_context_length": self.server.auto_context(),
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
        self._response_started = False
        if self.path == "/admin/stop":
            self._stop_backend()
            return
        if self.path == "/admin/cancel":
            self._cancel_request()
            return
        if self.path == "/admin/select":
            self._select_model()
            return
        if self.path == '/admin/clear-kv':
            self._clear_kv()
            return
        if self.path in ('/admin/halt', '/admin/resume', '/admin/shutdown'):
            self._control_action()
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
            payload = self._read_json()
            body = json.dumps(payload, ensure_ascii=False).encode()
            model = payload.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError("JSON field 'model' is required")
            if model == AUTO_ALIAS:
                # единая точка входа Pi: алиас резолвится в модель, выбранную в панели
                resolved = self.server.resolve_auto()
                if not resolved:
                    raise ValueError(f"no model selected for '{AUTO_ALIAS}' (pick one in mgpan)")
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
            backend = self.server.backends[backend_name]

        except TimeoutError as error:
            self._json(503, {"error": str(error)})
            return
        except ValueError as error:
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
        lease = None
        watch_done = threading.Event()
        watcher = None
        if isinstance(getattr(self, 'connection', None), socket.socket):
            watcher = threading.Thread(target=self._watch_disconnect, args=(watch_done,), daemon=True)
            watcher.start()
        try:
            lease = self.server.gate.acquire(backend_name, model, backend.admission_policy(model),
                timeout=getattr(self.server, 'queue_timeout', 600), request_id=inflight.id,
                cancelled=self._peer_gone)
            inflight.admit()
            if getattr(self, "_cancelled", False):
                raise ConnectionResetError("request cancelled")
            if lease.initialize:
                inflight.phase = 'loading'
                self.server.controller.activate(backend_name, model, cancelled=self._peer_gone)
                self.server.clear_lifecycle_failure()
                lease.mark_ready()
            if getattr(self, "_cancelled", False):
                raise ConnectionResetError("request cancelled during startup")
            inflight.phase = 'prefill'
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
        except (RequestCancelled, BrokenPipeError, ConnectionResetError):
            self.server.stats.finish(inflight, status=0, disconnected=True)
            self.close_connection = True
        except GateHalted as error:
            self.server.stats.finish(inflight, status=503, error=str(error))
            self._json(503, {'error': str(error)})
        except TimeoutError as error:
            if lease is None:
                self.server.stats.finish(inflight, status=503, error=str(error))
                self._json(503, {'error': str(error)})
            else:
                self.server.stats.finish(inflight, status=502, error=str(error))
                self._proxy_error(f'upstream timed out: {error}')
        except LifecycleError as error:
            if self._peer_gone():
                self.server.stats.finish(inflight, status=0, disconnected=True)
                return
            self.server.stats.finish(inflight, status=503, error=str(error))
            self.server.block_lifecycle(str(error))
            LOGGER.error("request model=%s backend=%s status=503 error=%s", model, backend_name, error)
            self._json(503, {"error": str(error)})
        except (HTTPError, URLError, OSError) as error:
            if getattr(self, "_cancelled", False):
                self.server.stats.finish(inflight, status=0, disconnected=True)
                return
            self.server.stats.finish(inflight, status=502, error=str(error))
            LOGGER.error("request model=%s backend=%s status=502 error=%s", model, backend_name, error)
            self._proxy_error(f"upstream request failed: {error}")
        except Exception as error:  # keep the proxy fail-closed on malformed upstreams
            if getattr(self, "_cancelled", False):
                self.server.stats.finish(inflight, status=0, disconnected=True)
                return
            self.server.stats.finish(inflight, status=502, error=str(error))
            LOGGER.exception("request model=%s backend=%s status=502", model, backend_name)
            self._proxy_error(f"proxy request failed: {error}")
        finally:
            watch_done.set()
            if watcher is not None:
                watcher.join(timeout=.5)
            with self.server._active_lock:
                for key, handler in list(self.server.active_requests.items()):
                    if handler is self:
                        self.server.active_requests.pop(key, None)
            if lease is not None:
                lease.release()

    def _watch_disconnect(self, done):
        while not done.wait(.1):
            if self._peer_gone():
                self.cancel()
                return

    def _peer_gone(self):
        if getattr(self, '_cancelled', False):
            return True
        connection = getattr(self, 'connection', None)
        if connection is None:
            return False
        try:
            if select.select([connection], [], [], 0)[0]:
                return connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b''
        except (OSError, ValueError):
            return True
        return False

    def _control_action(self):
        if not self._admin_allowed():
            return
        try:
            payload = self._read_json()
            if self.path == '/admin/halt':
                self._json(200, self.server.controller.halt())
            elif self.path == '/admin/resume':
                self._json(200, self.server.controller.resume())
            else:
                restart = payload.get('restart', False)
                if type(restart) is not bool:
                    raise ValueError('restart must be boolean')
                self._json(200, {'ok': True, 'restart': restart})
                self.server.restart_requested = restart
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        except ValueError as error:
            self._json(400, {'error': str(error)})

    def _proxy_error(self, message: str) -> None:
        if getattr(self, "_response_started", False):
            self.close_connection = True
        else:
            self._json(502, {"error": message})

    def _admin_allowed(self) -> bool:
        headers = getattr(self, "headers", {})
        try:
            host = urlsplit("//" + headers.get("Host", "localhost")).hostname
        except ValueError:
            host = None
        loopback_peer = self.client_address[0] in {"127.0.0.1", "::1"}
        if (loopback_peer and host in {"127.0.0.1", "::1", "localhost"}
                and not headers.get("Origin")
                and headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"}):
            return True
        tailscale_user = headers.get('Tailscale-User-Login', '').strip().casefold()
        if (loopback_peer and getattr(self, 'command', '') == 'GET' and tailscale_user
                and tailscale_user in getattr(self.server, 'tailscale_readonly_users', ())
                and not headers.get('Origin')
                and headers.get('Sec-Fetch-Site') not in {'cross-site', 'same-site'}):
            return True
        self._json(403, {"error": "admin API is local only"})
        return False

    def cancel(self) -> None:
        # рвём оба конца: клиентское соединение и апстрим — генерация у
        # ds4/omlx оборвётся на ближайшей записи, поток копирования упадёт в
        # штатный except и запрос засчитается как disconnected
        self._cancelled = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection = getattr(self, '_upstream_connection', None)
        upstream_socket = getattr(connection, 'sock', None)
        if upstream_socket is not None:
            try:
                upstream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        upstream = getattr(self, "_upstream", None)
        if upstream is not None:
            # close() может ждать блокирующий read1 в другом потоке: сначала shutdown.
            try:
                upstream.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            # Сам response закрывает только владеющий им поток в _forward:
            # одновременный close/read1 повреждает внутреннее состояние HTTPResponse.
        self.close_connection = True

    def _select_model(self) -> None:
        if not self._admin_allowed():
            return
        try:
            model = self._read_json().get("model")
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
            backend = self._read_json().get("backend")
            if not isinstance(backend, str) or backend not in self.server.backends:
                raise ValueError("known JSON field 'backend' is required")
        except (ValueError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
            return
        try:
            self._json(200, self.server.controller.stop(backend))
        except RuntimeError as error:
            self._json(409, {'error': str(error)})

    def _cancel_request(self) -> None:
        if not self._admin_allowed():
            return
        try:
            request_id = int(self._read_json().get("id"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._json(400, {"error": f"JSON field 'id' is required: {error}"})
            return
        self.server.gate.cancel(request_id)
        with self.server._active_lock:
            handler = self.server.active_requests.get(request_id)
        if handler is None:
            self._json(404, {"error": f"no in-flight request: {request_id}"})
            return
        handler.cancel()
        LOGGER.info("request cancelled id=%d by admin", request_id)
        self._json(200, {"ok": True, "cancelled": request_id})

    def _clear_kv(self) -> None:
        if not self._admin_allowed():
            return
        try:
            name = self._read_json().get("backend")
            if not isinstance(name, str) or name not in self.server.backends:
                raise ValueError("known JSON field 'backend' is required")
            self._json(200, self.server.controller.clear_kv(name))
        except (ValueError, IndexError) as error:
            self._json(400, {"error": str(error)})
        except RuntimeError as error:
            self._json(409, {"error": str(error)})
        except OSError as error:
            self._json(500, {"error": f"KV cleanup failed: {error}"})

    def _read_json(self) -> dict:
        body = self._read_body()
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ValueError("browser-origin requests are not supported")
        content_type = self.headers.get("Content-Type", "application/json").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > self.server.max_body_bytes:
            raise ValueError("invalid or oversized Content-Length")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("incomplete request body")
        return body

    def _forward(self, backend: Backend, body: bytes, inflight: InFlight | None = None) -> int:
        skip = HOP_HEADERS | {"host", "content-length"}
        skip |= {token.strip().lower() for token in self.headers.get("Connection", "").split(",")}
        request_headers = {key: value for key, value in self.headers.items() if key.lower() not in skip}
        request_headers["Content-Length"] = str(len(body))
        url = urlsplit(backend.base_url + self.path)
        cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        connection = cls(url.hostname, url.port, timeout=min(5, self.server.upstream_timeout))
        self._upstream_connection = connection
        try:
            connection.connect()
            connection.sock.settimeout(self.server.upstream_timeout)
            if self._peer_gone():
                raise RequestCancelled('client disconnected')
            connection.request('POST', url.path + ('?' + url.query if url.query else ''), body, request_headers)
            response = connection.getresponse()
            self._upstream = response
            with response:
                if self._peer_gone():
                    raise RequestCancelled('client disconnected')
                return self._relay(response, inflight)
        finally:
            self._upstream = None
            self._upstream_connection = None
            connection.close()

    def _relay(self, response, inflight: "InFlight | None") -> int:
        self._response_started = True
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            self.send_response(response.status)
            self._copy_headers(response.headers, skip={"content-length", "connection", "transfer-encoding"})
            self.send_header("Content-Length", content_length)
            self.end_headers()
            self._copy_stream(response, inflight)
            remaining = getattr(response, "length", None)
            if isinstance(remaining, int) and remaining > 0:
                raise OSError("incomplete upstream response")
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
        skip = skip | HOP_HEADERS | {token.strip().lower() for token in headers.get("Connection", "").split(",")}
        for key, value in headers.items():
            if key.lower() not in skip:
                self.send_header(key, value)

    def _json(self, status: int, payload: dict) -> None:
        self.close_connection = True
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
    from .config import load_config
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    config = load_config(config_path)
    for warning in config.get('_warnings', []): LOGGER.warning(warning)
    paths = Paths(Path(config['_config_path']))
    with instance_lock(paths.state / 'serve.lock', blocking=False):
        host, port = config.get('listen', '127.0.0.1:9000').rsplit(':', 1)
        server = GateServer((host, int(port)), ProxyHandler, config)
        LOGGER.info('listening on %s:%s instance=%s', host, port, paths.identity)
        def terminate(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        try:
            server.serve_forever()
        finally:
            server.controller.halt()
            server.processes.stop_all()
            server.server_close()
        restart = server.restart_requested
    if restart:
        os.execv(sys.executable, [sys.executable, '-m', 'model_gate', 'serve', '--config', str(paths.config)])
