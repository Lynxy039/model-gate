"""Регрессии аудита: границы HTTP, lifecycle и панель без запуска моделей."""
import io
import tempfile
from pathlib import Path
import threading
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from model_gate import panel
from model_gate.gate import AdmissionGate
from model_gate.control import Controller
from model_gate.proxy import Backend, Lifecycle, LifecycleError, ProcessManager, ProxyHandler
from model_gate.router import ModelRouter
from model_gate.stats import StatsCollector


def handler_for(body=b'{"model":"example"}', path="/v1/chat/completions"):
    handler = object.__new__(ProxyHandler)
    handler.path = path
    handler.client_address = ("127.0.0.1", 1234)
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:9000"
    handler.headers["Content-Type"] = "application/json"
    handler.headers["Content-Length"] = str(len(body))
    handler.rfile = io.BytesIO(body)
    handler._json = Mock()
    handler._forward = Mock(return_value=200)
    handler.server = SimpleNamespace(
        max_body_bytes=1024,
        router=ModelRouter({"test": {"models": ["example"]}}),
        lifecycle_available=Mock(return_value=(True, "")),
        gate=AdmissionGate({}), backends={"test": Backend("test", "http://backend", 1)},
        lifecycle=Mock(), processes=Mock(), clear_lifecycle_failure=Mock(),
        stats=StatsCollector(), active_requests={}, _active_lock=threading.Lock(),
    )
    handler.server.lifecycle.snapshot.return_value = {'test': ['example']}
    handler.server.controller = Controller(handler.server.gate, handler.server.lifecycle,
        handler.server.processes, handler.server.backends, Mock())
    return handler


class RoutingAuditTests(unittest.TestCase):
    def test_explicit_models_are_not_prefixes(self):
        router = ModelRouter({"one": {"models": ["example"]}})
        with self.assertRaises(ValueError):
            router.backend_for("example-not-configured")

    def test_command_only_models_are_routable(self):
        router = ModelRouter({"one": {"model_commands": {"example": ["server"]}}})
        self.assertEqual(router.backend_for("example"), "one")

    def test_backend_concurrency_applies_to_discovered_models(self):
        backend = Backend("test", "http://backend", 4)
        # В GateServer все известные модели должны получать политику backend-а.
        from model_gate.proxy import GateServer
        with patch.object(GateServer, "refresh_models"):
            server = GateServer(("127.0.0.1", 0), ProxyHandler, {
                "backends": {"test": {"base_url": "http://backend", "max_concurrency": 4,
                                       "model_commands": {"example": ["server"]}}},
            })
        try:
            first = server.gate.acquire("test", "example")
            self.assertEqual(first.max_concurrency, backend.max_concurrency)
            first.release()
        finally:
            server.server_close()


class LifecycleAuditTests(unittest.TestCase):
    def test_process_is_stopped_before_loading_a_different_backend(self):
        backends = {
            "ephemeral": Backend("ephemeral", "http://ephemeral", 1,
                                 model_commands={"one": ["server"]}, requires_unload=False),
            "resident": Backend("resident", "http://resident", 1, load_url="http://resident/load"),
        }
        manager = Mock()
        lifecycle = Lifecycle(backends, processes=manager)
        calls = []
        manager.stop.side_effect = lambda name: calls.append(("stop", name)) or True
        lifecycle._post = lambda url: calls.append(("load", url))
        lifecycle.activate("ephemeral", "one")
        lifecycle.activate("resident", "two")
        self.assertEqual(calls, [("stop", "ephemeral"), ("load", "http://resident/load")])

    def test_failed_status_probe_does_not_skip_unload(self):
        backend = Backend("resident", "http://resident", 1, unload_url="http://resident/unload")
        lifecycle = Lifecycle({"resident": backend})
        lifecycle.activate("resident", "one")
        with patch("model_gate.proxy.urlopen", side_effect=URLError("probe failed")):
            with self.assertRaises(LifecycleError):
                lifecycle.activate("resident", "two")
        self.assertEqual(lifecycle.snapshot(), {"resident": ["one"]})

    def test_status_remains_available_while_backend_is_starting(self):
        backend = Backend("test", "http://backend", 1, autostart_command=["server"])
        manager = ProcessManager({"test": backend})
        process = Mock()
        process.poll.return_value = None
        waiting, release, sampled = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def ready(*args):
            calls.append(None)
            if len(calls) == 1:
                return False
            waiting.set()
            release.wait(2)
            return True

        manager._ready = ready
        with patch("model_gate.proxy.subprocess.Popen", return_value=process):
            starter = threading.Thread(target=manager.ensure_ready, args=("test", "example"))
            starter.start()
            self.assertTrue(waiting.wait(1))
            reader = threading.Thread(target=lambda: (manager.snapshot(), sampled.set()))
            reader.start()
            try:
                self.assertTrue(sampled.wait(0.2), "status is blocked by startup polling")
            finally:
                release.set()
                starter.join(2)
                reader.join(2)

    def test_shutdown_waits_for_backend_exit(self):
        backend = Backend("test", "http://backend", 1)
        manager = ProcessManager({"test": backend})
        process = Mock()
        process.poll.return_value = None
        manager._processes["test"] = process
        manager.stop_all()
        process.wait.assert_called()


class HttpAuditTests(unittest.TestCase):
    def test_json_must_be_an_object_for_inference_and_admin(self):
        for path in ("/v1/chat/completions", "/admin/select", "/admin/stop", "/admin/cancel"):
            for body in (b"[]", b"null", b'"hello"'):
                with self.subTest(path=path, body=body):
                    handler = handler_for(body, path)
                    handler.do_POST()
                    self.assertEqual(handler._json.call_args.args[0], 400)
                    self.assertEqual(handler.server.gate.snapshot()["active"], 0)

    def test_browser_cross_origin_admin_request_is_rejected(self):
        handler = handler_for(b'{"backend":"test"}', "/admin/stop")
        handler.headers["Origin"] = "https://evil.example"
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[0], 403)
        handler.server.processes.stop.assert_not_called()

    def test_dns_rebinding_host_cannot_read_admin(self):
        handler = handler_for(path="/admin/stats")
        handler.headers.replace_header("Host", "evil.example:9000")
        handler.do_GET()
        self.assertEqual(handler._json.call_args.args[0], 403)

    def test_allowlisted_tailscale_user_can_read_admin_but_not_write(self):
        handler = handler_for(path="/admin/stats")
        handler.command = "GET"
        handler.headers.replace_header("Host", "gate-device.tailnet.example")
        handler.headers["Tailscale-User-Login"] = "owner@example.com"
        handler.server.tailscale_readonly_users = ["owner@example.com"]
        self.assertTrue(handler._admin_allowed())

        handler.command = "POST"
        self.assertFalse(handler._admin_allowed())
        self.assertEqual(handler._json.call_args.args[0], 403)

    def test_other_tailscale_user_cannot_read_admin(self):
        handler = handler_for(path="/admin/stats")
        handler.command = "GET"
        handler.headers.replace_header("Host", "gate-device.tailnet.example")
        handler.headers["Tailscale-User-Login"] = "shared-user@example.com"
        handler.server.tailscale_readonly_users = ["owner@example.com"]
        self.assertFalse(handler._admin_allowed())
        self.assertEqual(handler._json.call_args.args[0], 403)

    def test_tailnet_identity_header_is_not_trusted_from_non_loopback_peer(self):
        handler = handler_for(path="/admin/stats")
        handler.command = "GET"
        handler.client_address = ("100.64.0.2", 1234)
        handler.headers.replace_header("Host", "gate-device.tailnet.example")
        handler.headers["Tailscale-User-Login"] = "owner@example.com"
        handler.server.tailscale_readonly_users = ["owner@example.com"]
        self.assertFalse(handler._admin_allowed())
        self.assertEqual(handler._json.call_args.args[0], 403)

    def test_truncated_body_is_rejected(self):
        handler = handler_for()
        handler.headers.replace_header("Content-Length", "999")
        with self.assertRaises(ValueError):
            handler._read_body()

    def test_transfer_encoding_is_not_accepted(self):
        handler = handler_for()
        handler.headers["Transfer-Encoding"] = "chunked"
        with self.assertRaises(ValueError):
            handler._read_body()

    def test_error_after_stream_started_does_not_write_second_http_response(self):
        handler = handler_for()
        def fail_after_headers(*args):
            handler._response_started = True
            raise OSError("upstream reset")
        handler._forward.side_effect = fail_after_headers
        handler.do_POST()
        handler._json.assert_not_called()
        self.assertTrue(handler.close_connection)
        self.assertEqual(handler.server.gate.snapshot()["active"], 0)

    def test_upstream_http_errors_keep_their_status_and_body(self):
        handler = handler_for()
        del handler._forward
        handler._relay = Mock(return_value=429)
        handler.server.upstream_timeout = 10
        headers = Message()
        headers["Content-Type"] = "application/json"
        error = HTTPError("http://backend", 429, "busy", headers, io.BytesIO(b'{"error":"busy"}'))
        with patch('model_gate.proxy.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value = error
            self.assertEqual(handler._forward(handler.server.backends['test'], b'{}'), 429)
        self.assertIs(handler._relay.call_args.args[0], error)


class MaintenanceAuditTests(unittest.TestCase):
    def test_clear_kv_stops_server_then_deletes_only_regular_kv_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "prefix.kv").write_bytes(b"cache")
            (directory / "keep.txt").write_text("keep")
            (directory / "link.kv").symlink_to(directory / "keep.txt")
            handler = handler_for(b'{"backend":"test"}', "/admin/clear-kv")
            handler.server.backends["test"].model_commands = {
                "example": ["server", "--kv-disk-dir", tmp],
            }
            handler.server.processes.snapshot.return_value = {"test": {"model": "example"}}
            handler.server.processes.stop.side_effect = lambda _: (directory / "prefix.kv").exists()
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args, (200, {"ok": True, "removed": 1, "freed_bytes": 5}))
            self.assertFalse((directory / "prefix.kv").exists())
            self.assertTrue((directory / "keep.txt").exists())
            self.assertTrue((directory / "link.kv").is_symlink())
            handler.server.lifecycle.forget.assert_called_once_with("test")

    def test_stop_refuses_to_race_with_active_inference(self):
        handler = handler_for(b'{"backend":"test"}', "/admin/stop")
        lease = handler.server.gate.acquire("test", "example")
        try:
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[0], 409)
            handler.server.processes.stop.assert_not_called()
        finally:
            lease.release()

    def test_queue_has_a_deadline_and_cannot_enter_during_maintenance(self):
        gate = AdmissionGate({})
        with gate.maintenance():
            with self.assertRaises(TimeoutError):
                gate.acquire("test", "example", timeout=0)
        self.assertEqual(gate.snapshot()["waiting"], 0)
        lease = gate.acquire("test", "example", timeout=0)
        lease.release()

    def test_clear_kv_panel_uses_gate_api_not_local_filesystem(self):
        actions = panel.Actions("http://gate", "http://omlx")
        calls = []
        actions._run = lambda label, call: call()
        with patch("model_gate.panel.post_json", side_effect=lambda url, payload, **kwargs:
                   calls.append((url, payload)) or {"removed": 2, "freed_bytes": 42}), \
             patch('model_gate.panel.Path.unlink', side_effect=AssertionError('local filesystem must not be used')):
            actions.clear_kv("test", "example")
        self.assertEqual(calls[0], ("http://gate/admin/clear-kv", {"backend": "test"}))
        self.assertEqual(calls[1][1]["model"], "local-auto")


class PanelAuditTests(unittest.TestCase):
    def test_compact_header_shows_gate_connection_failure(self):
        state = panel.Poller("http://gate", "http://omlx", False).state
        state["gate_error"] = "connection refused"
        text = "\n".join(panel.flatten(line) for line in panel.compact_header_section(state, 60))
        self.assertIn("NOT RESPONDING", text)

    def test_remote_text_cannot_inject_terminal_controls(self):
        stdout = io.StringIO()
        with patch("model_gate.panel.sys.stdout", stdout):
            panel.Tty().paint([[("model\x1b]52;c;secret\x07\nname", "fg")]], "ok")
        self.assertNotIn("\x1b]52", stdout.getvalue())
        self.assertNotIn("\x07", stdout.getvalue())

    def test_key_decoder_keeps_batched_keys_and_focus_events(self):
        terminal = panel.Tty()
        terminal.fd = 42
        with patch("model_gate.panel.select.select", return_value=([42], [], [])), \
             patch("model_gate.panel.os.read", return_value=b"\x1b[I\x1b[BT"):
            self.assertEqual(terminal.key(0), "FOCUS_IN")
            self.assertEqual(terminal.key(0), "DOWN")
            self.assertEqual(terminal.key(0), "T")

    def test_key_decoder_keeps_a_fragmented_escape_sequence(self):
        terminal = panel.Tty()
        terminal.fd = 42
        with patch("model_gate.panel.select.select", return_value=([42], [], [])), \
             patch("model_gate.panel.os.read", side_effect=[b"\x1b[", b"A"]):
            self.assertIsNone(terminal.key(0))
            self.assertEqual(terminal.key(0), "UP")

    def test_remote_panel_uses_gate_host_telemetry_not_client_machine(self):
        poller = panel.Poller("https://gate-device.tailnet.example", "", False, remote=True)
        host = {'kind': 'host', 'sys': {'mem_total': 100, 'mem_used': 70, 'mem_frac': .7},
                'cpu': 23.0, 'gpu_pct': 44.0, 'gpu_temp': 61.0, 'sys_watts': 38.0}
        with patch('model_gate.panel.fetch_json', side_effect=[{}, {}, {'_host': host}]), \
                patch('model_gate.panel.read_vm_stat', side_effect=AssertionError('must use gate host')), \
                patch('model_gate.panel.read_cpu_busy', side_effect=AssertionError('must use gate host')):
            poller._sample(0)
        self.assertEqual(poller.state['sys'], host['sys'])
        self.assertEqual(poller.state['cpu'], 23.0)
        self.assertEqual(poller.state['gpu_temp'], 61.0)

    def test_three_backends_fit_inside_their_outer_box(self):
        state = panel.Poller("http://gate", "http://omlx", False).state
        state["gate_status"] = {"backends": [{"name": name} for name in ("a", "b", "c")]}
        lines = panel.backends_section(state, 110)
        # Внутренние боксы не должны обрезаться внешним box().
        self.assertTrue(any(panel.flatten(line).count("╭") == 3 for line in lines))


if __name__ == "__main__":
    unittest.main()
