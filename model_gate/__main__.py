from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import socket
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen

from .config import ConfigError, load_config, schema
from .paths import Paths, atomic_write, default_config
from .proxy import serve


def _check(config_path, network=True):
    config = load_config(config_path)
    issues = []
    host, port = config.get('listen', '127.0.0.1:9000').rsplit(':', 1)
    try:
        with socket.socket() as sock:
            sock.bind((host, int(port)))
    except OSError as error:
        try:
            with urlopen(f'http://{host}:{port}/healthz', timeout=.5) as response:
                health = json.loads(response.read())
            if response.status != 200 or health.get('instance_id') != Paths(Path(config['_config_path'])).identity:
                raise ValueError('another service owns the port')
        except (OSError, ValueError, KeyError):
            issues.append(f'gate {host}:{port}: {error}')
    for name, backend in config['backends'].items():
        if backend.get('kind') == 'ds4':
            for model, command in backend['model_commands'].items():
                for key in ('-m', '--vision', '--mtp-model', '--kv-disk-dir'):
                    if key in command:
                        index = command.index(key) + 1
                        if index < len(command) and key != '--kv-disk-dir' and not Path(command[index]).exists():
                            issues.append(f'{name}/{model}: {key} path not found: {command[index]}')
                binary = command[0]
                if not Path(binary).is_file() and not shutil.which(binary):
                    issues.append(f'{name}: executable not found: {binary}')
        elif backend.get('kind') == 'process':
            for command in [backend.get('autostart_command') or [], *backend.get('model_commands', {}).values()]:
                if command and not Path(command[0]).is_file() and not shutil.which(command[0]):
                    issues.append(f'{name}: executable not found: {command[0]}')
        if network and backend.get('kind') in ('omlx', 'http'):
            try:
                url = urlsplit(backend['base_url'])
                with socket.create_connection((url.hostname, url.port or (443 if url.scheme == 'https' else 80)), timeout=1):
                    pass
            except (OSError, ValueError, TypeError):
                issues.append(f'{name}: {backend["base_url"]} is not accepting connections')
    return config, issues


def doctor(config_path):
    config, issues = _check(config_path)
    for warning in config.get('_warnings', []): print(f'WARNING: {warning}')
    if config.get('memory_profile_gb') and sys.platform == 'darwin':
        try:
            total = int(__import__('subprocess').check_output(['sysctl', '-n', 'hw.memsize'], text=True).strip())
            available_gb = total // 1024**3
            if available_gb < config['memory_profile_gb']:
                issues.append(f'memory profile {config["memory_profile_gb"]} GiB exceeds machine RAM {available_gb} GiB (advisory profile)')
        except (OSError, ValueError): pass
    if issues:
        print('Configuration valid; diagnostics found:')
        for issue in issues: print(f'  ! {issue}')
        return 1
    print(f'OK: {config_path}; {len(config["backends"])} backend(s); gate={config.get("listen", "127.0.0.1:9000")}')
    return 0


def setup(config_path):
    config_path = Path(config_path).expanduser()
    if config_path.exists():
        raise ConfigError(f'{config_path} already exists; edit it directly or choose a new --config path')
    if not sys.stdin.isatty():
        raise ConfigError('setup requires an interactive terminal; edit the versioned JSON config manually')
    print('model-gate setup — DS4 + oMLX (all inference services stay on loopback)')
    listen = input('Gate listen [127.0.0.1:9000]: ').strip() or '127.0.0.1:9000'
    backends = {}
    ds4 = input('Add DS4 backend? [Y/n]: ').strip().lower() not in ('n', 'no')
    if ds4:
        binary = input('DS4 server binary [ds4-server]: ').strip() or 'ds4-server'
        model_id = input('Model ID for clients: ').strip()
        model_path = input('GGUF model path: ').strip()
        context = int(input('Context tokens [200000]: ').strip() or '200000')
        backends['ds4'] = {'type': 'ds4', 'binary': binary, 'port': 'auto',
                           'models': {model_id: {'path': model_path, 'context': context}}}
    omlx = input('Add oMLX backend? [Y/n]: ').strip().lower() not in ('n', 'no')
    if omlx:
        base_url = input('oMLX HTTP base URL [http://127.0.0.1:8001]: ').strip() or 'http://127.0.0.1:8001'
        backends['omlx'] = {'type': 'omlx', 'base_url': base_url, 'discover_models': True,
                            'load_url': '/admin/api/models/{model}/load',
                            'unload_url': '/admin/api/models/{model}/unload', 'model_status_path': '/v1/models/status'}
    if not backends: raise ConfigError('at least one backend is required')
    data = {'$schema': './model-gate.schema.json', 'version': 1, 'listen': listen,
            'memory_profile_gb': 128, 'require_unload': True, 'backends': backends}
    from .config import normalize
    normalized = normalize(data, config_path)
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write(Path(config_path), json.dumps(data, indent=2) + '\n')
    schema_path = Path(config_path).with_name('model-gate.schema.json')
    atomic_write(schema_path, json.dumps(schema(), indent=2) + '\n')
    print(f'Wrote {config_path}; discovered internal config: {normalized["_config_path"]}')
    return doctor(config_path)


def main() -> None:
    parser = argparse.ArgumentParser(description='macOS model controller for DS4 and oMLX')
    sub = parser.add_subparsers(dest='action')
    start = sub.add_parser('serve', help='start the local model gate')
    start.add_argument('--config', default=str(default_config()))
    check = sub.add_parser('doctor', help='validate config and inspect backend readiness')
    check.add_argument('--config', default=str(default_config()))
    wizard = sub.add_parser('setup', help='create config using an interactive wizard')
    wizard.add_argument('--config', default=str(default_config()))
    # Preserve the original `python -m model_gate --config FILE` launch syntax.
    parser.add_argument('--config', default=str(default_config()))
    args = parser.parse_args()
    try:
        if args.action == 'doctor': raise SystemExit(doctor(args.config))
        if args.action == 'setup': raise SystemExit(setup(args.config))
        serve(args.config)
    except ConfigError as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
