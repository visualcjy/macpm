"""Splash log lines become request rows (the dashboard's own numbers)."""

import os
import tempfile
import unittest

from macpm.modelfeed import ModelFeed
from macpm.modellog import LogTail, SplashLogReader
from macpm.serviceregistry import Service

SAMPLE = """01:11:07 Started · request 46 · input 77,828 · cached 69,568 · effort absent → medium
01:11:25 TTFT of request 46: 18651.3 ms: own prefill 18650, claim wait 0, decode 0
01:11:53 Done · request 46 · input 77,828 · cached 69,568 · output 1,299 · tools 54·6abfe557 · TTFT 18.7s · 46.5 tok/s · 3.0 tok/cycle
01:12:00 Memory: growth paused; waiting=0; suspended=0
"""


class SplashLogTests(unittest.TestCase):
    def test_three_lines_become_one_row(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write(SAMPLE)
            path = handle.name
        try:
            reader = SplashLogReader(path)
            rows = reader.poll()
        finally:
            os.unlink(path)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["prompt"], 77828)
        self.assertEqual(row["cached"], 69568)
        self.assertEqual(row["output"], 1299)
        self.assertEqual(row["first_ms"], 18651.3)
        self.assertEqual(row["tps"], 46.5)
        self.assertEqual(row["duration"], 46.0)

    def test_only_appended_bytes_are_read(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write(SAMPLE)
            path = handle.name
        try:
            reader = SplashLogReader(path)
            self.assertEqual(len(reader.poll()), 1)
            self.assertEqual(reader.poll(), [])
            with open(path, "a", encoding="utf-8") as extra:
                extra.write(SAMPLE)
            self.assertEqual(len(reader.poll()), 1)
        finally:
            os.unlink(path)

    def test_feed_prefers_the_log(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write(SAMPLE)
            path = handle.name
        try:
            os.environ["SPLASH_LOG"] = path
            feed = ModelFeed()
            service = Service("splash", "native", "127.0.0.1", 8000, "m", (1,))
            feed.select(service)
            feed._read_log(service)
            rows = feed.recent()
        finally:
            os.environ.pop("SPLASH_LOG", None)
            os.unlink(path)

        self.assertEqual(rows[0].cached, 69568)
        self.assertEqual(rows[0].first_ms, 18651.3)


if __name__ == "__main__":
    unittest.main()
