"""Versioned configuration, JSON Schema and normalization into runtime options."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import shutil

from .paths import Paths, default_config


class ConfigError(ValueError):
    pass


STRING = {'type': 'string', 'minLength': 1}
STRINGS = {'type': 'array', 'items': STRING}
POSITIVE = {'type': 'integer', 'minimum': 1}
SECONDS = {'type': 'number', 'minimum': 0}
PORT = {'oneOf': [{'enum': ['auto']}, {'type': 'integer', 'minimum': 1, 'maximum': 65535}]}
MODEL = {'type': 'object', 'additionalProperties': False, 'required': ['path'], 'properties': {
    'path': STRING, 'vision': STRING, 'context': POSITIVE, 'mtp': {'type': 'boolean'},
    'dspark_model': STRING, 'args': STRINGS,
}}
COMMON = {
    'max_concurrency': POSITIVE, 'prefixes': STRINGS, 'discover_models': {'type': 'boolean'},
    'model_list_path': STRING, 'load_url': STRING, 'unload_url': STRING, 'unload_all_url': STRING,
    'requires_unload': {'type': 'boolean'}, 'ready_path': STRING,
    'model_status_path': {'type': ['string', 'null']}, 'inject_stream_usage': {'type': 'boolean'},
    'startup_timeout': {'type': 'number', 'exclusiveMinimum': 0},
    'startup_poll_seconds': {'type': 'number', 'exclusiveMinimum': 0},
    'unload_timeout': {'type': 'number', 'exclusiveMinimum': 0},
    'settle_seconds': SECONDS, 'unload_settle_seconds': SECONDS, 'load_settle_seconds': SECONDS,
    'model_policies': {'type': 'object', 'additionalProperties': {
        'type': 'object', 'required': ['max_concurrency'], 'additionalProperties': False,
        'properties': {'max_concurrency': POSITIVE}}},
}
PROCESS = {'port': PORT, 'working_dir': STRING, 'env': {'type': 'object', 'additionalProperties': {'type': 'string'}},
           'command': STRINGS, 'model_commands': {'type': 'object', 'additionalProperties': STRINGS}}
ROOT = {'version': {'enum': [1]}, 'listen': STRING, 'auto_model': STRING,
        'memory_profile_gb': POSITIVE, 'require_unload': {'type': 'boolean'},
        'lifecycle_timeout': {'type': 'number', 'exclusiveMinimum': 0},
        'upstream_timeout': {'type': 'number', 'exclusiveMinimum': 0},
        'queue_timeout': {'type': 'number', 'exclusiveMinimum': 0},
        'lifecycle_failure_cooldown': SECONDS, 'max_body_bytes': POSITIVE,
        'model_discovery_timeout': {'type': 'number', 'exclusiveMinimum': 0},
        'tailscale_readonly_users': STRINGS}


def schema():
    variants = []
    for kind in ('ds4', 'omlx', 'process', 'http'):
        props = {**COMMON, 'type': {'enum': [kind]}}
        required = ['type']
        if kind in ('ds4', 'process'):
            props.update(PROCESS)
        if kind == 'ds4':
            props.update({'binary': STRING, 'models': {'type': 'object', 'minProperties': 1,
                          'additionalProperties': MODEL}, 'args': STRINGS,
                          'kv_disk_dir': STRING, 'kv_disk_space_mb': POSITIVE})
            required += ['binary', 'models']
        else:
            props['models'] = STRINGS
        if kind in ('omlx', 'http'):
            props['base_url'] = STRING
            required += ['base_url']
        variants.append({'type': 'object', 'required': required, 'properties': props, 'additionalProperties': False})
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': 'model-gate configuration v1', 'type': 'object',
            'required': ['version', 'backends'], 'additionalProperties': False,
            'properties': {**ROOT, '$schema': STRING, 'backends': {'type': 'object', 'minProperties': 1,
                           'additionalProperties': {'oneOf': variants}}}}


def validate(value, rule, path='$'):
    """Validate the small schema vocabulary we publish; no runtime dependency."""
    if 'oneOf' in rule:
        choices = rule['oneOf']
        if isinstance(value, dict) and 'type' in value:
            selected = [s for s in choices if value['type'] in s.get('properties', {}).get('type', {}).get('enum', [])]
            choices = selected or choices
        errors = []
        for choice in choices:
            try:
                validate(value, choice, path)
                return
            except ConfigError as error:
                errors.append(str(error))
        raise ConfigError(errors[0] if len(errors) == 1 else f'{path}: does not match a supported configuration variant')
    if 'enum' in rule and value not in rule['enum']:
        raise ConfigError(f"{path}: expected one of {rule['enum']}")
    types = rule.get('type', [])
    types = [types] if isinstance(types, str) else types
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
               'string': isinstance(value, str), 'integer': type(value) is int,
               'number': type(value) in (int, float) and math.isfinite(value),
               'boolean': type(value) is bool, 'null': value is None}
    if types and not any(matches[t] for t in types):
        raise ConfigError(f'{path}: expected {" or ".join(types)}')
    if isinstance(value, dict):
        for name in rule.get('required', []):
            if name not in value:
                raise ConfigError(f'{path}.{name}: required')
        if len(value) < rule.get('minProperties', 0):
            raise ConfigError(f'{path}: cannot be empty')
        for name, item in value.items():
            item_rule = rule.get('properties', {}).get(name, rule.get('additionalProperties', True))
            if item_rule is False:
                raise ConfigError(f'{path}.{name}: unknown setting')
            if isinstance(item_rule, dict):
                validate(item, item_rule, f'{path}.{name}')
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate(item, rule.get('items', {}), f'{path}[{index}]')
    if isinstance(value, str) and len(value) < rule.get('minLength', 0):
        raise ConfigError(f'{path}: cannot be empty')
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise ConfigError(f'{path}: must be finite')
        if value < rule.get('minimum', value) or value > rule.get('maximum', value):
            raise ConfigError(f'{path}: outside allowed range')
        if 'exclusiveMinimum' in rule and value <= rule['exclusiveMinimum']:
            raise ConfigError(f'{path}: must be greater than {rule["exclusiveMinimum"]}')


def read_config(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f'{path}: {error}') from error
    if not isinstance(data, dict):
        raise ConfigError('configuration must be a JSON object')
    return data


def migrate(data):
    """Legacy config -> explicit process/http backends; never rewrite in place."""
    result = copy.deepcopy(data)
    result['version'] = 1
    if result.get('listen', '').startswith('0.0.0.0:'):
        # The legacy default exposed an unauthenticated inference API to every interface.
        result['listen'] = '127.0.0.1:' + result['listen'].rsplit(':', 1)[1]
    for name, backend in result.get('backends', {}).items():
        backend.pop('stop_on_proxy_exit', None)
        backend['type'] = ('process' if backend.get('model_commands') or backend.get('autostart_command')
                           else 'omlx' if name == 'omlx' else 'http')
        if 'autostart_command' in backend:
            backend['command'] = backend.pop('autostart_command')
        if backend['type'] == 'process':
            from urllib.parse import urlsplit
            backend['port'] = urlsplit(backend.pop('base_url')).port or 80
            for command in [*backend.get('model_commands', {}).values(), backend.get('command') or []]:
                for index, arg in enumerate(command[:-1]):
                    if arg == '--host': command[index + 1] = '127.0.0.1'
        for policy in backend.get('model_policies', {}).values():
            policy.pop('sharing_group', None)
    return result


def normalize(data, path=None):
    path = Path(path or default_config()).expanduser().resolve()
    migrated = 'version' not in data
    data = migrate(data) if migrated else copy.deepcopy(data)
    validate(data, schema())
    result = {key: value for key, value in data.items() if key not in ('$schema', 'backends')}
    result['_config_path'] = str(path)
    if migrated:
        result['_warnings'] = ['legacy config loaded in memory; run `model-gate setup` to create a new v1 config']
    result['backends'] = {}
    for name, source in data['backends'].items():
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ConfigError(f'backends.{name}: use letters, digits, underscore or dash')
        kind = source['type']
        b = {key: value for key, value in source.items() if key not in ('type', 'binary', 'args', 'kv_disk_dir', 'kv_disk_space_mb')}
        b['kind'] = kind
        def file(value):
            p = Path(value).expanduser()
            return str(p if p.is_absolute() else (path.parent / p).resolve())
        if kind in ('ds4', 'process'):
            if kind == 'process' and not source.get('model_commands') and not source.get('models'):
                raise ConfigError(f'backends.{name}: command requires at least one configured model')
            port = source.get('port', 'auto')
            b['auto_port'] = port == 'auto'
            b['base_url'] = f'http://127.0.0.1:{0 if port == "auto" else port}'
            b['requires_unload'] = False
            b['model_status_path'] = None
            if source.get('working_dir'):
                b['working_dir'] = file(source['working_dir'])
            if kind == 'process':
                if not source.get('command') and not source.get('model_commands'):
                    raise ConfigError(f'backends.{name}: command or model_commands is required')
                if b['auto_port'] and not all('{port}' in ' '.join(cmd) for cmd in
                        list(source.get('model_commands', {}).values()) + ([source['command']] if source.get('command') else [])):
                    raise ConfigError(f'backends.{name}: auto port requires {{port}} in every command')
                b['autostart_command'] = b.pop('command', None)
            else:
                binary = source['binary']
                binary = file(binary) if '/' in binary else (shutil.which(binary) or binary)
                cache = file(source['kv_disk_dir']) if source.get('kv_disk_dir') else str(Paths(path).cache / name)
                b['model_commands'] = {}
                for model, settings in source['models'].items():
                    command = [binary]
                    if source.get('working_dir'):
                        command += ['--chdir', file(source['working_dir'])]
                    command += ['-m', file(settings['path']), '--host', '127.0.0.1', '--port', '{port}',
                                '--ctx', str(settings.get('context', 200000)),
                                '--kv-disk-dir', cache, '--kv-disk-space-mb', str(source.get('kv_disk_space_mb', 65536))]
                    if settings.get('vision'):
                        command += ['--vision', file(settings['vision'])]
                    if settings.get('mtp'):
                        command += ['--mtp']
                    if settings.get('dspark_model'):
                        command += ['--dspark', '--mtp-model', file(settings['dspark_model'])]
                    command += source.get('args', []) + settings.get('args', [])
                    b['model_commands'][model] = command
                b['models'] = list(source['models'])
        else:
            from urllib.parse import urlsplit
            url = urlsplit(b['base_url'])
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                raise ConfigError(f'backends.{name}.base_url: expected an HTTP(S) URL without credentials')
            b['base_url'] = b['base_url'].rstrip('/')
            for key in ('load_url', 'unload_url', 'unload_all_url'):
                if b.get(key, '').startswith('/'):
                    b[key] = b['base_url'] + b[key]
            if kind == 'omlx':
                b.setdefault('discover_models', True)
                b.setdefault('load_url', b['base_url'] + '/admin/api/models/{model}/load')
                b.setdefault('unload_url', b['base_url'] + '/admin/api/models/{model}/unload')
                b.setdefault('model_status_path', '/v1/models/status')
        result['backends'][name] = b
    known = set()
    for name, backend in result['backends'].items():
        candidates = set(backend.get('models', [])) | set(backend.get('model_commands', {}))
        duplicates = known & candidates
        if duplicates:
            raise ConfigError(f'backend model ids must be unique: {sorted(duplicates)}')
        known |= candidates
    if result.get('auto_model') and result['auto_model'] not in known:
        raise ConfigError(f'auto_model is not configured by any backend: {result["auto_model"]}')
    listen = result.get('listen', '127.0.0.1:9000')
    try:
        host, port = listen.rsplit(':', 1)
        if host not in ('127.0.0.1', 'localhost') or not 1 <= int(port) <= 65535:
            raise ValueError()
    except ValueError:
        raise ConfigError('listen: use 127.0.0.1:PORT (local macOS service)') from None
    return result


def load_config(path):
    return normalize(read_config(path), path)
