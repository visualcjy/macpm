import shutil
import tempfile
import unittest

from macpm.linux_backend.power import (
    BatteryReader,
    EnergySampler,
    HwmonPowerReader,
    RaplReader,
    rapl_delta,
)
from macpm.linux_backend.sysfs import SysRoot

from tests import linux_fixtures


class RaplTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-power-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_package_zone_is_selected(self):
        linux_fixtures.build(self.root, "hybrid")
        reader = RaplReader(SysRoot(self.root))

        self.assertTrue(reader.available)
        self.assertEqual(reader.name, "package-0")

    def test_core_subzone_is_preferred_over_package(self):
        linux_fixtures.build(self.root, "hybrid")
        base = "sys/class/powercap/intel-rapl:0:0"
        linux_fixtures._write(self.root, base + "/name", "core\n")
        linux_fixtures._write(self.root, base + "/energy_uj", "500000\n")
        reader = RaplReader(SysRoot(self.root))

        self.assertEqual(reader.name, "core")
        self.assertTrue(reader.energy_path.endswith("intel-rapl:0:0/energy_uj"))

    def test_missing_rapl_is_unavailable(self):
        linux_fixtures.build(self.root, "arm")
        reader = RaplReader(SysRoot(self.root))

        self.assertFalse(reader.available)
        self.assertIsNone(reader.read_uj())

    def test_delta_and_wrap(self):
        self.assertEqual(rapl_delta(1000, 3000, 10000), 2000)
        self.assertEqual(rapl_delta(99000, 1000, 100000), 2000)
        self.assertEqual(rapl_delta(None, 1000, 100000), 0)
        self.assertEqual(rapl_delta(5000, 4000, None), 0)

    def test_energy_sampler_reports_watts(self):
        linux_fixtures.build(self.root, "hybrid")
        sampler = EnergySampler(SysRoot(self.root))
        sampler.prime()

        linux_fixtures._write(
            self.root, "sys/class/powercap/intel-rapl:0/energy_uj", "3000000\n"
        )
        reading = sampler.sample(elapsed=1.0)

        # 2 J over 1 s.
        self.assertAlmostEqual(reading["cpu_w"], 2.0)
        self.assertAlmostEqual(reading["gpu_w"], 12.0)
        self.assertEqual(reading["gpu_source"], "hwmon")

    def test_first_sample_has_no_rate_yet(self):
        linux_fixtures.build(self.root, "hybrid")
        sampler = EnergySampler(SysRoot(self.root))

        reading = sampler.sample(elapsed=1.0)

        self.assertIsNone(reading["cpu_w"])


class HwmonTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-power-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_gpu_and_system_power_are_separated(self):
        linux_fixtures.build(self.root, "hybrid")
        linux_fixtures._write(
            self.root, "sys/class/hwmon/hwmon2/name", "ina2xx\n"
        )
        linux_fixtures._write(
            self.root, "sys/class/hwmon/hwmon2/power1_average", "5000000\n"
        )
        reader = HwmonPowerReader(SysRoot(self.root))

        self.assertEqual(reader.gpu_watts(), 12.0)
        self.assertEqual(reader.system_watts(), 5.0)


class BatteryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-power-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_power_now(self):
        linux_fixtures.build(self.root, "empty")
        linux_fixtures._write(
            self.root, "sys/class/power_supply/BAT0/power_now", "15000000\n"
        )

        self.assertEqual(BatteryReader(SysRoot(self.root)).watts(), 15.0)

    def test_current_times_voltage(self):
        linux_fixtures.build(self.root, "empty")
        linux_fixtures._write(
            self.root, "sys/class/power_supply/BAT0/current_now", "2000000\n"
        )
        linux_fixtures._write(
            self.root, "sys/class/power_supply/BAT0/voltage_now", "12000000\n"
        )

        self.assertEqual(BatteryReader(SysRoot(self.root)).watts(), 24.0)

    def test_no_battery(self):
        linux_fixtures.build(self.root, "empty")

        self.assertIsNone(BatteryReader(SysRoot(self.root)).watts())


if __name__ == "__main__":
    unittest.main()
