"""Headless rendering tests for the model-service view (key 6)."""

import contextlib
import io
import os
import unittest

from macpm.modelview import ModelView
from macpm.serviceregistry import Service


def vllm(port=8000, model="Qwen3-8B-Instruct"):
    return Service("vllm", "prometheus", "127.0.0.1", port, model, (101, 102))


def llama(port=9931, model="llama-3.1-8B-Q4"):
    return Service("llama.cpp", "llamacpp", "127.0.0.1", port, model, (201,))


def ollama(port=11434):
    return Service("ollama", "openai", "127.0.0.1", port, "-", (301,))


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

    def make(self, services, width=110, height=30):
        view = ModelView(registry=FakeRegistry(services), no_color=True)
        view.prime()
        return view, self.render(view, width, height)

    def test_empty_state_names_the_supported_backends(self):
        _, output = self.make([])

        self.assertIn("no local model service found", output)
        self.assertIn("vllm", output)
        self.assertIn("splash", output)

    def test_single_service_gets_one_detail_card(self):
        view, output = self.make([vllm()])

        self.assertIn("MODEL · vllm", output)
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
        view = ModelView(registry=registry, no_color=True)
        view.prime()
        calls = registry.calls

        view.toggle_pause()
        view.sample()
        self.assertEqual(registry.calls, calls)

        view.toggle_pause()
        view.sample()
        self.assertEqual(registry.calls, calls + 1)

    def test_selection_follows_a_service_across_rescans(self):
        view, _ = self.make([vllm(), llama()])
        view.select(1)
        self.assertEqual(view.selected().backend, "llama.cpp")

        # A rescan reorders the list; the same endpoint stays selected.
        view._replace([llama(), vllm()])
        self.assertEqual(view.selected().backend, "llama.cpp")

    def test_narrow_layout_drops_the_endpoint_column(self):
        _, output = self.make([vllm(), llama()], width=54)

        self.assertIn("MODEL", output)
        self.assertNotIn("ENDPOINT", output)


if __name__ == "__main__":
    unittest.main()
