"""Изолированные HTTP-проверки: случайные loopback-порты, никаких моделей."""
import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from model_gate.proxy import GateServer, ProxyHandler


class FakeBackend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        scenario = payload.get('scenario')
        if scenario == 'quiet-headers':
            self.server.blocked_started.set()
            self.server.blocked_release.wait(3)
            self.close_connection = True
            return
        if scenario == "quiet-stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            self.server.stream_started.set()
            self.server.stream_release.wait(3)
            self.close_connection = True
            return
        if scenario == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        if scenario == "truncated":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"partial")
            self.wfile.flush()
            self.close_connection = True
            return
        if scenario == "redirect":
            self.send_response(307)
            self.send_header("Location", "/should-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = 429 if scenario == "busy" else 200
        body = json.dumps({"error": "busy"} if status == 429 else {
            "model": payload["model"], "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
        cls.backend.stream_started = threading.Event()
        cls.backend.stream_release = threading.Event()
        cls.backend.blocked_started = threading.Event()
        cls.backend.blocked_release = threading.Event()
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        with patch.object(GateServer, "_load_selected", return_value=""):
            cls.gate = GateServer(("127.0.0.1", 0), ProxyHandler, {
                "auto_model": "example", "upstream_timeout": 1,
                "backends": {"test": {"base_url": f"http://127.0.0.1:{cls.backend.server_port}",
                                       "models": ["example"], "requires_unload": False}},
            })
        threading.Thread(target=cls.gate.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.gate.shutdown()
        cls.gate.server_close()
        cls.backend.shutdown()
        cls.backend.server_close()

    def request(self, payload, path="/v1/chat/completions", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.gate.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("POST", path, json.dumps(payload),
                           {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        body = response.read()
        return response.status, body

    def test_alias_roundtrip_and_usage(self):
        status, body = self.request({"model": "local-auto"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model"], "example")
        self.wait_idle()

    def wait_idle(self):
        with self.gate.gate._condition:
            self.assertTrue(self.gate.gate._condition.wait_for(lambda: not self.gate.gate._active, timeout=1))

    def test_admin_halt_rejects_requests_until_explicit_resume(self):
        status, body = self.request({}, '/admin/halt')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['halted'])
        try:
            status, _ = self.request({'model': 'example'})
            self.assertEqual(status, 503)
            status, body = self.request({}, '/admin/resume')
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)['halted'])
            status, _ = self.request({'model': 'example'})
            self.assertEqual(status, 200)
        finally:
            if self.gate.gate.snapshot()['halted']:
                self.request({}, '/admin/resume')
        self.wait_idle()

    def test_backend_429_is_not_replaced_with_502(self):
        status, body = self.request({"model": "example", "scenario": "busy"})
        self.assertEqual((status, json.loads(body)), (429, {"error": "busy"}))

    def test_redirect_is_returned_without_following_it(self):
        status, body = self.request({"model": "example", "scenario": "redirect"})
        self.assertEqual((status, body), (307, b""))

    def test_non_object_json_returns_400_and_closes_connection(self):
        status, _ = self.request([])
        self.assertEqual(status, 400)

    def test_browser_cannot_switch_a_local_model(self):
        status, _ = self.request({"model": "example"}, "/admin/select", {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_dns_rebinding_cannot_switch_a_local_model(self):
        status, _ = self.request({"model": "example"}, "/admin/select", {"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_cancel_unblocks_a_quiet_upstream_without_waiting_for_next_token(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.gate.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("POST", "/v1/chat/completions",
                           json.dumps({"model": "example", "scenario": "quiet-stream", "stream": True}),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertTrue(self.backend.stream_started.wait(1))
        request_id = self.gate.stats.snapshot()["inflight"][0]["id"]
        try:
            status, _ = self.request({"id": request_id}, "/admin/cancel")
            self.assertEqual(status, 200)
            with self.assertRaises(http.client.IncompleteRead):
                response.read()
        finally:
            self.backend.stream_release.set()
        self.wait_idle()
        record = next(item for item in self.gate.stats.snapshot()["recent"] if item["disconnected"])
        self.assertEqual(record["status"], 0)

    def test_disconnect_before_upstream_headers_releases_admission(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.gate.server_port, timeout=3)
        self.backend.blocked_started.clear()
        self.backend.blocked_release.clear()
        old_timeout = self.gate.upstream_timeout
        self.gate.upstream_timeout = 5
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps({'model': 'example', 'scenario': 'quiet-headers'}),
                               {'Content-Type': 'application/json'})
            self.assertTrue(self.backend.blocked_started.wait(1))
            connection.close()
            with self.gate.gate._condition:
                self.assertTrue(self.gate.gate._condition.wait_for(lambda: not self.gate.gate._active, timeout=.8))
        finally:
            connection.close()
            self.backend.blocked_release.set()
            self.gate.upstream_timeout = old_timeout

    def test_queued_disconnected_client_is_removed_without_loading(self):
        held = self.gate.gate.acquire('test', 'held')
        connection = http.client.HTTPConnection('127.0.0.1', self.gate.server_port, timeout=3)
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps({'model': 'example'}), {'Content-Type': 'application/json'})
            with self.gate.gate._condition:
                self.assertTrue(self.gate.gate._condition.wait_for(lambda: len(self.gate.gate._queue) == 1, timeout=1))
            connection.close()
            with self.gate.gate._condition:
                self.assertTrue(self.gate.gate._condition.wait_for(lambda: not self.gate.gate._queue, timeout=1))
        finally:
            connection.close()
            held.release()

    def test_truncated_upstream_does_not_get_a_second_json_response(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.gate.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("POST", "/v1/chat/completions",
                           json.dumps({"model": "example", "scenario": "truncated"}),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        with self.assertRaises(http.client.IncompleteRead) as error:
            response.read()
        self.assertEqual(error.exception.partial, b"partial")

    def test_sse_without_content_length_is_chunked(self):
        status, body = self.request({"model": "example", "scenario": "sse", "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"data: first\n\n")
