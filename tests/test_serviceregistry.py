"""Service discovery sees command lines and a TCP probe, never HTTP."""

import unittest

from macpm.serviceregistry import (
    ServiceRegistry,
    identify,
    parse_endpoint,
    parse_model,
    parse_port,
)

VLLM_API = ["python3", "-m", "vllm.entrypoints.openai.api_server",
            "--model", "meta-llama/Llama-3.1-8B-Instruct", "--port", "8001"]
VLLM_CLI = ["/opt/homebrew/bin/vllm", "serve", "Qwen/Qwen3-8B",
            "--port=8002", "--served-model-name", "qwen3-8b"]
VLLM_WORKER = ["python3", "-c", "from multiprocessing.spawn import spawn_main"]
SGLANG = ["python3", "-m", "sglang.launch_server",
          "--model-path", "/models/Qwen3-32B-AWQ", "--port", "30000"]
LLAMACPP = ["/usr/local/bin/llama-server", "-m", "/models/llama-3.1-8B-Q4_K_M.gguf"]
OLLAMA = ["/Applications/Ollama.app/Contents/MacOS/ollama", "serve"]
OMLX = ["/Users/x/.omlx/bin/omlx", "serve", "--model-dir", "/models"]
SPLASH = ["/opt/homebrew/bin/splash", "serve",
          "--model", "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M", "--port", "8099"]


class IdentifyTests(unittest.TestCase):
    def test_vllm_api_server(self):
        backend = identify(VLLM_API)
        self.assertEqual(backend.key, "vllm")
        self.assertEqual(backend.family, "prometheus")
        self.assertEqual(parse_port(VLLM_API, backend), 8001)
        self.assertEqual(parse_model(VLLM_API, backend), "Llama-3.1-8B-Instruct")

    def test_vllm_cli_prefers_the_served_name(self):
        backend = identify(VLLM_CLI)
        self.assertEqual(parse_port(VLLM_CLI, backend), 8002)
        self.assertEqual(parse_model(VLLM_CLI, backend), "qwen3-8b")

    def test_vllm_workers_are_not_servers(self):
        self.assertIsNone(identify(VLLM_WORKER))

    def test_sglang_model_path_basename(self):
        backend = identify(SGLANG)
        self.assertEqual(backend.key, "sglang")
        self.assertEqual(backend.default_port, 30000)
        self.assertEqual(parse_model(SGLANG, backend), "Qwen3-32B-AWQ")

    def test_llama_cpp_strips_the_gguf_suffix_and_uses_its_default_port(self):
        backend = identify(LLAMACPP)
        self.assertEqual(backend.key, "llama.cpp")
        self.assertEqual(parse_port(LLAMACPP, backend), 9931)
        self.assertEqual(parse_model(LLAMACPP, backend), "llama-3.1-8B-Q4_K_M")

    def test_ollama_and_omlx_use_their_default_ports(self):
        ollama = identify(OLLAMA)
        self.assertEqual((ollama.key, parse_port(OLLAMA, ollama)), ("ollama", 11434))
        omlx = identify(OMLX)
        self.assertEqual((omlx.key, parse_port(OMLX, omlx)), ("omlx", 8000))

    def test_splash_carries_its_repo_id(self):
        backend = identify(SPLASH)
        self.assertEqual(backend.key, "splash")
        self.assertEqual(parse_port(SPLASH, backend), 8099)
        self.assertEqual(parse_model(SPLASH, backend), "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M")

    def test_unrelated_command_lines_are_ignored(self):
        self.assertIsNone(identify(["python3", "-m", "http.server", "8000"]))
        self.assertIsNone(identify(["ollama", "run", "qwen3:8b"]))
        self.assertIsNone(identify([]))


class EndpointTests(unittest.TestCase):
    def test_url_forms(self):
        self.assertEqual(parse_endpoint("http://127.0.0.1:8000"), ("127.0.0.1", 8000))
        self.assertEqual(parse_endpoint("10.0.0.5:9931/"), ("10.0.0.5", 9931))
        self.assertEqual(parse_endpoint("8099"), ("127.0.0.1", 8099))

    def test_invalid_endpoints(self):
        self.assertIsNone(parse_endpoint(""))
        self.assertIsNone(parse_endpoint("not-a-host"))
        self.assertIsNone(parse_endpoint("host:port"))


def source(*rows):
    return lambda: list(rows)


class RegistryTests(unittest.TestCase):
    def make(self, rows, open_ports=(8001, 30000, 9931, 11434, 8000, 8099, 8002)):
        def prober(host, port):
            return port in open_ports
        return ServiceRegistry(process_source=source(*rows), prober=prober)

    def test_workers_of_one_pod_are_one_service(self):
        import macpm.serviceregistry as module

        original = module._pod_key
        module._pod_key = lambda pid: "pod:a" if pid in (10, 11) else "pod:b"
        try:
            registry = self.make([(10, VLLM_API), (11, VLLM_API),
                                  (20, LLAMACPP)])
            services = registry.discover(force=True)
        finally:
            module._pod_key = original
        by_key = {service.endpoint: service for service in services}

        self.assertEqual(by_key["127.0.0.1:8001"].pids, (10, 11))
        self.assertEqual(by_key["127.0.0.1:9931"].pids, (20,))

    def test_closed_ports_are_not_listed(self):
        registry = self.make([(10, VLLM_API)], open_ports=())
        self.assertEqual(registry.discover(force=True), [])

    def test_manual_endpoint_is_added_once(self):
        registry = ServiceRegistry(
            process_source=source(),
            prober=lambda host, port: True,
            manual=["http://127.0.0.1:8000/", "127.0.0.1:8000", "bogus"],
        )
        services = registry.discover(force=True)

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].backend, "manual")
        self.assertEqual(services[0].source, "manual")

    def test_scan_is_cached_until_the_ttl_expires(self):
        calls = []

        def processes():
            calls.append(1)
            return [(10, VLLM_API)]

        registry = ServiceRegistry(
            process_source=processes,
            prober=lambda host, port: True,
            ttl=1.0,
        )
        now = [0.0]
        registry.clock = lambda: now[0]

        registry.discover()
        registry.discover()
        self.assertEqual(len(calls), 1)

        now[0] = 2.0
        registry.discover()
        self.assertEqual(len(calls), 2)

    def test_a_failing_process_source_is_reported_not_raised(self):
        def boom():
            raise RuntimeError("psutil unavailable")

        registry = ServiceRegistry(process_source=boom, prober=lambda h, p: True)
        self.assertEqual(registry.discover(force=True), [])
        self.assertIn("psutil unavailable", registry.error)

    def test_identity_survives_a_pid_change(self):
        registry = self.make([(10, VLLM_API)])
        first = registry.discover(force=True)[0]
        registry._process_source = source((99, VLLM_API))
        second = registry.discover(force=True)[0]

        self.assertEqual(first.identity, second.identity)
        self.assertNotEqual(first.pids, second.pids)


if __name__ == "__main__":
    unittest.main()
