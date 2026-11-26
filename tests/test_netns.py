"""Container namespaces: ports read from /proc/<pid>/net/tcp."""

import os
import tempfile
import unittest

from macpm.serviceregistry import Service, ServiceRegistry, netns_listeners

# 0A = LISTEN. Local address is little-endian hex.
TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1
   1: 510000AC:7530 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2
   2: 0100007F:D431 0100007F:1F40 01 00000000:00000000 00:00000000 00000000     0        0 3
"""


class NetnsTests(unittest.TestCase):
    def test_listen_entries_are_parsed(self):
        root = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(root, "42", "net"))
            with open(os.path.join(root, "42", "net", "tcp"), "w") as handle:
                handle.write(TCP)
            pairs = netns_listeners(42, root)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertIn(("0.0.0.0", 8000), pairs)
        self.assertIn(("172.0.0.81", 30000), pairs)
        self.assertNotIn(("127.0.0.1", 54321), pairs)   # not LISTEN

    def test_a_gpu_service_gets_the_endpoint(self):
        registry = ServiceRegistry(
            process_source=lambda: [],
            prober=lambda host, port: port == 30000,
            gpu_source=lambda: [],
        )
        service = Service("sglang", "prometheus", "127.0.0.1", 0,
                          "Qwen3.8-27B-FP8", (42,), source="gpu")
        registry._prober = lambda host, port: True
        registry._fill_endpoint.__globals__["netns_listeners"] = lambda pid: [("172.0.0.81", 30000)]
        try:
            registry._fill_endpoint(service)
        finally:
            import importlib
            import macpm.serviceregistry as module
            importlib.reload(module)
            registry._fill_endpoint = module.ServiceRegistry._fill_endpoint.__get__(registry)

        self.assertEqual((service.host, service.port), ("172.0.0.81", 30000))


if __name__ == "__main__":
    unittest.main()
