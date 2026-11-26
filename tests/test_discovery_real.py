"""The exact shape seen on the 8x4090 k8s master, replayed as a fixture."""

import unittest

import macpm.serviceregistry as module
from macpm.serviceregistry import ServiceRegistry

NETNS = {1297436: "n1", 1296642: "n1", 3791241: "n2", 3792296: "n2",
         3792482: "n2", 3551255: "n3"}
PORTS = {"n1": [("0.0.0.0", 8000)], "n2": [("0.0.0.0", 30000)],
         "n3": [("0.0.0.0", 8000)]}
ADDRESS = {"n1": "172.21.203.136", "n2": "172.21.203.134",
           "n3": "172.21.203.147"}
IDENTITY = {
    ("172.21.203.136", 8000): (True, "Qwen3-Embedding-8B", "vllm"),
    ("172.21.203.134", 30000): (True, "Qwen3.8-27B-FP8", "sglang"),
    ("172.21.203.147", 8000): (True, "Qwen3-Reranker-8B", "vllm"),
}

PROCESSES = [
    (1296642, ["python3", "-m", "vllm.entrypoints.openai.api_server",
               "--model", "Qwen3-Embedding-8B", "--port", "8000"]),
]
GPU = [
    {"pid": 1297436, "name": "VLLM::EngineCore", "gpus": [2],
     "mem_mb": 21000, "model": "Qwen3-Embedding-8B"},
    {"pid": 3791241, "name": "python3", "gpus": [3], "mem_mb": 400,
     "model": "Qwen3.8-27B-FP8"},
    {"pid": 3792296, "name": "sglang::scheduler_TP0", "gpus": [3],
     "mem_mb": 23000, "model": "Qwen3.8-27B-FP8"},
    {"pid": 3792482, "name": "sglang::scheduler_TP1", "gpus": [4],
     "mem_mb": 23000, "model": "Qwen3.8-27B-FP8"},
    {"pid": 3551255, "name": "VLLM::EngineCore", "gpus": [7],
     "mem_mb": 20000, "model": "Qwen3-Reranker-8B"},
]


class RealFixtureTests(unittest.TestCase):
    def setUp(self):
        self.saved = (module._pod_key, module._candidate_ports,
                      module.netns_address, module._api_identity)
        module._pod_key = lambda pid: NETNS.get(pid, "pid:%d" % pid)
        module._candidate_ports = lambda pid: PORTS[NETNS[pid]]
        module.netns_address = lambda pid: ADDRESS[NETNS[pid]]
        module._api_identity = (lambda host, port: IDENTITY.get(
            (host, port), (False, "", "")))
        self.registry = ServiceRegistry(
            process_source=lambda: list(PROCESSES),
            prober=lambda host, port: True,
            gpu_source=lambda: list(GPU),
            validate_api=True,
        )

    def tearDown(self):
        (module._pod_key, module._candidate_ports,
         module.netns_address, module._api_identity) = self.saved

    def test_three_services_not_five_rows(self):
        services = self.registry.discover(force=True)
        found = {(s.backend, s.endpoint, s.model) for s in services}

        self.assertEqual(found, {
            ("vllm", "172.21.203.136:8000", "Qwen3-Embedding-8B"),
            ("vllm", "172.21.203.147:8000", "Qwen3-Reranker-8B"),
            ("sglang", "172.21.203.134:30000", "Qwen3.8-27B-FP8"),
        })
        sglang = [s for s in services if s.backend == "sglang"][0]
        self.assertEqual(sglang.pids, (3791241, 3792296, 3792482))


if __name__ == "__main__":
    unittest.main()
