import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_gate import __main__ as cli
from model_gate.config import ConfigError


class CliTests(unittest.TestCase):
    def test_setup_requires_tty(self):
        with patch.object(cli.sys, 'stdin', io.StringIO('')):
            with self.assertRaisesRegex(ConfigError, 'interactive terminal'):
                cli.setup(Path('/tmp/no-config'))

    def test_setup_refuses_to_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'config.json'
            target.write_text('{"keep": true}')
            with self.assertRaisesRegex(ConfigError, 'already exists'):
                cli.setup(target)
            self.assertEqual(json.loads(target.read_text()), {'keep': True})

    def test_setup_writes_typed_config_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'config.json'
            answers = iter(['', 'n', 'y', 'http://127.0.0.1:8123'])
            tty = io.StringIO('')
            with patch.object(cli.sys, 'stdin', tty), patch('builtins.input', side_effect=lambda _: next(answers)), \
                 patch.object(cli, 'doctor', return_value=0):
                tty.isatty = lambda: True
                self.assertEqual(cli.setup(target), 0)
            config = json.loads(target.read_text())
            schema = json.loads((target.parent / 'model-gate.schema.json').read_text())
            self.assertEqual(config['version'], 1)
            self.assertEqual(config['backends']['omlx']['type'], 'omlx')
            self.assertEqual(config['$schema'], './model-gate.schema.json')
            self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
            self.assertEqual((target.stat().st_mode & 0o777), 0o600)

    def test_doctor_reports_memory_profile_as_advisory_on_macos(self):
        with patch.object(cli, '_check', return_value=({'_warnings': [], 'backends': {},
                 'listen': '127.0.0.1:9000', 'memory_profile_gb': 128}, [])), \
             patch.object(cli.sys, 'platform', 'darwin'), \
             patch('subprocess.check_output', return_value='68719476736'):
            with patch('builtins.print') as output:
                self.assertEqual(cli.doctor('unused'), 1)
            self.assertTrue(any('advisory profile' in str(call) for call in output.call_args_list))
