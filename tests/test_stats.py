import unittest
from unittest.mock import patch

from model_gate.stats import StatsCollector, parse_usage


class StatsAuditTests(unittest.TestCase):
    def test_malformed_upstream_usage_cannot_leak_an_inflight_entry(self):
        stats = StatsCollector()
        entry = stats.start("model", "backend", False, 1)
        entry.usage = {"prompt_tokens": "oops", "completion_tokens": {},
                       "prompt_tokens_details": "bad", "prompt_tokens_per_second": "NaN"}
        stats.finish(entry, status=200)
        snapshot = stats.snapshot()
        self.assertEqual(snapshot["inflight"], [])
        self.assertEqual(snapshot["recent"][0]["prompt_tokens"], 0)
        self.assertIsNone(snapshot["recent"][0]["prefill_tps"])

    def test_usage_parser_handles_braces_inside_json_strings(self):
        self.assertEqual(parse_usage(b'{"usage":{"prompt_tokens":3,"note":"}"}}'),
                         {"prompt_tokens": 3, "note": "}"})

    def test_decode_does_not_make_old_prefill_metrics_current(self):
        stats = StatsCollector()
        with patch("model_gate.stats.time.time", return_value=1):
            stats.record_backend_log("backend", "prefill chunk 100/100 (100.0%) chunk=100.00 t/s avg=100.00 t/s")
            stats.record_backend_log("backend", "prompt done 1.000s")
        with patch("model_gate.stats.time.time", return_value=10):
            entry = stats.start("model", "backend", True, 1)
            stats.record_backend_log("backend", "decoding chunk=50.00 t/s avg=50.00 t/s")
        stats.finish(entry, status=200)
        self.assertIsNone(stats.snapshot()["recent"][0]["prefill_tps"])
