import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from macpm import backend
from macpm.linux_backend import gpu as gpu_backend
from macpm.linux_backend.sampler import LinuxSampler

from tests import linux_fixtures


def counter(read_count, write_count, read_bytes, write_bytes):
    return SimpleNamespace(
        read_count=read_count,
        write_count=write_count,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
    )


def nic(sent, received):
    return SimpleNamespace(bytes_sent=sent, bytes_recv=received)


def fake_time(*ticks):
    """Patch sampler.time only, so nothing else in the process sees the mock."""
    values = iter(ticks)
    return SimpleNamespace(monotonic=lambda: next(values), time=lambda: 1_700_000_000.0)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-sampler-")
        linux_fixtures.build(self.root, "hybrid")
        gpu_backend.reset_shared()
        self.patches = [
            mock.patch.dict(os.environ, {"MACPM_SYSROOT": self.root}),
            mock.patch(
                "macpm.linux_backend.cpu.psutil.cpu_percent",
                return_value=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        gpu_backend.reset_shared()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_sample_has_the_macpm_shape(self):
        with mock.patch(
            "macpm.linux_backend.sampler.psutil.disk_io_counters",
            return_value={},
        ), mock.patch(
            "macpm.linux_backend.sampler.psutil.net_io_counters",
            return_value={},
        ):
            sampler = LinuxSampler(1)
            sampler.prime()
            sample = sampler.sample()
            sampler.close()

        self.assertIn("processor", sample)
        self.assertIn("gpu", sample)
        self.assertIn("disk", sample)
        self.assertIn("network", sample)
        self.assertEqual(
            [cluster["name"] for cluster in sample["processor"]["clusters"]],
            ["E-Cluster", "P-Cluster"],
        )
        self.assertEqual(sample["gpu"]["freq_hz"], 1200)
        self.assertEqual(sample["gpu"]["mem_total_mb"], 8192.0)
        self.assertEqual(sample["gpu"]["mem_used_mb"], 2048.0)
        self.assertEqual(sample["gpu"]["temp_c"], 43.0)
        self.assertAlmostEqual(sample["gpu"]["idle_ratio"], 0.63)
        self.assertEqual(sample["disk"]["rops_per_s"], 0)
        self.assertEqual(sample["network"]["ibyte_rate"], 0)

    def test_energy_and_disk_rates(self):
        disks = iter(
            [
                {
                    "sda": counter(10, 5, 1000, 500),
                    "sda1": counter(10, 5, 1000, 500),
                    "loop0": counter(99, 99, 9999, 9999),
                },
                {
                    "sda": counter(20, 8, 3000, 900),
                    "sda1": counter(20, 8, 3000, 900),
                    "loop0": counter(120, 120, 19999, 19999),
                },
            ]
        )
        nets = iter(
            [
                {
                    "eth0": nic(100, 200),
                    "lo": nic(999, 999),
                    "docker0": nic(50, 50),
                },
                {
                    "eth0": nic(1100, 1200),
                    "lo": nic(999, 999),
                    "docker0": nic(50, 50),
                },
            ]
        )
        with mock.patch(
            "macpm.linux_backend.sampler.psutil.disk_io_counters",
            side_effect=lambda perdisk=True: next(disks),
        ), mock.patch(
            "macpm.linux_backend.sampler.psutil.net_io_counters",
            side_effect=lambda pernic=True: next(nets),
        ), mock.patch(
            "macpm.linux_backend.sampler.time", fake_time(100.0, 101.0)
        ):
            sampler = LinuxSampler(1)
            sampler.prime()
            linux_fixtures._write(
                self.root,
                "sys/class/powercap/intel-rapl:0/energy_uj",
                "3000000\n",
            )
            sample = sampler.sample()
            sampler.close()

        processor = sample["processor"]
        # 2 J in 1 s -> 2 W -> 2000 mJ per interval.
        self.assertAlmostEqual(processor["cpu_energy"], 2000.0)
        self.assertAlmostEqual(processor["gpu_energy"], 12000.0)
        self.assertTrue(processor["cpu_energy_available"])
        self.assertTrue(processor["gpu_energy_available"])
        self.assertFalse(processor["ane_energy_available"])
        self.assertTrue(processor["power_source"].startswith("rapl"))

        # Only the physical disk/interface is counted (not the partition,
        # loop device, loopback or docker bridge): 1 s of deltas.
        self.assertEqual(sample["disk"]["rops_per_s"], 10)
        self.assertEqual(sample["disk"]["wops_per_s"], 3)
        self.assertEqual(sample["disk"]["rbytes_per_s"], 2000)
        self.assertEqual(sample["network"]["ibyte_rate"], 1000)
        self.assertEqual(sample["network"]["obyte_rate"], 1000)

    def test_multi_gpu_sample_lists_every_card(self):
        linux_fixtures.build(self.root, "multi")
        gpu_backend.reset_shared()
        with mock.patch(
            "macpm.linux_backend.sampler.psutil.disk_io_counters",
            return_value=None,
        ), mock.patch(
            "macpm.linux_backend.sampler.psutil.net_io_counters",
            return_value={},
        ), mock.patch(
            "macpm.linux_backend.sampler.time", fake_time(100.0, 101.0)
        ):
            sampler = LinuxSampler(1)
            sampler.prime()
            sample = sampler.sample()
            sampler.close()

        cards = sample["gpus"]
        self.assertEqual(len(cards), 4)
        self.assertEqual([card["busy_pct"] for card in cards], [10.0, 35.0, 60.0, 85.0])
        self.assertEqual(sample["gpu"]["count"], 4)
        # The single-card field points at the busiest card.
        self.assertEqual(sample["gpu"]["busy_pct"], 85.0)
        self.assertEqual(sample["gpu"]["index"], 3)
        # Per-card power is summed (4 x 12 W) for the power panel.
        self.assertAlmostEqual(sample["processor"]["gpu_energy"], 48000.0)

    def test_capabilities_on_a_hybrid_laptop(self):
        with mock.patch(
            "macpm.linux_backend.sampler.psutil.disk_io_counters",
            return_value={},
        ), mock.patch(
            "macpm.linux_backend.sampler.psutil.net_io_counters",
            return_value={},
        ):
            sampler = LinuxSampler(1)
            caps = sampler.capabilities
            sampler.close()

        self.assertEqual(caps.platform, "linux")
        self.assertTrue(caps.cpu_freq)
        self.assertTrue(caps.e_cluster)
        self.assertTrue(caps.cpu_power)
        self.assertTrue(caps.gpu_util)
        self.assertTrue(caps.gpu_freq)
        self.assertTrue(caps.temps)
        self.assertTrue(caps.fans)
        self.assertTrue(caps.proc_gpu)
        self.assertFalse(caps.ane)

    def test_everything_degrades_to_na_without_sysfs(self):
        linux_fixtures.build(self.root, "empty")
        gpu_backend.reset_shared()
        with mock.patch(
            "macpm.linux_backend.sampler.psutil.disk_io_counters",
            return_value=None,
        ), mock.patch(
            "macpm.linux_backend.sampler.psutil.net_io_counters",
            return_value={},
        ), mock.patch(
            "macpm.linux_backend.sampler.time",
            fake_time(100.0, 101.0, 102.0, 103.0, 104.0),
        ):
            sampler = LinuxSampler(1)
            sampler.prime()
            sample = None
            # Four samples: the energy grace period (3) must run out so a
            # counter that never reports turns into n/a.
            for _ in range(4):
                sample = sampler.sample()
            caps = sampler.capabilities
            sampler.close()

        self.assertFalse(caps.cpu_freq)
        self.assertFalse(caps.cpu_power)
        self.assertFalse(caps.gpu_util)
        self.assertFalse(caps.temps)
        self.assertFalse(caps.ane)
        processor = sample["processor"]
        self.assertFalse(processor["cpu_energy_available"])
        self.assertFalse(processor["gpu_energy_available"])
        self.assertEqual(sample["gpu"]["freq_hz"], 0)
        self.assertEqual(sample["gpu"]["idle_ratio"], 1.0)
        self.assertEqual(sample["disk"]["rops_per_s"], 0)


class BackendSelectionTests(unittest.TestCase):
    def test_platform_name(self):
        self.assertEqual(backend.platform_name(), "macos")
        self.assertFalse(backend.is_linux())

    def test_macos_capabilities_report_everything(self):
        caps = backend.macos_capabilities()

        self.assertEqual(caps.platform, "macos")
        self.assertEqual(caps.missing(), [])

    def test_sampler_factory_selects_the_linux_backend(self):
        root = tempfile.mkdtemp(prefix="macpm-sampler-")
        linux_fixtures.build(root, "empty")
        gpu_backend.reset_shared()
        try:
            with mock.patch.dict(os.environ, {"MACPM_SYSROOT": root}), mock.patch(
                "macpm.backend.is_linux", return_value=True
            ), mock.patch(
                "macpm.linux_backend.sampler.psutil.disk_io_counters",
                return_value=None,
            ), mock.patch(
                "macpm.linux_backend.sampler.psutil.net_io_counters",
                return_value={},
            ):
                sampler = backend.create_sampler(1)
                self.assertEqual(type(sampler).__name__, "LinuxSampler")
                sampler.close()
        finally:
            gpu_backend.reset_shared()
            shutil.rmtree(root, ignore_errors=True)

    def test_dump_capabilities_json(self):
        from macpm import dump

        root = tempfile.mkdtemp(prefix="macpm-sampler-")
        linux_fixtures.build(root, "empty")
        gpu_backend.reset_shared()
        try:
            with mock.patch.dict(os.environ, {"MACPM_SYSROOT": root}), mock.patch(
                "macpm.backend.is_linux", return_value=True
            ), mock.patch(
                "macpm.linux_backend.sampler.psutil.disk_io_counters",
                return_value=None,
            ), mock.patch(
                "macpm.linux_backend.sampler.psutil.net_io_counters",
                return_value={},
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = dump.main(["--capabilities"])
            payload = json.loads(buffer.getvalue())
        finally:
            gpu_backend.reset_shared()
            shutil.rmtree(root, ignore_errors=True)

        self.assertEqual(code, 0)
        self.assertIn("capabilities", payload)
        self.assertEqual(payload["macpm_platform"], "macos")


class LazyImportTests(unittest.TestCase):
    def test_importing_the_tui_does_not_touch_macos_frameworks(self):
        """Importing macpm on Linux must not dlopen IOReport/AppleSMC."""
        code = (
            "import sys, macpm.macpm, macpm.processes, macpm.sensors; "
            "bad = [name for name in ('macpm.ioreport', 'macpm.smc') "
            "if name in sys.modules]; "
            "assert not bad, bad"
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        environment = dict(os.environ)
        environment["PYTHONPATH"] = root
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
