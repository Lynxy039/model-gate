"""Server-side model operations. UI and CLI never touch backend processes/files."""
from collections import deque
from pathlib import Path
import threading
import time

from .proxy import LifecycleError


class Controller:
    def __init__(self, gate, lifecycle, processes, backends, cancel_all):
        self.gate, self.lifecycle, self.processes = gate, lifecycle, processes
        self.backends, self.cancel_all = backends, cancel_all
        self._operation = threading.RLock()
        self._lock = threading.Lock()
        self._states = {name: {'phase': 'unloaded', 'model': None, 'error': None} for name in backends}
        self._events = deque(maxlen=100)

    def _state(self, name, phase, model=None, error=None):
        with self._lock:
            self._states[name] = {'phase': phase, 'model': model, 'error': error}
            self._events.append({'ts': time.time(), 'backend': name, 'phase': phase, 'model': model, 'error': error})

    def snapshot(self):
        with self._lock:
            return {'states': {k: dict(v) for k, v in self._states.items()}, 'events': list(self._events)}

    def activate(self, name, model, cancelled=lambda: False):
        with self._operation:
            if self.gate.snapshot()['halted'] or cancelled():
                raise LifecycleError('activation cancelled: gate halted or request disconnected')
            self._state(name, 'loading', model)
            try:
                self.lifecycle.activate(name, model)
                self.processes.ensure_ready(name, model, cancelled=cancelled)
                if cancelled() or self.gate.snapshot()['halted']:
                    raise LifecycleError('activation cancelled')
            except Exception as error:
                self._state(name, 'error', model, str(error))
                raise
            loaded = self.lifecycle.snapshot()
            for other in self.backends:
                if other != name and not loaded[other]:
                    self._state(other, 'unloaded')
            self._state(name, 'ready', model)

    def _stop(self, name):
        self._state(name, 'unloading')
        try:
            backend = self.backends[name]
            if backend.model_commands or backend.autostart_command:
                if not self.processes.stop(name) and self.processes.is_ready(name):
                    raise LifecycleError(f'{name} is not owned by gate; refusing to stop an external process')
                self.lifecycle.forget(name)
            else:
                self.lifecycle.unload_backend(name)
            self._state(name, 'unloaded')
        except Exception as error:
            self._state(name, 'error', error=str(error))
            raise

    def stop(self, name):
        with self.gate.maintenance(), self._operation:
            self._stop(name)
        return {'ok': True, 'backend': name}

    def clear_kv(self, name):
        with self.gate.maintenance(), self._operation:
            process = self.processes.snapshot().get(name)
            if not process:
                raise ValueError('KV cleanup requires a managed running backend')
            command = self.backends[name].command_for(process['model']) or []
            if '--kv-disk-dir' not in command:
                raise ValueError('backend has no --kv-disk-dir')
            path = Path(command[command.index('--kv-disk-dir') + 1])
            if not path.is_absolute() or not path.is_dir():
                raise ValueError('KV directory must be an existing absolute path')
            self._stop(name)
            removed = freed = 0
            for entry in path.iterdir():
                if entry.suffix == '.kv' and entry.is_file() and not entry.is_symlink():
                    freed += entry.stat().st_size
                    entry.unlink()
                    removed += 1
            return {'ok': True, 'removed': removed, 'freed_bytes': freed}

    def halt(self):
        # Admission closes BEFORE waiting on a slow load/unload operation.
        self.gate.halt()
        self.cancel_all()
        errors = {}
        with self._operation:
            for name in self.backends:
                try:
                    self._stop(name)
                except Exception as error:
                    errors[name] = str(error)
        return {'ok': not errors, 'halted': True, 'errors': errors}

    def resume(self):
        with self._operation:
            self.gate.resume()
        return {'ok': True, 'halted': False}
