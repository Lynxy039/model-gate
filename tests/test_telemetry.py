import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from model_gate.proxy import Backend
from model_gate.telemetry import collect


class TelemetryTests(unittest.TestCase):
    def test_panel_telemetry_is_collected_inside_gate_per_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / 'a.kv').write_bytes(b'abc')
            (cache / 'ignored.kv').symlink_to(cache / 'a.kv')
            ds4 = Backend('ds4', 'http://127.0.0.1:1234', 1,
                model_commands={'m': ['ds4-server', '-m', '/m.gguf', '--kv-disk-dir', tmp, '--kv-disk-space-mb', '9']}, kind='ds4')
            omlx = Backend('omlx', 'http://127.0.0.1:8001', 1, kind='omlx')
            open_url = MagicMock()
            open_url.return_value.__enter__.return_value.read.return_value = json.dumps({'uptime': 42}).encode()
            host = {'kind': 'host', 'sys': {'mem_frac': 0.5}}
            with patch('model_gate.telemetry.subprocess.run', return_value=SimpleNamespace(stdout='8')) as ps, \
                    patch('model_gate.telemetry.collect_host', return_value=host):
                result = collect({'ds4': ds4, 'omlx': omlx},
                    {'ds4': {'pid': 10, 'model': 'm'}, 'omlx': {'pid': 11, 'model': 'x'}}, open_url)
            self.assertEqual(result['_host'], host)
            self.assertEqual(result['ds4']['kv_disk']['bytes'], 3)
            self.assertEqual(result['ds4']['kv_disk']['cap'], 9 * 1024**2)
            self.assertEqual(result['ds4']['rss_bytes'], 8192)
            self.assertEqual(result['omlx']['metrics'], {'uptime': 42})
            self.assertEqual(result['omlx']['rss_bytes'], 8192)
            self.assertEqual(open_url.call_count, 1)
            self.assertEqual(ps.call_count, 2)

    def test_unavailable_process_and_omlx_metrics_are_nonfatal(self):
        b = Backend('omlx', 'http://127.0.0.1:8001', 1, kind='omlx')
        def offline(*args, **kwargs): raise OSError('offline')
        with patch('model_gate.telemetry.collect_host', return_value={'kind': 'host'}):
            self.assertEqual(collect({'omlx': b}, {}, offline), {
                'omlx': {'kind': 'omlx', 'metrics': None, 'kv_disk': None, 'rss_bytes': None},
                '_host': {'kind': 'host'}})
