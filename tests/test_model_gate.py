import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

from model_gate.gate import AdmissionGate, AdmissionPolicy
from model_gate.proxy import Backend, GateServer, Lifecycle, LifecycleError, ProcessManager, ProxyHandler
from model_gate.router import ModelRouter


class AdmissionGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = AdmissionGate({
            ("omlx", "coder"): AdmissionPolicy(8, "light"),
            ("omlx", "thinking"): AdmissionPolicy(2, "light"),
            ("omlx", "heavy-122b"): AdmissionPolicy(),
            ("ds4", "flash"): AdmissionPolicy(),
        })

    def test_light_models_run_together(self):
        coder = self.gate.acquire("omlx", "coder")
        coder.mark_ready()
        thinking = self.gate.acquire("omlx", "thinking")
        thinking.mark_ready()

        self.assertEqual(self.gate.snapshot()["active"], 2)
        coder.release()
        thinking.release()

    def test_heavy_model_waits_for_every_light_request(self):
        coder = self.gate.acquire("omlx", "coder")
        coder.mark_ready()
        thinking = self.gate.acquire("omlx", "thinking")
        thinking.mark_ready()
        result = []
        thread = threading.Thread(
            target=lambda: result.append(self.gate.acquire("omlx", "heavy-122b"))
        )
        thread.start()
        self.assertTrue(thread.is_alive())

        coder.release()
        self.assertTrue(thread.is_alive())
        thinking.release()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        result[0].release()

    def test_exclusive_waiter_blocks_new_light_work(self):
        coder = self.gate.acquire("omlx", "coder")
        coder.mark_ready()
        heavy = []
        later_light = []
        heavy_thread = threading.Thread(
            target=lambda: heavy.append(self.gate.acquire("ds4", "flash"))
        )
        light_thread = threading.Thread(
            target=lambda: later_light.append(self.gate.acquire("omlx", "thinking"))
        )
        heavy_thread.start()
        light_thread.start()
        self.assertTrue(light_thread.is_alive())

        coder.release()
        heavy_thread.join(timeout=1)
        self.assertEqual(len(heavy), 1)
        self.assertTrue(light_thread.is_alive())
        heavy[0].release()
        light_thread.join(timeout=1)
        later_light[0].release()

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

    def test_model_list_refreshes_and_aggregates_backends(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/v1/models"
        handler._json = Mock()
        handler.server = SimpleNamespace(
            refresh_models=Mock(),
            backends={
                "omlx": Backend("omlx", "http://omlx", 1, models=["coder"]),
                "ds4": Backend("ds4", "http://ds4", 1, models=["flash"]),
            },
        )

        ProxyHandler.do_GET(handler)

        handler.server.refresh_models.assert_called_once_with()
        handler._json.assert_called_once_with(200, {
            "object": "list",
            "data": [
                {"id": "coder", "object": "model", "owned_by": "omlx"},
                {"id": "flash", "object": "model", "owned_by": "ds4"},
            ],
        })

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
            backends={"omlx": object()},
            refresh_models=Mock(),
        )

        ProxyHandler.do_POST(handler)

        self.assertEqual(handler.path, "/v1/chat/completions")
        handler._forward.assert_called_once_with(
            handler.server.backends["omlx"], b'{"model":"coder"}')
        handler._json.assert_not_called()


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
            backends={"ds4": object()},
        )

        ProxyHandler.do_POST(handler)

        handler._json.assert_not_called()
        lease.release.assert_called_once_with()


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
        )


class LifecycleTests(unittest.TestCase):
    def test_light_models_load_without_unloading_each_other(self):
        backends = {
            "omlx": Backend(
                "omlx", "http://omlx", 1,
                model_policies={"coder": {"sharing_group": "light"}, "thinking": {"sharing_group": "light"}},
                load_url="http://omlx/load/{model}",
                unload_url="http://omlx/unload/{model}",
            ),
        }
        lifecycle = Lifecycle(backends)
        calls = []
        lifecycle._post = calls.append

        lifecycle.activate("omlx", "coder", "light")
        lifecycle.activate("omlx", "thinking", "light")

        self.assertEqual(calls, ["http://omlx/load/coder", "http://omlx/load/thinking"])

    def test_evicted_model_is_loaded_again_after_ttl(self):
        backends = {
            "omlx": Backend("omlx", "http://omlx", 1, load_url="http://omlx/load/{model}"),
        }
        lifecycle = Lifecycle(backends)
        calls = []
        lifecycle._post = calls.append

        lifecycle.activate("omlx", "coder", "light")
        lifecycle._is_loaded = lambda *_: False
        lifecycle.activate("omlx", "coder", "light")

        self.assertEqual(calls, ["http://omlx/load/coder", "http://omlx/load/coder"])

    def test_heavy_model_unloads_light_pool_before_loading(self):
        backends = {
            "omlx": Backend(
                "omlx", "http://omlx", 1,
                model_policies={"coder": {"sharing_group": "light"}},
                load_url="http://omlx/load/{model}",
                unload_url="http://omlx/unload/{model}",
            ),
        }
        lifecycle = Lifecycle(backends)
        calls = []
        lifecycle._post = calls.append

        lifecycle.activate("omlx", "coder", "light")
        lifecycle.activate("omlx", "heavy-122b", None)

        self.assertEqual(calls, [
            "http://omlx/load/coder",
            "http://omlx/unload/coder",
            "http://omlx/load/heavy-122b",
        ])

    def test_switch_without_unload_hook_fails_closed_for_resident_backend(self):
        backends = {
            "omlx": Backend("omlx", "http://omlx", 1),
            "ds4": Backend("ds4", "http://ds4", 1, requires_unload=False),
        }
        lifecycle = Lifecycle(backends)
        lifecycle.activate("omlx", "coder", None)
        with self.assertRaises(LifecycleError):
            lifecycle.activate("ds4", "flash", None)

    def test_ephemeral_ds4_needs_no_unload_hook(self):
        backends = {
            "omlx": Backend("omlx", "http://omlx", 1, load_url="http://omlx/load/{model}"),
            "ds4": Backend("ds4", "http://ds4", 1, requires_unload=False),
        }
        lifecycle = Lifecycle(backends)
        calls = []
        lifecycle._post = calls.append

        lifecycle.activate("ds4", "flash", None)
        lifecycle.activate("omlx", "coder", "light")

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
