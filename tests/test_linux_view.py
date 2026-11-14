"""Headless rendering tests for the Linux default/per-core views.

The TUI draws through dashing/blessed; with ``COLUMNS``/``LINES`` set the
frames render into a pipe, so the panel layout and the capability-driven
hiding (ANE, GPU, power) can be checked without a pty.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests import linux_fixtures

# macpm.macpm parses sys.argv at import time.
_argv, sys.argv = sys.argv, ["macpm"]
try:
    from macpm.capabilities import Capabilities
    from macpm.linux_backend import gpu as gpu_backend
    from macpm.linux_backend.sampler import LinuxSampler
    from macpm.macpm import DefaultView, get_process_summary
finally:
    sys.argv = _argv


def fake_time(monotonic=100.0):
    return types.SimpleNamespace(
        monotonic=lambda: monotonic, time=lambda: 1_700_000_000.0
    )


def linux_caps(**overrides):
    values = dict(
        platform="linux",
        cpu_power=True,
        gpu_util=True,
        gpu_freq=True,
        gpu_power=True,
        ane=False,
        temps=True,
        fans=True,
        proc_gpu=True,
        e_cluster=True,
    )
    values.update(overrides)
    return Capabilities(**values)


class ViewRenderTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-view-")
        linux_fixtures.build(self.root, "hybrid")
        gpu_backend.reset_shared()
        self.patches = [
            mock.patch.dict(os.environ, {"MACPM_SYSROOT": self.root}),
            mock.patch(
                "macpm.linux_backend.cpu.psutil.cpu_percent",
                return_value=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            ),
            mock.patch(
                "macpm.linux_backend.sampler.psutil.disk_io_counters",
                return_value=None,
            ),
            mock.patch(
                "macpm.linux_backend.sampler.psutil.net_io_counters",
                return_value={},
            ),
            mock.patch("macpm.linux_backend.sampler.time", fake_time()),
        ]
        for patch in self.patches:
            patch.start()
        os.environ["COLUMNS"] = "120"
        os.environ["LINES"] = "40"

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        gpu_backend.reset_shared()
        shutil.rmtree(self.root, ignore_errors=True)

    def render(self, sampler, show_cores=False, capabilities=None):
        args = types.SimpleNamespace(
            color=2, show_cores=show_cores, interval=1, avg=30
        )
        view = DefaultView(
            sampler.soc,
            args,
            netinfo=None,
            capabilities=capabilities or sampler.capabilities,
        )
        sample = sampler.sample()
        sensors = types.SimpleNamespace(
            lines=lambda: ["CPU 52.0°C   peak 52.0°C", "GPU 43.0°C   peak 43.0°C",
                           "Fan 1 2318 rpm"],
            fan_levels=lambda: [("F1", 58.0)],
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display(sample, args, sensors)
        return buffer.getvalue()

    def test_linux_view_shows_power_gpu_and_temps(self):
        sampler = LinuxSampler(1)
        sampler.prime()
        output = self.render(sampler)
        sampler.close()

        self.assertIn("Power:", output)
        self.assertIn("GPU Usage", output)
        self.assertIn("E-CPU Usage", output)
        self.assertIn("Temps & Fans", output)
        self.assertIn("Disk IO", output)
        self.assertIn("Network IO", output)
        self.assertNotIn("ANE", output)
        self.assertIn("Intel Core i7-12700H", output)

    def test_view_without_gpu_power_or_e_cores(self):
        sampler = LinuxSampler(1)
        sampler.prime()
        caps = linux_caps(
            cpu_power=False,
            gpu_util=False,
            gpu_freq=False,
            gpu_power=False,
            e_cluster=False,
            temps=False,
            fans=False,
        )
        output = self.render(sampler, capabilities=caps)
        sampler.close()

        self.assertNotIn("GPU Usage", output)
        self.assertNotIn("Power:", output)
        self.assertNotIn("E-CPU Usage", output)
        self.assertNotIn("ANE", output)
        self.assertIn("CPU Usage", output)
        self.assertIn("Temps & Fans", output)

    def test_per_core_view_without_e_cores(self):
        sampler = LinuxSampler(1)
        sampler.prime()
        caps = linux_caps(e_cluster=False, gpu_util=False, ane=False)
        output = self.render(sampler, show_cores=True, capabilities=caps)
        sampler.close()

        self.assertIn("P1", output)
        self.assertNotIn("GPU Usage", output)
        self.assertNotIn("ANE", output)


class SummaryTests(unittest.TestCase):
    def sample(self):
        return {
            "timestamp": 1.0,
            "thermal_pressure": "Nominal",
            "processor": {
                "clusters": [
                    {
                        "name": "P-Cluster",
                        "freq_hz": 3000e6,
                        "idle_ratio": 0.5,
                        "cpus": [
                            {"cpu": index, "freq_hz": 3000e6, "idle_ratio": 0.5}
                            for index in range(4)
                        ],
                    }
                ],
                "ane_energy": 0,
                "cpu_energy": 0,
                "gpu_energy": 0,
                "combined_power": 0,
            },
            "gpu": {"freq_hz": 0, "idle_ratio": 1.0},
        }

    def test_summary_without_gpu_marks_it_na(self):
        caps = linux_caps(gpu_util=False, e_cluster=False)
        lines = get_process_summary(self.sample(), caps)

        self.assertEqual(len(lines), 3)
        self.assertIn("CPU ", lines[0])
        self.assertNotIn("E ", lines[0])
        self.assertIn("GPU n/a", lines[1])
        self.assertIn("cached", lines[2])
        self.assertIn("slab", lines[2])

    def test_summary_with_gpu(self):
        caps = linux_caps(e_cluster=False)
        lines = get_process_summary(self.sample(), caps)

        self.assertIn("GPU 0% @ 0 MHz", lines[1])


class FakeScreen:
    """Minimal curses window: the key queue is what ``begin`` polls."""

    def __init__(self, keys):
        self.keys = list(keys)

    def nodelay(self, flag=True):
        pass

    def keypad(self, flag=True):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else -1


class MainLoopTests(unittest.TestCase):
    """Drive ``begin()`` once on Linux so the whole loop is covered."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-loop-")
        linux_fixtures.build(self.root, "hybrid")
        gpu_backend.reset_shared()
        self.patches = [
            mock.patch.dict(
                os.environ,
                {"MACPM_SYSROOT": self.root, "MACPM_PLATFORM": "linux"},
            ),
            mock.patch(
                "macpm.linux_backend.cpu.psutil.cpu_percent",
                return_value=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            ),
            mock.patch(
                "macpm.linux_backend.sampler.psutil.disk_io_counters",
                return_value=None,
            ),
            mock.patch(
                "macpm.linux_backend.sampler.psutil.net_io_counters",
                return_value={},
            ),
            mock.patch("macpm.linux_backend.sampler.time", fake_time()),
        ]
        for patch in self.patches:
            patch.start()
        os.environ["COLUMNS"] = "120"
        os.environ["LINES"] = "40"

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        gpu_backend.reset_shared()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_begin_runs_view_one_three_and_exits(self):
        from macpm import macpm as module
        from macpm.linux_backend.sampler import LinuxSampler

        sampler = LinuxSampler(0.05)
        # One key per outer iteration: the loop drains keys until getch()
        # returns -1, so a burst would be handled before anything is drawn.
        screen = FakeScreen(
            [-1, ord("1"), -1, -1, ord("3"), -1, -1, ord("5"), -1, -1, ord("q")]
        )
        old_interval = module.args.interval
        old_public = module.args.no_public_ip
        module.args.interval = 0.05
        module.args.no_public_ip = True
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                module.begin(screen, sampler)
        finally:
            module.args.interval = old_interval
            module.args.no_public_ip = old_public
            sampler.close()

        output = buffer.getvalue()
        self.assertIn("E-CPU Usage", output)
        self.assertIn("GPU Usage", output)
        self.assertIn("proc ", output)
        self.assertIn("processes by GPU", output)
        self.assertIn("VRAM", output)
        self.assertIn("Stopping", output)


if __name__ == "__main__":
    unittest.main()
