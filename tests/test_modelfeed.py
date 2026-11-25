"""Adapter tests for the model metrics feed, using recorded payload shapes."""

import http.server
import json
import threading
import unittest
import urllib.error

from macpm.modelfeed import (
    ModelFeed,
    RequestRow,
    ModelStats,
    fetch_stats,
    parse_llamacpp,
    parse_ollama,
    parse_omlx,
    parse_prometheus,
    parse_sglang,
    parse_vllm,
    parse_splash,
)
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





LLAMA_PROPS = {
    "total_slots": 4,
    "model_path": "/models/llama-3.1-8B-Q4_K_M.gguf",
    "default_generation_settings": {"n_ctx": 32768},
}

LLAMA_METRICS = """
# HELP llamacpp:requests_processing Requests processing
llamacpp:requests_processing 3
llamacpp:requests_deferred 1
llamacpp:predicted_tokens_seconds 42.75
llamacpp:prompt_tokens_seconds 318.0
llamacpp:tokens_predicted_total 128400.0 1700000000
llamacpp:n_tokens_max 5120
"""

OLLAMA_PS = {
    "models": [
        {
            "name": "qwen3:8b",
            "model": "qwen3:8b",
            "size": 5_127_025_024,
            "size_vram": 5_127_025_024,
            "expires_at": "2099-01-01T00:00:00-08:00",
        }
    ]
}


class PrometheusTests(unittest.TestCase):
    def test_labels_comments_and_timestamps_are_tolerated(self):
        values = parse_prometheus(LLAMA_METRICS)

        self.assertEqual(values["llamacpp:requests_processing"], 3.0)
        self.assertEqual(values["llamacpp:tokens_predicted_total"], 128400.0)
        self.assertNotIn("# HELP llamacpp:requests_processing Requests processing",
                         values)


class LlamaCppParserTests(unittest.TestCase):
    def test_props_plus_metrics(self):
        stats = parse_llamacpp(LLAMA_PROPS, parse_prometheus(LLAMA_METRICS))

        self.assertEqual(stats.state, "live")
        self.assertEqual(stats.model, "llama-3.1-8B-Q4_K_M")
        self.assertEqual(stats.decode_tps, 42.75)
        self.assertEqual(stats.prefill_tps, 318.0)
        self.assertEqual((stats.active, stats.waiting), (3, 1))
        self.assertEqual((stats.slots_used, stats.slots_total), (3, 4))
        self.assertEqual(stats.ctx_total, 32768)
        self.assertEqual(stats.ctx_used, 5120)
        self.assertEqual(stats.tokens_total, 128400)

    def test_metrics_disabled_leaves_the_card_useful(self):
        stats = parse_llamacpp(LLAMA_PROPS, {})

        self.assertEqual(stats.model, "llama-3.1-8B-Q4_K_M")
        self.assertEqual(stats.slots_total, 4)
        self.assertIsNone(stats.decode_tps)


class OllamaParserTests(unittest.TestCase):
    def test_loaded_model_and_unload_countdown(self):
        stats = parse_ollama(OLLAMA_PS)

        self.assertEqual(stats.state, "live")
        self.assertEqual(stats.model, "qwen3:8b")
        self.assertAlmostEqual(stats.mem_used, 5_127_025_024)
        self.assertIsNotNone(stats.expires_s)
        self.assertGreater(stats.expires_s, 0)

    def test_nothing_loaded(self):
        stats = parse_ollama({"models": []})

        self.assertEqual(stats.model, "-")
        self.assertEqual(stats.note, "no model loaded")


class MockHandler(http.server.BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):
        MockHandler.seen.append((self.path, self.headers.get("Authorization")))
        body, status = MockHandler.routes.get(self.path, ({"error": "no"}, 404))
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class MockServerTests(unittest.TestCase):
    def setUp(self):
        MockHandler.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def service(self, backend="llama.cpp"):
        return Service(backend, "llamacpp", "127.0.0.1", self.port, "-", (1,))

    def test_llamacpp_without_metrics_says_so(self):
        MockHandler.routes = {
            "/props": (LLAMA_PROPS, 200),
            "/metrics": ({"error": "not supported"}, 501),
        }
        stats = fetch_stats(self.service())

        self.assertEqual(stats.state, "live")
        self.assertIn("metrics disabled", stats.note)
        self.assertEqual(stats.slots_total, 4)

    def test_llamacpp_with_metrics_reads_them(self):
        MockHandler.routes = {
            "/props": (LLAMA_PROPS, 200),
            "/metrics": (LLAMA_METRICS, 200),
        }
        stats = fetch_stats(self.service())

        self.assertEqual(stats.decode_tps, 42.75)
        self.assertFalse(stats.note)

    def test_ollama_ps_is_read(self):
        MockHandler.routes = {"/api/ps": (OLLAMA_PS, 200)}
        stats = fetch_stats(self.service("ollama"))

        self.assertEqual(stats.model, "qwen3:8b")

    def test_vllm_metrics_endpoint_is_supported(self):
        MockHandler.routes = {"/metrics": (VLLM_METRICS, 200),
                              "/v1/models": ({"data": [{"id": "Qwen3-72B",
                                                        "max_model_len": 131072}]}, 200)}
        stats = fetch_stats(self.service("vllm"))

        self.assertEqual(stats.model, "Qwen3-72B")
        self.assertEqual(stats.ctx_total, 131072)
        self.assertEqual(stats.active, 5)
        self.assertEqual(stats.kv_pct, 78.0)

    def test_sglang_metrics_and_server_info(self):
        MockHandler.routes = {
            "/metrics": (SGLANG_METRICS, 200),
            "/get_server_info": ({"model_path": "/m/Qwen3-32B",
                                  "context_length": 65536}, 200),
        }
        stats = fetch_stats(self.service("sglang"))

        self.assertEqual(stats.model, "Qwen3-32B")
        self.assertEqual(stats.ctx_total, 65536)
        self.assertEqual(stats.decode_tps, 52.5)

    def test_api_key_is_sent_as_a_bearer_token(self):
        MockHandler.routes = {"/props": (LLAMA_PROPS, 200),
                              "/metrics": (LLAMA_METRICS, 200)}

        fetch_stats(self.service(), key="secret")

        headers = [auth for path, auth in MockHandler.seen if path == "/props"]
        self.assertEqual(headers, ["Bearer secret"])




class RecentRequestTests(unittest.TestCase):
    def test_a_request_that_disappears_becomes_a_completed_row(self):
        now = [100.0]
        feed = ModelFeed(clock=lambda: now[0])

        feed._track(ModelStats(state="live", active_rows=[
            {"rid": "a", "prompt": 100, "output": 10}]))
        now[0] += 2.0
        feed._track(ModelStats(state="live", active_rows=[
            {"rid": "a", "prompt": 100, "output": 90}]))
        now[0] += 1.0
        feed._track(ModelStats(state="live", active_rows=[]))

        rows = feed.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].prompt, 100)
        self.assertEqual(rows[0].output, 90)
        self.assertAlmostEqual(rows[0].duration, 3.0)
        self.assertAlmostEqual(rows[0].tps, 30.0)
        self.assertIsNone(rows[0].first_ms)
        self.assertIsNone(rows[0].cached)

    def test_switching_service_clears_the_history(self):
        feed = ModelFeed(clock=lambda: 1.0)
        feed._track(ModelStats(state="live", active_rows=[]))
        feed.select(Service("splash", "native", "127.0.0.1", 8000, "x", (1,)))

        self.assertEqual(feed.recent(), [])




VLLM_METRICS = """
vllm:num_requests_running 5
vllm:num_requests_waiting 1
vllm:kv_cache_usage_perc 0.78
vllm:generation_tokens_total 2100000
vllm:prompt_tokens_total 18400000
vllm:request_success_total 41200
"""

SGLANG_METRICS = """
sglang:num_running_reqs 3
sglang:num_queue_reqs 0
sglang:gen_throughput 52.5
sglang:token_usage 0.42
sglang:generation_tokens_total 1000
sglang:prompt_tokens_total 5000
"""


class VllmSglangTests(unittest.TestCase):
    def test_vllm_gauges_and_counters(self):
        stats = parse_vllm(parse_prometheus(VLLM_METRICS), "Qwen3-72B")

        self.assertEqual(stats.active, 5)
        self.assertEqual(stats.waiting, 1)
        self.assertAlmostEqual(stats.kv_pct, 78.0)
        self.assertEqual(stats.requests, 41200)
        self.assertIsNone(stats.decode_tps)      # derived from counters later

    def test_sglang_reads_its_gauge(self):
        stats = parse_sglang(parse_prometheus(SGLANG_METRICS),
                             {"model_path": "/models/Qwen3-32B"})

        self.assertEqual(stats.model, "Qwen3-32B")
        self.assertEqual(stats.active, 3)
        self.assertAlmostEqual(stats.decode_tps, 52.5)
        self.assertAlmostEqual(stats.kv_pct, 42.0)


class RateDerivationTests(unittest.TestCase):
    def test_counters_become_tok_per_second(self):
        now = [100.0]
        feed = ModelFeed(clock=lambda: now[0])
        service = Service("vllm", "prometheus", "127.0.0.1", 8000, "m", (1,))
        first = ModelStats(state="live", gen_tokens=1000, prompt_tokens=2000)
        feed._derive_rates(service, first)
        self.assertIsNone(first.decode_tps)

        now[0] += 2.0
        second = ModelStats(state="live", gen_tokens=1080, prompt_tokens=3000)
        feed._derive_rates(service, second)

        self.assertAlmostEqual(second.decode_tps, 40.0)
        self.assertAlmostEqual(second.prefill_tps, 500.0)


if __name__ == "__main__":
    unittest.main()
