"""Adapter tests for the model metrics feed, using recorded payload shapes."""

import unittest
import urllib.error

from macpm.modelfeed import ModelFeed, ModelStats, parse_omlx, parse_splash
from macpm.serviceregistry import Service

SPLASH_STATUS = {
    "schema_version": 8,
    "ready": True,
    "state": {"entries": 84, "active_lanes": 0},
    "requests": {"submitted": 24, "completed": 24, "cancelled": 0, "failed": 0},
    "active_requests": [
        {"phase": "decode", "prompt_tokens": 700, "generated_tokens": 30},
        {"phase": "decode", "prompt_tokens": 640, "generated_tokens": 12},
        {"phase": "prefill", "prompt_tokens": 4000, "generated_tokens": 0},
    ],
    "maximum_context_tokens": 320529,
    "capabilities": {"max_context_tokens": 320529},
    "metrics": {
        "decode_tokens_per_second": 60.46786035,
        "prefill_tokens_per_second": 608.4140132,
        "ttft_ms": {"p50": 575.023833, "p95": 5462.264417, "samples": 24},
    },
    "memory_actual": {"current_bytes": 2473902080, "peak_bytes": 22575382528},
    "kv": {"allocated_bytes": 1626341376},
    "memory_plan": {"budget": {"hard_budget_bytes": 25769803776}},
    "thermal_state": "nominal",
}

OMLX_ACTIVITY = {
    "active_models": {
        "models": [
            {"id": "idle-model", "loaded": True, "active_requests": 0,
             "waiting_requests": 0, "idle_seconds": 30, "generating": []},
            {"id": "qwen3-8b", "loaded": True, "active_requests": 2,
             "waiting_requests": 1,
             "generating": [{"tokens_per_second": 40.5}, {"tokens_per_second": 12.0}],
             "prefilling": [{"speed": 318.0, "processed": 1024, "total": 4096,
                             "prompt_tokens": 4096}],
             "idle_seconds": 0.0},
        ],
        "model_memory_used": 9 * 1024 ** 3,
        "model_memory_max": 48 * 1024 ** 3,
        "total_active_requests": 2,
        "total_waiting_requests": 1,
    }
}


class SplashParserTests(unittest.TestCase):
    def test_reads_rates_ttft_and_capacity(self):
        stats = parse_splash(SPLASH_STATUS)

        self.assertEqual(stats.state, "live")
        self.assertAlmostEqual(stats.decode_tps, 60.47, places=2)
        self.assertAlmostEqual(stats.prefill_tps, 608.41, places=2)
        self.assertEqual(stats.ttft_p50_ms, 575.023833)
        self.assertEqual(stats.ctx_total, 320529)
        self.assertEqual(stats.ctx_used, 5382)
        self.assertEqual(stats.kv_total, 25769803776)
        self.assertEqual(stats.requests, 24)
        self.assertEqual(stats.thermal, "nominal")

    def test_phases_are_counted(self):
        stats = parse_splash(SPLASH_STATUS)

        self.assertEqual(stats.phases, {"decode": 2, "prefill": 1})
        self.assertEqual(stats.active, 3)

    def test_an_unready_engine_is_stale(self):
        payload = dict(SPLASH_STATUS, ready=False)
        self.assertEqual(parse_splash(payload).state, "stale")


class OmlxParserTests(unittest.TestCase):
    def test_picks_the_busy_model(self):
        stats = parse_omlx(OMLX_ACTIVITY)

        self.assertEqual(stats.model, "qwen3-8b")
        self.assertEqual(stats.active, 2)
        self.assertEqual(stats.waiting, 1)
        self.assertAlmostEqual(stats.decode_tps, 52.5, places=1)
        self.assertAlmostEqual(stats.prefill_tps, 318.0, places=1)
        self.assertEqual(stats.phases, {"decode": 2, "prefill": 1})

    def test_no_loaded_model_is_reported(self):
        stats = parse_omlx({"active_models": {"models": []}})
        self.assertEqual(stats.state, "no-telemetry")


class FeedTests(unittest.TestCase):
    def test_an_auth_failure_becomes_the_auth_state(self):
        def fetcher(service, key):
            raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

        feed = ModelFeed(fetcher=fetcher)
        feed.select(Service("omlx", "native", "127.0.0.1", 8010, "x", (1,)))
        feed.maybe_refresh()
        feed._thread.join(2)

        self.assertEqual(feed.snapshot().state, "auth")

    def test_a_transport_error_becomes_the_error_state(self):
        def fetcher(service, key):
            raise TimeoutError("timed out")

        feed = ModelFeed(fetcher=fetcher)
        feed.select(Service("splash", "native", "127.0.0.1", 8000, "x", (1,)))
        feed.maybe_refresh()
        feed._thread.join(2)

        snapshot = feed.snapshot()
        self.assertEqual(snapshot.state, "error")
        self.assertIn("timed out", snapshot.note)

    def test_switching_service_drops_the_old_snapshot(self):
        def fetcher(service, key):
            return ModelStats(state="live", decode_tps=1.0)

        feed = ModelFeed(fetcher=fetcher)
        first = Service("splash", "native", "127.0.0.1", 8000, "x", (1,))
        feed.select(first)
        feed.maybe_refresh()
        feed._thread.join(2)
        self.assertIsNotNone(feed.snapshot())

        feed.select(Service("omlx", "native", "127.0.0.1", 8010, "y", (2,)))

        self.assertIsNone(feed.snapshot())


if __name__ == "__main__":
    unittest.main()
