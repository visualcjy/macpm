"""Vendor ``*smi`` backends: Iluvatar ixsmi, Denglin dlsmi, Techo teco-smi.

The CSV/pmon samples below were captured from real nodes (BI-V150S, KS58
OCTA, TECO_AICARD_01) so the parsers are exercised against the vendors'
actual output shapes.
"""

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from macpm.capabilities import Capabilities
from macpm.linux_backend.gpu import GpuMonitor, reset_smi_cache
from macpm.linux_backend.sysfs import SysRoot
from macpm.processes import ProcessRow

from tests import linux_fixtures


def write_tool(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env python3\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def tool_script(query, pmon="", apps=""):
    """A fake SMI CLI that answers the three call shapes macpm uses."""
    return (
        "import sys\n"
        f"QUERY = {query!r}\n"
        f"PMON = {pmon!r}\n"
        f"APPS = {apps!r}\n"
        "args = ' '.join(sys.argv[1:])\n"
        "if 'pmon' in args:\n"
        "    sys.stdout.write(PMON)\n"
        "elif 'query-compute-apps' in args or 'query-device=pid' in args:\n"
        "    sys.stdout.write(APPS)\n"
        "else:\n"
        "    sys.stdout.write(QUERY)\n"
    )


IXSMI_QUERY = (
    "0, Iluvatar BI-V150S, 0 %, 29968 MiB, 32768 MiB, 29 C, 13 W, 500 MHz, 1750 MHz\n"
    "1, Iluvatar BI-V150S, 12 %, 2048 MiB, 32768 MiB, 31 C, 54 W, 900 MHz, 1750 MHz\n"
)
IXSMI_PMON = """# gpu        pid    type    sm    mem    enc    dec    jpu    command
# Idx          #     C/G     %      %      %      %      %    name
    0    4242       C    73     92      -      -      -    VLLM::Worker_TP0
    1    4243       C     -     40      -      -      -    VLLM::EngineCore
"""
IXSMI_APPS = """4242, VLLM::Worker_TP0, 29180 MiB
4243, VLLM::EngineCore, 2048 MiB
"""

DLSMI_QUERY = """0, KS58 OCTA-2, 0 %, 12919 MiB, 16384 MiB, 49 C, 5.88 W, 1000 MHz, 1000 MHz
1, KS58 OCTA-3, 0 %, 13039 MiB, 16384 MiB, 48 C, 5.93 W, 1000 MHz, 1000 MHz
2, KS58 OCTA-2, 0 %, 13039 MiB, 16384 MiB, 57 C, 6.75 W, 1000 MHz, 1000 MHz
"""
DLSMI_APPS = """2371162, VLLM::Worker_TP0, 12622 MiB, 0
2371163, VLLM::Worker_TP1, 12742 MiB, 1
"""

TECO_QUERY = """0, TECO_AICARD_01, 0 %, 60737 MB, 65536 MB, 39, 88.000 W, 2360 MHz, 3000 MHz
1, TECO_AICARD_01, 0 %, 60737 MB, 65536 MB, 37, 92.000 W, 2360 MHz, 3000 MHz
2, TECO_AICARD_01, 0 %, 60737 MB, 65536 MB, 40, 90.500 W, 2360 MHz, 3000 MHz
3, TECO_AICARD_01, 0 %, 60727 MB, 65536 MB, 43, 101.500 W, 2360 MHz, 3000 MHz
"""
TECO_PMON = """Device pid Gpu Memory command
Idx # % % name
0 3739689 0 99 VLLM
1 3739690 0 98 VLLM
"""


class VendorTest(unittest.TestCase):
    env_name = ""
    tool_name = ""
    query = ""
    pmon = ""
    apps = ""
    expected_cards = 0

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-smi-")
        linux_fixtures.build(self.root, "empty")
        self.tool = write_tool(
            self.root, self.tool_name, tool_script(self.query, self.pmon, self.apps)
        )
        reset_smi_cache()
        self.env = mock.patch.dict(
            os.environ,
            {
                "MACPM_SYSROOT": self.root,
                "MACPM_PLATFORM": "linux",
                self.env_name: self.tool,
            },
        )
        self.env.start()

    def tearDown(self):
        reset_smi_cache()
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def monitor(self):
        return GpuMonitor.detect(SysRoot(self.root))


class IxsmiTests(VendorTest):
    env_name = "MACPM_IXSMI"
    tool_name = "ixsmi"
    query = IXSMI_QUERY
    pmon = IXSMI_PMON
    apps = IXSMI_APPS
    expected_cards = 2

    def test_every_card_is_enumerated(self):
        monitor = self.monitor()
        samples = monitor.samples(1.0)

        self.assertEqual(monitor.count, self.expected_cards)
        self.assertEqual(samples[0].name, "Iluvatar BI-V150S")
        self.assertEqual([s.busy_pct for s in samples], [0.0, 12.0])
        self.assertEqual([s.freq_mhz for s in samples], [500.0, 900.0])
        self.assertEqual(samples[0].max_freq_mhz, 1750.0)
        self.assertEqual(samples[1].power_w, 54.0)
        self.assertEqual(samples[0].mem_total_mb, 32768.0)
        self.assertEqual(samples[1].temp_c, 31.0)

    def test_processes_come_from_pmon_joined_with_compute_apps(self):
        records = self.monitor().processes()

        self.assertEqual(len(records), 2)
        worker = records[0]
        self.assertEqual(worker["gpus"], [0])
        self.assertEqual(worker["card"], "0")
        self.assertEqual(worker["pid"], 4242)
        self.assertEqual(worker["name"], "VLLM::Worker_TP0")
        self.assertEqual(worker["util_pct"], 73.0)
        self.assertEqual(worker["mem_mb"], 29180.0)
        # pmon reports "-" for the SM percentage of this one.
        self.assertIsNone(records[1]["util_pct"])
        self.assertEqual(records[1]["mem_mb"], 2048.0)

    def test_capabilities_include_processes(self):
        self.assertEqual(
            self.monitor().capabilities(),
            {"util": True, "freq": True, "power": True, "proc": True},
        )
        # Only pmon with a numeric SM column feeds the process view's GPU%.
        self.assertTrue(self.monitor().per_process_supported)


class DlsmiTests(VendorTest):
    env_name = "MACPM_DLSMI"
    tool_name = "dlsmi"
    query = DLSMI_QUERY
    apps = DLSMI_APPS
    expected_cards = 3

    def test_every_card_is_enumerated(self):
        monitor = self.monitor()
        samples = monitor.samples(1.0)

        self.assertEqual(monitor.count, self.expected_cards)
        self.assertEqual({b.driver for b in monitor.backends}, {"denglin"})
        self.assertEqual(samples[0].name, "KS58 OCTA-2")
        self.assertEqual(samples[0].power_w, 5.88)
        self.assertEqual(samples[0].freq_mhz, 1000.0)
        self.assertEqual(samples[2].temp_c, 57.0)

    def test_processes_carry_the_card_index(self):
        records = self.monitor().processes()

        self.assertEqual([r["card"] for r in records], ["0", "1"])
        self.assertEqual([r["pid"] for r in records], [2371162, 2371163])
        self.assertEqual(records[0]["name"], "VLLM::Worker_TP0")
        self.assertEqual(records[0]["mem_mb"], 12622.0)
        self.assertIsNone(records[0]["util_pct"])

    def test_no_sm_counter_is_not_a_process_view_source(self):
        monitor = self.monitor()

        self.assertFalse(monitor.per_process_supported)
        self.assertTrue(monitor.has_vendor_processes)
        self.assertEqual(
            monitor.capabilities(),
            {"util": True, "freq": True, "power": True, "proc": True},
        )


class TecoTests(VendorTest):
    env_name = "MACPM_TECO"
    tool_name = "teco-smi"
    query = TECO_QUERY
    pmon = TECO_PMON
    expected_cards = 4

    def test_every_device_is_enumerated_with_teco_fields(self):
        monitor = self.monitor()
        samples = monitor.samples(1.0)

        self.assertEqual(monitor.count, self.expected_cards)
        self.assertEqual({b.driver for b in monitor.backends}, {"teco"})
        self.assertEqual([s.temp_c for s in samples], [39.0, 37.0, 40.0, 43.0])
        self.assertEqual([s.power_w for s in samples], [88.0, 92.0, 90.5, 101.5])
        self.assertEqual(samples[0].mem_total_mb, 65536.0)
        self.assertEqual(samples[0].freq_mhz, 2360.0)
        self.assertEqual(samples[0].max_freq_mhz, 3000.0)

    def test_processes_come_from_pmon(self):
        records = self.monitor().processes()

        self.assertEqual([r["card"] for r in records], ["0", "1"])
        self.assertEqual([r["pid"] for r in records], [3739689, 3739690])
        self.assertEqual(records[0]["name"], "VLLM")
        self.assertEqual(records[0]["util_pct"], 0.0)
        # teco pmon reports memory as a percentage of the card's 65536 MiB.
        self.assertAlmostEqual(records[0]["mem_mb"], 0.99 * 65536, places=1)


class ProcessGroupingTests(unittest.TestCase):
    def test_same_pid_on_several_cards_becomes_one_row(self):
        from macpm.linux_backend.gpu import GpuMonitor, _format_cards

        self.assertEqual(_format_cards([0, 1, 2, 3]), "0-3")
        self.assertEqual(_format_cards([0, 2]), "0,2")
        self.assertEqual(_format_cards([]), "-")

        with mock.patch(
            "macpm.linux_backend.gpu._smi_processes",
            return_value=[
                {"gpu": 0, "pid": 7, "name": "worker", "util_pct": None, "mem_mb": 100.0},
                {"gpu": 1, "pid": 7, "name": "worker", "util_pct": None, "mem_mb": 200.0},
                {"gpu": 2, "pid": 8, "name": "other", "util_pct": None, "mem_mb": None},
            ],
        ), mock.patch(
            "macpm.linux_backend.gpu.smi_tool", return_value="/bin/true"
        ):
            monitor = GpuMonitor(SysRoot("/nonexistent"), [])
            records = monitor.processes()

        self.assertEqual([(r["pid"], r["card"], r["mem_mb"]) for r in records],
                         [(7, "0-1", 300.0), (8, "2", None)])


class CoexistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-smi-")
        linux_fixtures.build(self.root, "empty")
        self.ixsmi = write_tool(
            self.root, "ixsmi", tool_script(IXSMI_QUERY, IXSMI_PMON, IXSMI_APPS)
        )
        self.dlsmi = write_tool(
            self.root, "dlsmi", tool_script(DLSMI_QUERY, "", DLSMI_APPS)
        )
        reset_smi_cache()
        self.env = mock.patch.dict(
            os.environ,
            {
                "MACPM_SYSROOT": self.root,
                "MACPM_PLATFORM": "linux",
                "MACPM_IXSMI": self.ixsmi,
                "MACPM_DLSMI": self.dlsmi,
            },
        )
        self.env.start()

    def tearDown(self):
        reset_smi_cache()
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_both_vendors_contribute_cards_and_processes(self):
        monitor = GpuMonitor.detect(SysRoot(self.root))

        self.assertEqual(
            sorted(b.driver for b in monitor.backends),
            ["denglin", "denglin", "denglin", "iluvatar", "iluvatar"],
        )
        records = monitor.processes()
        self.assertEqual(len(records), 4)
        self.assertEqual(
            sorted({(r["gpus"][0], r["pid"]) for r in records}),
            [(0, 4242), (0, 2371162), (1, 4243), (1, 2371163)],
        )


class VendorRowRenderTests(unittest.TestCase):
    def test_vendor_rows_render_card_memory_and_a_dash_for_util(self):
        import io
        import contextlib

        from macpm.gpuview import GpuView

        view = GpuView.__new__(GpuView)
        view.color = 2
        view.caps = Capabilities(platform="linux", gpu_util=True)
        view.sort = "gpu"
        view.desc = True
        view.offset = 0
        view.ready = True
        view.paused = False
        view._sorted = None
        view.cards = []
        view.rows = [
            ProcessRow(4242, "VLLM::Worker_TP0", "root", 0.0, 0, 0.0,
                       gpu_card="0", gpu_mem_mb=29180.0, util_known=False),
            ProcessRow(4243, "VLLM::EngineCore", "root", 0.0, 0, 0.0,
                       gpu_card="1", gpu_mem_mb=2048.0, util_known=False),
        ]
        view.sampler = mock.Mock(gpu_available=True)
        view._term = mock.Mock(width=120, height=20, color=lambda v: "",
                               move=lambda r, c: f"\x1b[{r+1};{c+1}H")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display({"gpus": [], "processor": {}}, mock.Mock(interval=1), None)
        output = buffer.getvalue()

        self.assertIn("CARD", output)
        self.assertIn("MEM", output)
        self.assertIn("VLLM::Worker_TP0", output)
        self.assertIn("28G", output)
        self.assertIn("—", output)


if __name__ == "__main__":
    unittest.main()
