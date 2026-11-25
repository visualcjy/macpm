"""Docker json logs and published-port discovery."""

import json
import os
import tempfile
import unittest

from macpm.modellog import JsonLogTail, docker_containers
from macpm.serviceregistry import ServiceRegistry

SAMPLE_PS = """vllm-qwen|vllm/vllm-openai:latest|0.0.0.0:8000->8000/tcp, :::8000->8000/tcp
sglang-box|sglang/srt:latest|127.0.0.1:30000->30000/tcp
idle|nginx:latest|80/tcp
"""


class JsonLogTailTests(unittest.TestCase):
    def test_docker_json_lines_are_unwrapped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write(json.dumps({"log": "line one\n"}) + "\n")
            handle.write("plain line\n")
            path = handle.name
        try:
            tail = JsonLogTail(path)
            self.assertEqual(tail.read(), ["line one", "plain line"])
        finally:
            os.unlink(path)


class DockerDiscoveryTests(unittest.TestCase):
    def test_published_ports_become_rows(self):
        rows = docker_containers(runner=lambda cmd: SAMPLE_PS)

        self.assertIn(("vllm", 8000, "vllm-qwen", "vllm/vllm-openai:latest"), rows)
        self.assertIn(("sglang", 30000, "sglang-box", "sglang/srt:latest"), rows)
        self.assertEqual(len(rows), 2)      # the internal-only port is skipped

    def test_registry_merges_containers(self):
        registry = ServiceRegistry(
            process_source=lambda: [],
            prober=lambda host, port: True,
            container_source=lambda: [("vllm", 8000, "c", "vllm/x")],
        )
        services = registry.discover(force=True)

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].source, "container")
        self.assertEqual(services[0].endpoint, "127.0.0.1:8000")


if __name__ == "__main__":
    unittest.main()
