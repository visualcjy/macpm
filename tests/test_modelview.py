"""Headless rendering tests for the model-service view (key 6)."""

import contextlib
import io
import os
import types
import unittest

from macpm.modelview import ModelView
from macpm.modelfeed import ModelStats, RequestRow
from macpm.serviceregistry import Service


def vllm(port=8000, model="Qwen3-8B-Instruct"):
    return Service("vllm", "prometheus", "127.0.0.1", port, model, (101, 102))


def llama(port=9931, model="llama-3.1-8B-Q4"):
    return Service("llama.cpp", "llamacpp", "127.0.0.1", port, model, (201,))


def ollama(port=11434):
    return Service("ollama", "openai", "127.0.0.1", port, "-", (301,))


class FakeFeed:
    def __init__(self, stats=None):
        self.stats = stats
        self.selected = None
        self.refreshes = 0

    def select(self, service):
        self.selected = service

    def snapshot(self):
        return self.stats

    def maybe_refresh(self):
        self.refreshes += 1

    def recent(self):
        return list(getattr(self, "recent_rows", []))

    def probe_states(self, services):
        pass

    def state(self, service):
        return ""


class FakeRegistry:
    def __init__(self, services):
        self.services = list(services)
        self.calls = 0
        self.error = ""

    def discover(self, force=False):
        self.calls += 1
        return list(self.services)


class RenderTests(unittest.TestCase):
    def render(self, view, width=110, height=30):
        os.environ["COLUMNS"] = str(width)
        os.environ["LINES"] = str(height)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display()
        return buffer.getvalue()

    def make(self, services, width=110, height=30, stats=None):
        view = ModelView(registry=FakeRegistry(services), feed=FakeFeed(stats),
                         no_color=True)
        view.prime()
        return view, self.render(view, width, height)

    def test_empty_state_names_the_supported_backends(self):
        _, output = self.make([])

        self.assertIn("no local model service found", output)
        self.assertIn("vllm", output)
        self.assertIn("splash", output)

    def test_single_service_gets_one_detail_card(self):
        view, output = self.make([vllm()])

        self.assertIn("MODEL SERVICE · vllm", output)
        self.assertIn("Qwen3-8B-Instruct", output)
        self.assertNotIn("MODEL SERVICES", output)
        self.assertIn("unverified", output)

    def test_list_shows_every_service_with_the_selection_marker(self):
        view, output = self.make([vllm(), llama(), ollama()])

        self.assertIn("MODEL SERVICES · 3 detected", output)
        self.assertIn("BACKEND", output)
        self.assertIn("vllm", output)
        self.assertIn("llama.cpp", output)
        self.assertIn("127.0.0.1:9931", output)
        self.assertEqual(output.count("\u25b6"), 1)
        self.assertEqual(view.selected().backend, "vllm")

    def test_arrow_keys_move_the_selection(self):
        view, _ = self.make([vllm(), llama(), ollama()])

        view.select(1)
        self.assertEqual(view.selected().backend, "llama.cpp")
        view.select(-5)
        self.assertEqual(view.selected().backend, "vllm")

    def test_sort_cycles_between_backend_model_and_port(self):
        view, _ = self.make([ollama(), vllm(), llama()])

        self.assertEqual(view.sort, "port")
        self.assertEqual([s.port for s in view.ordered()], [8000, 9931, 11434])
        view.cycle_sort()                       # backend
        self.assertEqual(view.sort, "backend")
        self.assertEqual([s.backend for s in view.ordered()],
                         ["llama.cpp", "ollama", "vllm"])
        view.cycle_sort()                       # model
        self.assertEqual(view.sort, "model")
        view.cycle_sort()                       # back to port
        self.assertEqual(view.sort, "port")

    def test_pause_stops_rescanning(self):
        registry = FakeRegistry([vllm()])
        feed = FakeFeed()
        view = ModelView(registry=registry, feed=feed, no_color=True)
        view.prime()
        calls = registry.calls

        view.toggle_pause()
        view.sample()
        self.assertEqual(registry.calls, calls)

        view.toggle_pause()
        view.sample()
        self.assertEqual(registry.calls, calls + 1)
        self.assertGreaterEqual(feed.refreshes, 1)

    def test_selection_follows_a_service_across_rescans(self):
        view, _ = self.make([vllm(), llama()])
        view.select(1)
        self.assertEqual(view.selected().backend, "llama.cpp")

        # A rescan reorders the list; the same endpoint stays selected.
        view._replace([llama(), vllm()])
        self.assertEqual(view.selected().backend, "llama.cpp")

    def test_selected_row_shows_the_live_rate(self):
        stats = ModelStats(state="live", decode_tps=61.4)
        view, output = self.make([vllm(), llama()], stats=stats)

        self.assertIn("live · 61 t/s", output)
        self.assertEqual(output.count("unverified"), 1)

    def test_detail_lists_rates_ttft_and_kv(self):
        stats = ModelStats(
            state="live", decode_tps=60.5, prefill_tps=608.4, active=2,
            ttft_p50_ms=575.0, ttft_p95_ms=5462.0,
            ctx_used=24100, ctx_total=320529,
            kv_used=1626341376.0, kv_total=25769803776.0,
            mem_used=2473902080.0, mem_peak=22575382528.0,
            requests=24, thermal="nominal",
        )
        view, output = self.make([vllm()], stats=stats)

        self.assertIn("60.5 tok/s", output)
        self.assertIn("608.4 tok/s", output)
        self.assertIn("575ms p50", output)
        self.assertIn("1.5 GiB", output)
        self.assertIn("24 done", output)
        self.assertIn("thermal nominal", output)

    def test_wide_terminal_shows_the_eight_card_grid(self):
        parse = {"processor": {"clusters": [{"name": "E-Cluster", "idle_ratio": 0.5}],
                               "gpu_energy": 20000.0, "combined_power": 20000.0},
                 "gpu": {"idle_ratio": 0.25, "name": "Apple M3 Max",
                         "mem_used_mb": 8192, "mem_total_mb": 36864},
                 "disk": {"rbytes_per_s": 1024 ** 2, "wbytes_per_s": 1024}}
        view = ModelView(registry=FakeRegistry([vllm()]), feed=FakeFeed(),
                         no_color=True, gpu_source=lambda: [])
        view.prime()
        os.environ["COLUMNS"] = "72"
        os.environ["LINES"] = "28"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display(parse, types.SimpleNamespace(interval=1), None)
        output = buffer.getvalue()

        # 72 columns keeps the two-column, six-card layout.
        for title in ("Decode", "GPU load", "VRAM", "GPU temp", "Power", "Context", "CPU", "Disk read"):
            self.assertIn(title, output)

    def test_wide_terminal_shows_all_eight_cards(self):
        parse = {"processor": {"clusters": [{"name": "E-Cluster", "idle_ratio": 0.5}],
                               "gpu_energy": 20000.0, "combined_power": 20000.0},
                 "gpu": {"idle_ratio": 0.25, "name": "Apple M3 Max",
                         "mem_used_mb": 8192, "mem_total_mb": 36864},
                 "disk": {"rbytes_per_s": 1024 ** 2, "wbytes_per_s": 1024}}
        view = ModelView(registry=FakeRegistry([vllm()]), feed=FakeFeed(),
                         no_color=True, gpu_source=lambda: [])
        view.prime()
        os.environ["COLUMNS"] = "110"
        os.environ["LINES"] = "34"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display(parse, types.SimpleNamespace(interval=1), None)
        output = buffer.getvalue()

        for title in ("Decode", "GPU load", "VRAM", "GPU temp",
                      "Power", "Context", "CPU", "Disk read"):
            self.assertIn(title, output)





    def test_ttft_marks_durations_but_not_percentile_names(self):
        from macpm.modelview import _timing_spans
        from macpm.theme import MUTED, VALUE

        spans = _timing_spans("1.32s p50 · 18.65s p95")
        roles = dict(spans)
        self.assertEqual(roles["1.32s"], VALUE)
        self.assertEqual(roles["18.65s"], VALUE)
        self.assertTrue(all(role == MUTED for text, role in spans
                            if "p50" in text or "p95" in text))


    def test_cpu_load_is_weighted_by_cores_not_by_cluster(self):
        parse = {
            "processor": {"clusters": [
                {"name": "E", "idle_ratio": 0.95,
                 "cpus": [{"idle_ratio": 0.95}] * 8},
                {"name": "P", "idle_ratio": 0.05,
                 "cpus": [{"idle_ratio": 0.05}] * 4},
            ], "combined_power": 0},
            "gpu": {"idle_ratio": 0.5},
            "disk": {},
        }
        view = ModelView(registry=FakeRegistry([vllm()]), feed=FakeFeed(),
                         no_color=True, gpu_source=lambda: [])
        view.prime()
        os.environ["COLUMNS"] = "110"
        os.environ["LINES"] = "34"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display(parse, types.SimpleNamespace(interval=1), None)
        # weighted: 1 - (0.95*8 + 0.05*4) / 12 = 35 %; the old cluster
        # average said 50 %.
        self.assertIn("35 %", buffer.getvalue())

    def test_heat_roles_follow_the_gpu_view(self):
        from macpm.modelview import _heat_role
        from macpm.theme import CRIT, OK, WARN

        self.assertEqual(_heat_role("gpu", 40), OK)
        self.assertEqual(_heat_role("gpu", 75), WARN)
        self.assertEqual(_heat_role("gpu", 95), CRIT)
        self.assertEqual(_heat_role("temp", 70), OK)
        self.assertEqual(_heat_role("temp", 85), WARN)
        self.assertEqual(_heat_role("temp", 95), CRIT)
        self.assertEqual(_heat_role("speed", 999), OK)

    def test_card_names_keep_the_brand_and_drop_the_series(self):
        from macpm.modelview import _short_card

        self.assertEqual(_short_card("NVIDIA GeForce RTX 4090"), "NV RTX 4090")
        self.assertEqual(_short_card("AMD Radeon RX 7900 XTX"), "AMD RX 7900")
        self.assertEqual(_short_card("Intel Arc A770"), "Intel A770")
        self.assertEqual(_short_card("Apple M3 Max"), "Apple M3 Max")

    def test_multi_gpu_cards_aggregate_vram_and_temperature(self):
        parse = {
            "processor": {"clusters": [], "combined_power": 0},
            "gpu": {"idle_ratio": 0.5, "name": "RTX 4090",
                    "mem_used_mb": 1000, "mem_total_mb": 24000, "temp_c": 30},
            "gpus": [
                {"name": "RTX 4090", "busy_pct": 10, "mem_used_mb": 20000,
                 "mem_total_mb": 24000, "temp_c": 61, "power_w": 100},
                {"name": "RTX 4090", "busy_pct": 30, "mem_used_mb": 22000,
                 "mem_total_mb": 24000, "temp_c": 65, "power_w": 120},
            ],
            "disk": {},
        }
        view = ModelView(registry=FakeRegistry([vllm()]), feed=FakeFeed(),
                         no_color=True, gpu_source=lambda: [])
        view.prime()
        os.environ["COLUMNS"] = "110"
        os.environ["LINES"] = "34"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display(parse, types.SimpleNamespace(interval=1), None)
        output = buffer.getvalue()

        self.assertIn("41.0 GB / 46.9 GB", output)
        self.assertIn("65 C", output)
        self.assertIn("220.00 W", output)

    def test_cards_and_vram_come_from_the_vendor_gpu_table(self):
        def gpu():
            return [
                {"pid": 101, "gpus": [0, 1, 2, 3], "mem_mb": 28672.0},
                {"pid": 102, "gpus": [0, 1, 2, 3], "mem_mb": 1024.0},
            ]

        view = ModelView(registry=FakeRegistry([vllm(), llama()]), feed=FakeFeed(),
                         no_color=True, gpu_source=gpu)
        view.prime()
        output = self.render(view)

        self.assertIn("CARDS", output)
        self.assertIn("0-3", output)
        self.assertIn("29.0 GiB", output)

    def test_cards_and_vram_are_hidden_without_a_gpu_table(self):
        view = ModelView(registry=FakeRegistry([vllm()]), feed=FakeFeed(),
                         no_color=True, gpu_source=lambda: [])
        view.prime()

        self.assertNotIn("CARDS", self.render(view))

    def test_recent_requests_are_listed_under_the_metrics(self):
        feed_view = ModelView(
            registry=FakeRegistry([vllm()]),
            feed=FakeFeed(ModelStats(state="live", decode_tps=42.0)),
            no_color=True,
        )
        feed_view.feed.recent_rows = [RequestRow(
            rid="a", finished=1_700_000_000.0, prompt=700, output=1204,
            tps=42.1, duration=28.6)]
        feed_view.prime()
        output = self.render(feed_view)

        self.assertIn("recent", output)
        self.assertIn("done", output)
        self.assertIn("1.2K", output)
        self.assertIn("42.1 tok/s", output)
        self.assertIn("29s", output)

    def test_auth_failure_is_named(self):
        stats = ModelStats(state="auth", note="HTTP 401")
        _, output = self.make([vllm()], stats=stats)

        self.assertIn("auth", output)
        self.assertIn("HTTP 401", output)

    def test_narrow_layout_drops_the_endpoint_column(self):
        _, output = self.make([vllm(), llama()], width=54)

        self.assertIn("MODEL", output)
        self.assertNotIn("ENDPOINT", output)


if __name__ == "__main__":
    unittest.main()
