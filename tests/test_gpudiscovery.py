"""GPU-table driven discovery: services without a known API port."""

import unittest

from macpm.serviceregistry import ServiceRegistry, gpu_services


ROWS = [
    {"pid": 11, "name": "VLLM::EngineCore", "gpus": [2], "mem_mb": 21000,
     "model": "Qwen3-Embedding-8B"},
    {"pid": 12, "name": "VLLM::EngineCore", "gpus": [7], "mem_mb": 20000,
     "model": "Qwen3-Reranker-8B"},
    {"pid": 13, "name": "sglang::scheduler_TP0", "gpus": [3], "mem_mb": 23000,
     "model": "Qwen3.8-27B-FP8"},
    {"pid": 14, "name": "sglang::scheduler_TP1", "gpus": [4], "mem_mb": 23000,
     "model": "Qwen3.8-27B-FP8"},
]


class GpuDiscoveryTests(unittest.TestCase):
    def test_rows_become_services_deduplicated_by_model(self):
        services = gpu_services(lambda: ROWS)

        models = sorted(service.model for service in services)
        self.assertEqual(models, ["Qwen3-Embedding-8B", "Qwen3-Reranker-8B",
                                  "Qwen3.8-27B-FP8", "Qwen3.8-27B-FP8"])
        self.assertTrue(all(service.endpoint == "?" for service in services))

    def test_registry_keeps_them_without_probing(self):
        registry = ServiceRegistry(
            process_source=lambda: [],
            prober=lambda host, port: False,
            gpu_source=lambda: ROWS,
        )
        services = registry.discover(force=True)

        self.assertEqual(len(services), 4)
        self.assertTrue(all(service.source == "gpu" for service in services))


if __name__ == "__main__":
    unittest.main()


class ApiValidationTests(unittest.TestCase):
    def test_services_without_an_api_are_dropped(self):
        registry = ServiceRegistry(
            process_source=lambda: [],
            prober=lambda host, port: True,
            container_source=lambda: [("vllm", 9000, "c", "vllm/x")],
            validate_api=True,
            api_probe=lambda host, port: False,
        )
        self.assertEqual(registry.discover(force=True), [])

    def test_a_live_api_is_kept(self):
        registry = ServiceRegistry(
            process_source=lambda: [],
            prober=lambda host, port: True,
            container_source=lambda: [("vllm", 9000, "c", "vllm/x")],
            validate_api=True,
            api_probe=lambda host, port: True,
        )
        self.assertEqual(len(registry.discover(force=True)), 1)
