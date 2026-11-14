"""Wiring tests: the platform factory picks the Linux pieces end to end."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from macpm import backend
from macpm.linux_backend import gpu as gpu_backend
from macpm.linux_backend.net import LinuxNetworkSampler
from macpm.sensors import SensorReader

from tests import linux_fixtures


class NetworkFactoryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-wiring-")
        linux_fixtures.build(self.root, "hybrid")
        gpu_backend.reset_shared()

    def tearDown(self):
        gpu_backend.reset_shared()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_linux_network_sampler_is_selected(self):
        with mock.patch.dict(os.environ, {"MACPM_SYSROOT": self.root}), mock.patch(
            "macpm.backend.is_linux", return_value=True
        ):
            sampler = backend.create_network_sampler()

        self.assertIsInstance(sampler, LinuxNetworkSampler)

    def test_macos_network_sampler_is_still_the_default_here(self):
        sampler = backend.create_network_sampler()
        try:
            self.assertEqual(type(sampler).__name__, "NetworkSampler")
        finally:
            sampler.reset()


class SensorWiringTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-wiring-")
        linux_fixtures.build(self.root, "hybrid")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_linux_reader_uses_hwmon(self):
        with mock.patch.dict(
            os.environ, {"MACPM_SYSROOT": self.root, "MACPM_PLATFORM": "linux"}
        ):
            reader = SensorReader("linux")
            reader.read()
            lines = reader.lines()
            levels = reader.fan_levels()
            reader.close()

        self.assertEqual(lines[0], "CPU 52.0°C   peak 52.0°C")
        self.assertEqual(lines[1], "GPU 43.0°C   peak 43.0°C")
        self.assertIn("Fan 1 2318 rpm", lines[2])
        self.assertEqual(levels, [("F1", 57.95)])

    def test_linux_reader_reports_missing_sensors(self):
        linux_fixtures.build(self.root, "empty")
        with mock.patch.dict(
            os.environ, {"MACPM_SYSROOT": self.root, "MACPM_PLATFORM": "linux"}
        ):
            reader = SensorReader("linux")
            reader.read()
            lines = reader.lines()
            reader.close()

        self.assertEqual(lines[0], "sensors unavailable")


class ProcessGpuWiringTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-wiring-")
        linux_fixtures.build(self.root, "hybrid")
        gpu_backend.reset_shared()

    def tearDown(self):
        gpu_backend.reset_shared()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_linux_process_sampler_attaches_the_fdinfo_reader(self):
        with mock.patch.dict(
            os.environ, {"MACPM_SYSROOT": self.root, "MACPM_PLATFORM": "linux"}
        ):
            from macpm.processes import ProcessSampler

            sampler = ProcessSampler()

        self.assertTrue(sampler.gpu_available)
        self.assertEqual(sampler._gpu_times(), {})

    def test_no_gpu_means_no_gpu_column(self):
        linux_fixtures.build(self.root, "arm")
        gpu_backend.reset_shared()
        with mock.patch.dict(
            os.environ, {"MACPM_SYSROOT": self.root, "MACPM_PLATFORM": "linux"}
        ):
            from macpm.processes import ProcessSampler

            sampler = ProcessSampler()

        self.assertFalse(sampler.gpu_available)


if __name__ == "__main__":
    unittest.main()
