import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

from model_gate.gate import AdmissionGate, AdmissionPolicy
from model_gate import panel
from model_gate.panel import Actions
from model_gate.proxy import Backend, GateServer, Lifecycle, LifecycleError, ProcessManager, ProxyHandler
from model_gate.router import ModelRouter
from model_gate.stats import StatsCollector


class AdmissionGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = AdmissionGate({
            ("omlx", "coder"): AdmissionPolicy(2),
            ("omlx", "thinking"): AdmissionPolicy(2),
            ("ds4", "flash"): AdmissionPolicy(),
        })

    def test_same_model_uses_its_own_concurrency(self):
        first = self.gate.acquire("omlx", "coder")
        first.mark_ready()
        second = self.gate.acquire("omlx", "coder")

        self.assertEqual(self.gate.snapshot()["active"], 2)
        first.release()
        second.release()

    def test_another_model_waits_for_every_lease(self):
        coder = self.gate.acquire("omlx", "coder")
        coder.mark_ready()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.gate.acquire("omlx", "thinking")))
        thread.start()
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.gate.snapshot()["waiting"], 1)

        coder.release()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.gate.snapshot()["waiting"], 0)
        result[0].release()

    def test_another_backend_waits_too(self):
        first = self.gate.acquire("omlx", "coder")
        first.mark_ready()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.gate.acquire("ds4", "flash")))
        thread.start()
        self.assertTrue(thread.is_alive())

        first.release()
        thread.join(timeout=1)

        self.assertEqual(len(result), 1)
        result[0].release()

    def test_ds4_is_limited_to_one(self):
        first = self.gate.acquire("ds4", "flash")
        first.mark_ready()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.gate.acquire("ds4", "flash")))
        thread.start()
        self.assertTrue(thread.is_alive())
        first.release()
        thread.join(timeout=1)
        self.assertEqual(len(result), 1)
        result[0].release()


class ProxyModelDiscoveryTests(unittest.TestCase):
    def test_refresh_models_adds_models_returned_by_backend(self):
        server = object.__new__(GateServer)
        server.backends = {
            "omlx": Backend("omlx", "http://omlx", 1, discover_models=True),
        }
        server._model_refresh_lock = threading.Lock()
        server.model_discovery_timeout = 1
        server.router = ModelRouter({})
        response = Mock()
        response.read.return_value = b'{"data":[{"id":"new-model"}]}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with patch("model_gate.proxy.urlopen", return_value=response):
            server.refresh_models()

        self.assertEqual(server.backends["omlx"].models, ["new-model"])
        self.assertEqual(server.router.backend_for("new-model"), "omlx")

    def test_model_list_exposes_only_auto_facade(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/v1/models"
        handler._json = Mock()
        handler.server = SimpleNamespace(resolve_auto=Mock(return_value="glm5.3-flash"),
                                         auto_context=Mock(return_value=262144))

        ProxyHandler.do_GET(handler)

        data = handler._json.call_args[0][1]["data"]
        self.assertEqual([m["id"] for m in data], ["local-auto"])
        self.assertEqual(data[0]["selected"], "glm5.3-flash")
        self.assertEqual(data[0]["max_context_length"], 262144)

    def test_refresh_models_stores_metadata_and_respects_only(self):
        server = object.__new__(GateServer)
        server.backends = {
            "omlx": Backend("omlx", "http://omlx", 1, discover_models=True),
            "ds4": Backend("ds4", "http://ds4", 1, discover_models=True),
        }
        server._model_refresh_lock = threading.Lock()
        server.model_discovery_timeout = 1
        server.router = ModelRouter({})
        response = Mock()
        response.read.return_value = b'{"data":[{"id":"new-model","max_model_len":262144,"created":1}]}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with patch("model_gate.proxy.urlopen", return_value=response) as mock_urlopen:
            server.refresh_models(only="omlx")

        self.assertEqual(server.backends["omlx"].models, ["new-model"])
        self.assertEqual(
            server.backends["omlx"].model_meta["new-model"],
            {"max_model_len": 262144, "created": 1},
        )
        self.assertEqual(server.backends["ds4"].models, [])
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_backend_view_lists_only_that_backend_with_metadata(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/omlx/v1/models"
        handler._json = Mock()
        handler.server = SimpleNamespace(
            refresh_models=Mock(),
            backends={
                "omlx": Backend(
                    "omlx", "http://omlx", 1, models=["coder"],
                    model_meta={"coder": {"max_model_len": 262144}},
                ),
                "ds4": Backend("ds4", "http://ds4", 1, models=["flash"]),
            },
        )

        ProxyHandler.do_GET(handler)

        handler.server.refresh_models.assert_called_once_with(only="omlx")
        handler._json.assert_called_once_with(200, {
            "object": "list",
            "data": [
                {"max_model_len": 262144, "id": "coder", "object": "model", "owned_by": "omlx"},
            ],
        })

    def test_post_backend_view_strips_prefix_before_forward(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/omlx/v1/chat/completions"
        handler.headers = {"Content-Length": "2"}
        handler._read_body = Mock(return_value=b'{"model":"coder"}')
        handler._forward = Mock(return_value=200)
        handler._json = Mock()
        lease = Mock(initialize=False)
        handler.server = SimpleNamespace(
            router=SimpleNamespace(backend_for=Mock(return_value="omlx")),
            lifecycle_available=Mock(return_value=(True, "")),
            gate=SimpleNamespace(acquire=Mock(return_value=lease)),
            backends={"omlx": Backend("omlx", "http://omlx", 1)},
            refresh_models=Mock(),
            stats=StatsCollector(),
            active_requests={}, _active_lock=threading.Lock(),
        )

        ProxyHandler.do_POST(handler)

        self.assertEqual(handler.path, "/v1/chat/completions")
        args = handler._forward.call_args.args
        self.assertIs(args[0], handler.server.backends["omlx"])
        self.assertEqual(json.loads(args[1]), {"model": "coder"})
        handler._json.assert_not_called()


class ProxyAdminTests(unittest.TestCase):
    def test_stop_endpoint_stops_local_backend(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/admin/stop"
        handler.client_address = ("127.0.0.1", 1234)
        handler._read_body = Mock(return_value=b'{"backend":"ds4"}')
        handler._json = Mock()
        handler.server = SimpleNamespace(
            backends={'ds4': Backend('ds4', 'http://127.0.0.1:8003', 1, model_commands={'example': ['server']})},
            gate=AdmissionGate({}), lifecycle=SimpleNamespace(forget=Mock()),
            processes=SimpleNamespace(stop=Mock(return_value=True)),
        )
        from model_gate.control import Controller
        handler.server.controller = Controller(handler.server.gate, handler.server.lifecycle,
            handler.server.processes, handler.server.backends, Mock())

        ProxyHandler.do_POST(handler)

        handler.server.processes.stop.assert_called_once_with("ds4")
        handler._json.assert_called_once_with(200, {"ok": True, "backend": "ds4"})

    def test_select_endpoint_points_auto_alias_at_known_model(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/admin/select"
        handler.client_address = ("127.0.0.1", 1234)
        handler._read_body = Mock(return_value=b'{"model":"glm5.3-flash"}')
        handler._json = Mock()
        handler.server = SimpleNamespace(
            router=SimpleNamespace(backend_for=Mock(return_value="ds4")),
            select_model=Mock(),
        )

        ProxyHandler.do_POST(handler)

        handler.server.select_model.assert_called_once_with("glm5.3-flash")
        handler._json.assert_called_once_with(200, {"ok": True, "selected": "glm5.3-flash"})

    def test_select_endpoint_rejects_unknown_model(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/admin/select"
        handler.client_address = ("127.0.0.1", 1234)
        handler._read_body = Mock(return_value=b'{"model":"ghost"}')
        handler._json = Mock()
        handler.server = SimpleNamespace(
            router=SimpleNamespace(backend_for=Mock(side_effect=ValueError("no backend configured for model: ghost"))),
            select_model=Mock(),
        )

        ProxyHandler.do_POST(handler)

        handler.server.select_model.assert_not_called()
        self.assertEqual(handler._json.call_args[0][0], 400)


class PanelActionTests(unittest.TestCase):
    def test_switch_selects_model_then_finishes_warmup_before_reporting_success(self):
        actions = Actions("http://gate", "http://omlx")
        calls = []
        with patch("model_gate.panel.post_json", side_effect=lambda url, payload, **kwargs: calls.append((url, payload)) or {}):
            actions.switch("glm5.3-flash")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not actions.status().endswith(": ok"):
                time.sleep(0.01)
        self.assertEqual(calls, [
            ("http://gate/admin/select", {"model": "glm5.3-flash"}),
            ("http://gate/v1/chat/completions", {
                "model": "glm5.3-flash", "max_tokens": 1,
                "messages": [{"role": "user", "content": "warmup"}],
            }),
        ])
        self.assertTrue(actions.status().endswith(": ok"))


class PanelThemeTests(unittest.TestCase):
    def test_every_theme_defines_the_same_roles(self):
        base = set(panel.THEMES[panel.DEFAULT_THEME])
        for name, palette in panel.THEMES.items():
            self.assertEqual(set(palette), base, name)
            for role, rgb in palette.items():
                self.assertEqual(len(rgb), 3, f"{name}/{role}")
                self.assertTrue(all(0 <= v <= 255 for v in rgb), f"{name}/{role}")

    def test_apply_theme_rebuilds_sgr_and_ignores_unknown_name(self):
        original = panel.THEME
        try:
            for name in panel.THEMES:
                self.assertEqual(panel.apply_theme(name), name)
                self.assertEqual(set(panel.SGR) - {""}, set(panel.THEMES[name]))
            self.assertEqual(panel.apply_theme("tokyo-night"), "tokyo-night")
            # неизвестное имя не должно трогать текущую палитру
            self.assertEqual(panel.apply_theme("нет-такой"), "tokyo-night")
            self.assertEqual(set(panel.SGR) - {""}, set(panel.THEMES["tokyo-night"]))
        finally:
            panel.apply_theme(original)

    def test_next_theme_walks_the_whole_list_and_wraps(self):
        original = panel.THEME
        try:
            panel.apply_theme(panel.DEFAULT_THEME)
            seen = [panel.apply_theme(panel.next_theme()) for _ in range(len(panel.THEMES))]
            self.assertEqual(sorted(seen), sorted(panel.THEMES))
        finally:
            panel.apply_theme(original)

    def test_saved_theme_is_reused_and_garbage_falls_back_to_default(self):
        original = panel.THEME
        with tempfile.TemporaryDirectory() as tmp, patch.object(panel, "THEME_PATH", Path(tmp) / "theme"):
            panel.save_theme("nord")
            self.assertEqual(panel.load_theme(), "nord")
            panel.THEME_PATH.write_text("розовый\n")
            self.assertEqual(panel.load_theme(), panel.DEFAULT_THEME)
            panel.apply_theme(original)


class ProxyStreamingTests(unittest.TestCase):
    def test_copy_stream_reads_one_upstream_chunk_and_flushes_each_time(self):
        handler = object.__new__(ProxyHandler)
        handler.wfile = Mock()
        response = Mock()
        response.read.side_effect = AssertionError("must use read1 for streaming")
        response.read1.side_effect = [b"first", b"second", b""]

        ProxyHandler._copy_stream(handler, response)

        self.assertEqual(handler.wfile.write.call_args_list, [
            unittest.mock.call(b"first"),
            unittest.mock.call(b"second"),
        ])
        self.assertEqual(handler.wfile.flush.call_count, 2)

    def test_client_disconnect_does_not_send_a_second_error_response(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/v1/chat/completions"
        handler.headers = {"Content-Length": "2"}
        handler._read_body = Mock(return_value=b'{"model":"flash"}')
        handler._forward = Mock(side_effect=BrokenPipeError())
        handler._json = Mock()
        lease = Mock(initialize=False)
        handler.server = SimpleNamespace(
            router=SimpleNamespace(backend_for=Mock(return_value="ds4")),
            lifecycle_available=Mock(return_value=(True, "")),
            gate=SimpleNamespace(acquire=Mock(return_value=lease)),
            backends={"ds4": Backend("ds4", "http://ds4", 1)},
            stats=StatsCollector(),
            active_requests={}, _active_lock=threading.Lock(),
        )

        ProxyHandler.do_POST(handler)

        handler._json.assert_not_called()
        lease.release.assert_called_once_with()
        snapshot = handler.server.stats.snapshot()
        self.assertEqual(snapshot["models"]["ds4/flash"]["disconnected"], 1)
        self.assertEqual(snapshot["inflight"], [])


class ProcessManagerTests(unittest.TestCase):
    def test_autostart_runs_once_and_waits_for_ready(self):
        backend = Backend(
            "ds4", "http://ds4", 1,
            autostart_command=["zsh", "-lic", "ds4run"],
            startup_timeout=1,
            startup_poll_seconds=0,
        )
        manager = ProcessManager({"ds4": backend})
        process = Mock()
        process.poll.return_value = None
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with patch("model_gate.proxy.subprocess.Popen", return_value=process) as popen:
            with patch("model_gate.proxy.urlopen", side_effect=[URLError("down"), response, response]):
                manager.ensure_ready("ds4")
                manager.ensure_ready("ds4")

        popen.assert_called_once_with(
            ["zsh", "-lic", "ds4run"],
            start_new_session=True,
            stdout=unittest.mock.ANY,
            stderr=unittest.mock.ANY,
            text=True,
            bufsize=1,
        )

    def test_status_reports_and_stops_managed_backend(self):
        backend = Backend("ds4", "http://ds4", 1)
        manager = ProcessManager({"ds4": backend})
        process = Mock()
        process.poll.return_value = None
        process.pid = 1234
        manager._processes["ds4"] = process
        manager._commands["ds4"] = ["ds4-server", "-m", "flash.gguf"]
        manager._models["ds4"] = "flash"

        self.assertEqual(manager.snapshot(), {
            "ds4": {"running": True, "pid": 1234, "model": "flash", "started_ts": None, 'base_url': 'http://ds4'},
        })
        self.assertTrue(manager.stop("ds4"))
        process.terminate.assert_called_once_with()
        self.assertEqual(manager.snapshot(), {})

    def test_auto_port_is_reserved_for_gate_backend_and_interpolated(self):
        backend = Backend('ds4', 'http://127.0.0.1:0', 1,
                          autostart_command=['server', '--port', '{port}'], auto_port=True,
                          ready_path='/health', startup_poll_seconds=.001)
        manager = ProcessManager({'ds4': backend})
        process = Mock(); process.pid = 123
        process.poll.return_value = None
        manager._ready = Mock(side_effect=[False, True])
        with patch('model_gate.proxy.subprocess.Popen', return_value=process) as popen, \
             patch('model_gate.proxy.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            manager.ensure_ready('ds4', 'model')
        command = popen.call_args.args[0]
        port = int(command[command.index('--port') + 1])
        self.assertGreater(port, 0)
        self.assertEqual(backend.base_url, f'http://127.0.0.1:{port}')
        self.assertEqual(manager.snapshot()['ds4']['base_url'], backend.base_url)

    def test_autostart_restarts_backend_when_model_command_changes(self):
        commands = {
            "ds4-0731": ["ds4-server", "-m", "flash.gguf"],
            "ds4-vision": ["ds4-server", "-m", "vision.gguf", "--vision", "encoder.gguf"],
        }
        backend = Backend(
            "ds4", "http://ds4", 1,
            model_commands=commands,
            startup_timeout=1,
            startup_poll_seconds=0,
        )
        manager = ProcessManager({"ds4": backend})
        first = Mock()
        second = Mock()
        first.poll.return_value = None
        second.poll.return_value = None
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with patch("model_gate.proxy.subprocess.Popen", side_effect=[first, second]) as popen:
            with patch("model_gate.proxy.urlopen", side_effect=[URLError("down"), response, response]):
                manager.ensure_ready("ds4", "ds4-0731")
                manager.ensure_ready("ds4", "ds4-vision")

        first.terminate.assert_called_once_with()
        self.assertEqual([call.args[0] for call in popen.call_args_list], list(commands.values()))


class LifecycleTests(unittest.TestCase):
    def test_activating_another_model_unloads_the_first_one(self):
        backends = {
            "omlx": Backend(
                "omlx", "http://omlx", 1,
                load_url="http://omlx/load/{model}",
                unload_url="http://omlx/unload/{model}",
            ),
        }
        lifecycle = Lifecycle(backends)
        calls = []
        lifecycle._post = calls.append
        lifecycle._is_loaded = lambda *_: True

        lifecycle.activate("omlx", "coder")
        lifecycle.activate("omlx", "heavy-122b")

        self.assertEqual(calls, [
            "http://omlx/load/coder",
            "http://omlx/unload/coder",
            "http://omlx/load/heavy-122b",
        ])

    def test_evicted_model_is_loaded_again_after_ttl(self):
        backends = {
            "omlx": Backend("omlx", "http://omlx", 1, load_url="http://omlx/load/{model}"),
        }
        lifecycle = Lifecycle(backends)
        calls = []
        lifecycle._post = calls.append

        lifecycle.activate("omlx", "coder")
        lifecycle._is_loaded = lambda *_: False
        lifecycle.activate("omlx", "coder")

        self.assertEqual(calls, ["http://omlx/load/coder", "http://omlx/load/coder"])

    def test_switch_without_unload_hook_fails_closed_for_resident_backend(self):
        backends = {
            "omlx": Backend("omlx", "http://omlx", 1),
            "ds4": Backend("ds4", "http://ds4", 1, requires_unload=False),
        }
        lifecycle = Lifecycle(backends)
        lifecycle.activate("omlx", "coder")
        with self.assertRaises(LifecycleError):
            lifecycle.activate("ds4", "flash")

    def test_ephemeral_ds4_needs_no_unload_hook(self):
        backends = {
            "omlx": Backend("omlx", "http://omlx", 1, load_url="http://omlx/load/{model}"),
            "ds4": Backend("ds4", "http://ds4", 1, requires_unload=False),
        }
        lifecycle = Lifecycle(backends)
        calls = []
        lifecycle._post = calls.append

        lifecycle.activate("ds4", "flash")
        lifecycle.activate("omlx", "coder")

        self.assertEqual(calls, ["http://omlx/load/coder"])


class ModelRouterTests(unittest.TestCase):
    def test_routes_by_longest_prefix(self):
        router = ModelRouter({
            "omlx": {"prefixes": ["qwen"]},
            "ds4": {"prefixes": ["deepseek-v4"]},
        })
        self.assertEqual(router.backend_for("deepseek-v4-flash"), "ds4")
        self.assertEqual(router.backend_for("qwen3-coder-next"), "omlx")

    def test_unknown_model_is_rejected(self):
        router = ModelRouter({"omlx": {"models": ["known"]}})
        with self.assertRaises(ValueError):
            router.backend_for("unknown")


if __name__ == "__main__":
    unittest.main()
