import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_gate.config import ConfigError, load_config, migrate, normalize, schema, validate


class ConfigTests(unittest.TestCase):
    def test_legacy_config_migrates_without_mutating_original(self):
        legacy = {'listen': '127.0.0.1:9000', 'backends': {
            'ds4': {'base_url': 'http://127.0.0.1:8003', 'models': ['flash'],
                    'model_commands': {'flash': ['ds4-server', '-m', '/models/f.gguf']},
                    'max_concurrency': 1, 'ready_path': '/health'},
            'omlx': {'base_url': 'http://127.0.0.1:8001', 'models': ['coder'], 'load_url': '/load',
                     'unload_url': '/unload/{model}', 'discover_models': True}}}
        result = migrate(legacy)
        self.assertNotIn('type', legacy['backends']['ds4'])
        self.assertEqual(result['backends']['ds4']['type'], 'process')
        self.assertEqual(result['backends']['ds4']['port'], 8003)
        self.assertEqual(result['backends']['omlx']['type'], 'omlx')

    def test_config_normalizes_ds4_auto_port_and_private_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{}')
            raw = {'version': 1, 'tailscale_readonly_users': ['owner@example.com'],
                'backends': {'ds4': {'type': 'ds4', 'binary': 'ds4-server',
                'port': 'auto', 'models': {'flash': {'path': 'models/f.gguf', 'context': 200000}}}}}
            result = normalize(raw, path)
            self.assertEqual(result['tailscale_readonly_users'], ['owner@example.com'])
            backend = result['backends']['ds4']
            self.assertEqual(backend['base_url'], 'http://127.0.0.1:0')
            self.assertTrue(backend['auto_port'])
            self.assertEqual(backend['model_commands']['flash'][backend['model_commands']['flash'].index('--port') + 1], '{port}')
            self.assertTrue(Path(backend['model_commands']['flash'][backend['model_commands']['flash'].index('--kv-disk-dir') + 1]).is_absolute())

    def test_omlx_resident_base_url_and_load_hooks_are_normalized(self):
        result = normalize({'version': 1, 'backends': {'omlx': {'type': 'omlx', 'base_url': 'http://127.0.0.1:8001',
            'models': [], 'load_url': '/admin/load/{model}', 'unload_url': '/admin/unload/{model}'}}})
        backend = result['backends']['omlx']
        self.assertEqual(backend['load_url'], 'http://127.0.0.1:8001/admin/load/{model}')
        self.assertTrue(backend['discover_models'])

    def test_unknown_keys_and_missing_models_fail_with_paths(self):
        for raw, expected in [({'version': 1, 'surprise': True, 'backends': {}}, '$.surprise'),
                              ({'version': 1, 'backends': {'ds4': {'type': 'ds4'}}}, 'binary')]:
            with self.subTest(expected=expected), self.assertRaises(ConfigError) as error:
                normalize(raw)
            self.assertIn(expected, str(error.exception))

    def test_non_loopback_gate_and_backend_credentials_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, '127.0.0.1'):
            normalize({'version': 1, 'listen': '0.0.0.0:9000', 'backends': {'x': {'type': 'http', 'base_url': 'http://127.0.0.1:1', 'models': []}}})
        with self.assertRaisesRegex(ConfigError, 'without credentials'):
            normalize({'version': 1, 'backends': {'x': {'type': 'http', 'base_url': 'http://user:pass@host', 'models': []}}})

    def test_custom_process_supports_auto_port_and_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = {'version': 1, 'backends': {'own': {'type': 'process', 'port': 'auto', 'working_dir': tmp,
                'command': ['server', '--port', '{port}'], 'models': ['model']}}}
            backend = normalize(raw, Path(tmp) / 'config.json')['backends']['own']
            self.assertTrue(backend['auto_port'])
            self.assertEqual(backend['working_dir'], tmp)

    def test_legacy_wildcard_gate_and_backend_bind_are_made_loopback_only(self):
        raw = {'listen': '0.0.0.0:9000', 'backends': {'ds4': {
            'base_url': 'http://100.64.1.2:8003', 'models': ['m'],
            'model_commands': {'m': ['ds4-server', '--host', '0.0.0.0', '--port', '8003']}}}}
        result = normalize(raw)
        self.assertEqual(result['listen'], '127.0.0.1:9000')
        self.assertEqual(result['backends']['ds4']['port'], 8003)
        self.assertEqual(result['backends']['ds4']['model_commands']['m'][2], '127.0.0.1')
        self.assertEqual(raw['listen'], '0.0.0.0:9000')

    def test_duplicate_model_ids_across_backends_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, 'unique'):
            normalize({'version': 1, 'backends': {
                'one': {'type': 'http', 'base_url': 'http://127.0.0.1:1', 'models': ['same']},
                'two': {'type': 'http', 'base_url': 'http://127.0.0.1:2', 'models': ['same']}}})

    def test_custom_process_requires_a_routable_model(self):
        with self.assertRaisesRegex(ConfigError, 'configured model'):
            normalize({'version': 1, 'backends': {'own': {
                'type': 'process', 'port': 9001, 'command': ['server']}}})

    def test_published_schema_rejects_unknown_setting_at_nested_path(self):
        with self.assertRaisesRegex(ConfigError, 'mystery'):
            validate({'version': 1, 'backends': {'x': {'type': 'http', 'base_url': 'http://localhost:1',
                    'models': [], 'mystery': 2}}}, schema())
