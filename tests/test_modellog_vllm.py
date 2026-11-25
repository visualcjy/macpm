"""vLLM's periodic stats line and the command-log tail."""

import unittest

from macpm.modellog import CommandTail, parse_vllm_stats

LINE = ("(APIServer pid=1) INFO 11-25 20:57:31 [loggers.py:310] Engine 000: "
        "Avg prompt throughput: 22.3 tokens/s, Avg generation throughput: 15.4 tokens/s, "
        "Running: 1 reqs, Waiting: 0 reqs, GPU KV cache usage: 0.1%, "
        "Prefix cache hit rate: 92.9%")


class VllmStatsTests(unittest.TestCase):
    def test_the_real_line_is_parsed(self):
        stats = parse_vllm_stats(LINE)

        self.assertEqual(stats["prefill_tps"], 22.3)
        self.assertEqual(stats["decode_tps"], 15.4)
        self.assertEqual((stats["active"], stats["waiting"]), (1, 0))
        self.assertEqual(stats["kv_pct"], 0.1)
        self.assertEqual(stats["prefix_hit"], 92.9)

    def test_other_lines_are_ignored(self):
        self.assertIsNone(parse_vllm_stats("GET /health 200 OK"))


class CommandTailTests(unittest.TestCase):
    def test_only_new_lines_are_returned(self):
        tail = CommandTail(["/bin/sh", "-c", "printf 'a\\nb\\n'"])

        self.assertEqual(tail.read(), ["a", "b"])
        self.assertEqual(tail.read(), [])


if __name__ == "__main__":
    unittest.main()
