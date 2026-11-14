"""NVIDIA backend: identity and compute processes parsed from nvidia-smi.

No NVIDIA hardware is needed — a fake ``nvidia-smi`` produces the CSV shapes the
real tool emits (query-gpu, query-compute-apps, pmon).
"""

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from macpm.linux_backend import gpu as gpu_module
from macpm.linux_backend.gpu import GpuMonitor, reset_nvidia_cache
from macpm.linux_backend.sysfs import SysRoot

from tests import linux_fixtures

ROWS = """0, NVIDIA A100-SXM4-80GB, 87, 1410, 312.4, 71000, 81920, 58
1, NVIDIA A100-SXM4-80GB, 12, 1410, 98.5, 2048, 81920, 41
"""

# driver 580 rejects ``cuda_version`` and answers the fallback (no CUDA) shape
INFO = """NVIDIA A100-SXM4-80GB, 550.54.14, 2, 92.00.36.00.02, 1, 16, 4, 16, 1234567890123
NVIDIA A100-SXM4-80GB, 550.54.14, 2, 92.00.36.00.02, 1, 16, 4, 16, 1234567890123
"""

UUIDS = """0, GPU-aaaa
1, GPU-bbbb
"""

APPS = """4242, python3 /opt/serve.py, 40000 MiB, GPU-aaaa
4243, python3 /opt/train.py, 1024 MiB, GPU-bbbb
"""

PMON = """# gpu        pid  type    sm   mem   enc   dec   command
# Idx          #   C/G     %     %     %     %   name
    0       4242     C    45    30     -     -   python3
    1       4243     C     -    30     -     -   python3
"""

TOOL = """#!/usr/bin/env python3
import sys
args = " ".join(sys.argv[1:])
if "utilization.gpu" in args:
    sys.stdout.write({rows!r})
elif "serial" in args and "cuda_version" in args:
    sys.exit(1)
elif "serial" in args:
    sys.stdout.write({info!r})
elif "index,uuid" in args:
    sys.stdout.write({uuids!r})
elif "query-compute-apps" in args:
    sys.stdout.write({apps!r})
elif "pmon" in args:
    sys.stdout.write({pmon!r})
""".format(rows=ROWS, info=INFO, uuids=UUIDS, apps=APPS, pmon=PMON)


class NvidiaTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-nv-")
        linux_fixtures.build(self.root, "empty")
        self.tool = os.path.join(self.root, "nvidia-smi")
        with open(self.tool, "w", encoding="utf-8") as handle:
            handle.write(TOOL)
        os.chmod(self.tool, os.stat(self.tool).st_mode | stat.S_IEXEC)
        reset_nvidia_cache()
        self.env = mock.patch.dict(
            os.environ,
            {
                "MACPM_SYSROOT": self.root,
                "MACPM_PLATFORM": "linux",
                "MACPM_NVIDIA_SMI": self.tool,
            },
        )
        self.env.start()

    def tearDown(self):
        reset_nvidia_cache()
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def monitor(self):
        return GpuMonitor.detect(SysRoot(self.root))

    def test_cards_are_enumerated(self):
        monitor = self.monitor()
        samples = monitor.samples(1.0)

        self.assertEqual(monitor.count, 2)
        self.assertEqual({backend.driver for backend in monitor.backends}, {"nvidia"})
        self.assertEqual(samples[0].name, "NVIDIA A100-SXM4-80GB")
        self.assertEqual([sample.busy_pct for sample in samples], [87.0, 12.0])
        self.assertEqual(samples[0].power_w, 312.4)
        self.assertEqual(samples[1].temp_c, 41.0)
        self.assertEqual(samples[0].mem_total_mb, 81920.0)

    def test_info_derives_cards_and_dies_from_serials(self):
        info = self.monitor().info()

        self.assertEqual(info["vendor"], "nvidia")
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["cards"], 1)
        self.assertEqual(info["dies"], 2)
        self.assertEqual(info["driver"], "550.54.14")
        self.assertEqual(info["cuda"], "")
        # Falls back to the link maximum (idle cards report gen 1 as current).
        self.assertEqual(info["pcie_gen"], 4)
        self.assertEqual(info["pcie_width"], 16)
        self.assertEqual(info["vbios"], "92.00.36.00.02")

    def test_compute_processes_are_attributed_to_their_card(self):
        records = self.monitor().processes()

        self.assertEqual([r["card"] for r in records], ["0", "1"])
        self.assertEqual([r["pid"] for r in records], [4242, 4243])
        self.assertEqual(records[0]["name"], "python3 /opt/serve.py")
        self.assertEqual(records[0]["mem_mb"], 40000.0)
        self.assertEqual(records[0]["util_pct"], 45.0)
        self.assertIsNone(records[1]["util_pct"])

    def test_capabilities_expose_processes(self):
        self.assertEqual(
            self.monitor().capabilities(),
            {"util": True, "freq": True, "power": True, "proc": True},
        )
        self.assertTrue(self.monitor().has_vendor_processes)

    def test_drm_nodes_do_not_duplicate_the_smi_backend(self):
        for index in range(2):
            linux_fixtures.add_card(
                self.root, index, driver="nvidia", vendor="0x10de",
                busy=None, freq=None, power=None, temp=None,
            )
        monitor = self.monitor()

        self.assertEqual(monitor.count, 2)
        self.assertEqual([b.driver for b in monitor.backends], ["nvidia", "nvidia"])

    def test_missing_nvidia_smi_is_not_an_error(self):
        with mock.patch.dict(os.environ, {"MACPM_NVIDIA_SMI": ""}), mock.patch.object(
            gpu_module, "nvidia_smi_path", return_value=""
        ):
            reset_nvidia_cache()
            monitor = GpuMonitor.detect(SysRoot(self.root))

        self.assertEqual(monitor.count, 0)
        self.assertEqual(monitor.info(), {})
        self.assertEqual(monitor.processes(), [])


if __name__ == "__main__":
    unittest.main()
