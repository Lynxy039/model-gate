import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from model_gate import launcher


class LauncherTests(unittest.TestCase):
    def test_stop_uses_graceful_gate_api_not_pid_or_ps(self):
        with patch.object(launcher, 'gate_alive', return_value=True), \
             patch.object(launcher, 'post') as post, \
             patch.object(launcher.subprocess, 'run') as ps:
            launcher.stop_gate('http://127.0.0.1:9000')
        post.assert_called_once_with('http://127.0.0.1:9000/admin/shutdown', {'restart': False})
        ps.assert_not_called()

    def test_start_uses_private_per_config_log_and_no_shared_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'; config.write_text('{}')
            process = Mock()
            log = Path(tmp) / 'logs' / 'gate.log'
            paths = Mock(log=log)
            with patch.object(launcher, 'Paths', return_value=paths), \
                 patch.object(launcher.subprocess, 'Popen', return_value=process) as spawn:
                self.assertIs(launcher.start_gate(config), process)
            args = spawn.call_args.args[0]
            self.assertIn('serve', args)
            self.assertEqual(spawn.call_args.kwargs['cwd'], Path.home())
            self.assertTrue(log.exists())
            self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)

    def test_restart_waits_for_new_gate_boot_id(self):
        with patch.object(launcher, 'gate_health', side_effect=[{'boot_id': 'old'}, {'boot_id': 'new'}]):
            launcher.wait_restarted('http://127.0.0.1:9000', 'old', timeout=1)

    def test_distinct_config_paths_have_distinct_state_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a.json', Path(tmp) / 'b.json'
            self.assertNotEqual(launcher.Paths(a).state, launcher.Paths(b).state)
            self.assertNotEqual(launcher.Paths(a).log, launcher.Paths(b).log)

    def test_gate_url_reads_versioned_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text('{"version":1,"listen":"127.0.0.1:9123","backends":{"x":{"type":"http","base_url":"http://127.0.0.1:1","models":["x"]}}}')
            self.assertEqual(launcher.gate_url_from_config(config), 'http://127.0.0.1:9123')
