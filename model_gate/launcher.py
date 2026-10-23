"""Per-config process-independent launcher for the local gate and TUI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .paths import Paths, default_config, instance_lock, private_dir


def gate_health(url: str):
    try:
        with urlopen(Request(f'{url}/healthz'), timeout=2) as response:
            payload = json.loads(response.read())
            return payload if response.status == 200 and payload.get('ok') is True else None
    except (URLError, OSError, ValueError, AttributeError):
        return None


def gate_alive(url: str) -> bool:
    return gate_health(url) is not None


def gate_url_from_config(config: Path) -> str:
    from .config import load_config
    host, port = load_config(config).get('listen', '127.0.0.1:9000').rsplit(':', 1)
    return f'http://{host}:{port}'


def post(url, payload):
    request = Request(url, data=json.dumps(payload).encode(), method='POST', headers={
        'Content-Type': 'application/json', 'Host': urlsplit(url).netloc,
    })
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read()) if response.status != 204 else {}


def stop_gate(url: str, restart=False) -> None:
    if gate_alive(url): post(f'{url}/admin/shutdown', {'restart': restart})


def start_gate(config: Path) -> subprocess.Popen:
    paths = Paths(config.resolve())
    private_dir(paths.log.parent)
    fd = os.open(paths.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab', buffering=0) as log:
        return subprocess.Popen([sys.executable, '-m', 'model_gate', 'serve', '--config', str(config.resolve())],
            cwd=Path.home(), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def wait_down(url: str, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not gate_alive(url): return
        time.sleep(.25)
    raise RuntimeError(f'gate did not stop within {timeout}s')


def wait_ready(url: str, process=None, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gate_alive(url): return
        if process is not None and process.poll() is not None:
            raise RuntimeError(f'gate exited {process.returncode}; inspect configured log')
        time.sleep(.25)
    raise RuntimeError(f'gate did not become ready within {timeout}s')


def wait_restarted(url: str, previous_boot_id: str, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = gate_health(url)
        if health and health.get('boot_id') != previous_boot_id: return
        time.sleep(.25)
    raise RuntimeError(f'gate did not restart within {timeout}s')


def launch(args, config: Path, url: str):
    paths = Paths(config.resolve())
    with instance_lock(paths.state / 'mgrun.lock'):
        if args.stop:
            if gate_alive(url):
                stop_gate(url)
                wait_down(url)
            return
        if args.restart_gate and gate_alive(url):
            previous = gate_health(url).get('boot_id')
            stop_gate(url, restart=True)
            wait_restarted(url, previous)
            return
        if not gate_alive(url):
            print(f'starting model-gate ({config})…')
            wait_ready(url, start_gate(config))
        print(f'model-gate ready at {url}')


def main():
    parser = argparse.ArgumentParser(description='model-gate + terminal panel')
    parser.add_argument('--config', default=str(default_config()))
    parser.add_argument('--gpu', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--restart-gate', action='store_true')
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    config = Path(args.config).expanduser().resolve()
    if not config.exists():
        from .__main__ import setup
        setup(config)
    url = gate_url_from_config(config)
    launch(args, config, url)
    if args.stop or args.restart_gate: return
    panel = shutil.which('mgpan') or 'mgpan'
    sys.argv = [panel, '--url', url, '--config', str(config), '--gpu' if args.gpu else '--no-gpu']
    if args.compact: sys.argv.append('--compact')
    from .panel import main as panel_main
    panel_main()
