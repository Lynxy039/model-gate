import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

loader = importlib.machinery.SourceFileLoader("mgrun_under_test", str(Path(__file__).resolve().parents[1] / "mgrun"))
spec = importlib.util.spec_from_loader(loader.name, loader)
mgrun = importlib.util.module_from_spec(spec)
loader.exec_module(mgrun)


class LauncherAuditTests(unittest.TestCase):
    def test_stale_pid_cannot_kill_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pid"
            path.write_text("12345")
            with patch.object(mgrun, "PID_FILE", path), \
                 patch.object(mgrun.subprocess, "run", return_value=SimpleNamespace(stdout="editor")), \
                 patch.object(mgrun.os, "kill") as kill:
                with self.assertRaises(SystemExit):
                    mgrun.stop_gate("http://127.0.0.1:9000")
                kill.assert_not_called()

    def test_invalid_pid_cannot_signal_a_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pid"
            path.write_text("-1")
            with patch.object(mgrun, "PID_FILE", path), patch.object(mgrun.os, "kill") as kill:
                with self.assertRaises(SystemExit):
                    mgrun.stop_gate("http://127.0.0.1:9000")
                kill.assert_not_called()

    def test_start_closes_parent_log_fd_and_atomically_replaces_pid_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            victim = directory / "keep"
            victim.write_text("keep")
            pid = directory / "pid"
            pid.symlink_to(victim)
            process = Mock(pid=12345)
            with patch.object(mgrun, "REPO", directory), patch.object(mgrun, "PID_FILE", pid), \
                 patch.object(mgrun.subprocess, "Popen", return_value=process) as popen:
                mgrun.start_gate(directory / "config.json")
            self.assertEqual(victim.read_text(), "keep")
            self.assertFalse(pid.is_symlink())
            self.assertEqual(pid.read_text(), "12345")
            self.assertTrue(popen.call_args.kwargs["stdout"].closed)
