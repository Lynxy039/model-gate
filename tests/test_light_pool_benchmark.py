import unittest
from unittest.mock import patch

from model_gate.light_pool_benchmark import build_prompt, parse_contexts, parse_vm_stat, prompt_for_tokens


class LightPoolBenchmarkTests(unittest.TestCase):
    def test_parse_contexts_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            parse_contexts("25000,0")

    def test_prompt_is_deterministic_and_changes_with_seed(self):
        self.assertEqual(build_prompt("coder-1", 3), build_prompt("coder-1", 3))
        self.assertNotEqual(build_prompt("coder-1", 3), build_prompt("thinking-1", 3))

    def test_vm_stat_is_converted_to_bytes(self):
        parsed = parse_vm_stat(
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free: 10.\nPages active: 20.\nPages wired down: 3.\n"
            "Pages occupied by compressor: 4.\n"
        )
        self.assertEqual(parsed["free_bytes"], 163_840)
        self.assertEqual(parsed["compressed_bytes"], 65_536)

    def test_prompt_sizing_converges_without_large_overshoot(self):
        def count_tokens(_, payload, __):
            units = payload["messages"][0]["content"].count("seed-")
            return {"input_tokens": units * 30}

        with patch("model_gate.light_pool_benchmark.request_json", side_effect=count_tokens):
            _, count = prompt_for_tokens("http://omlx", "model", 5_000, "seed", 1)

        self.assertGreaterEqual(count, 4_950)
        self.assertLessEqual(count, 5_050)


if __name__ == "__main__":
    unittest.main()
