import shutil
import tempfile
import unittest

from macpm.linux_backend.sysfs import SysRoot
from macpm.linux_backend.thermal import ThermalPressure, ThermalReader

from tests import linux_fixtures


class ThermalReaderTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-thermal-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cpu_gpu_and_fan_are_classified(self):
        linux_fixtures.build(self.root, "hybrid")
        sample = ThermalReader(SysRoot(self.root)).read()

        self.assertEqual(sample.cpu_c, 52.0)
        self.assertEqual(sample.gpu_c, 43.0)
        self.assertEqual(len(sample.fans), 1)
        fan = sample.fans[0]
        self.assertEqual(fan.rpm, 2318.0)
        self.assertEqual(fan.max_rpm, 4000.0)
        self.assertIsNone(sample.error)

    def test_arm_board_sensor_is_used_as_cpu(self):
        linux_fixtures.build(self.root, "arm")
        sample = ThermalReader(SysRoot(self.root)).read()

        self.assertEqual(sample.cpu_c, 46.0)
        self.assertIsNone(sample.gpu_c)
        self.assertEqual(sample.fans, [])

    def test_no_sensors_reports_an_error(self):
        linux_fixtures.build(self.root, "empty")
        sample = ThermalReader(SysRoot(self.root)).read()

        self.assertIsNone(sample.cpu_c)
        self.assertIsNone(sample.gpu_c)
        self.assertEqual(sample.error, "no hwmon sensors")

    def test_implausible_values_are_ignored(self):
        linux_fixtures.build(self.root, "hybrid")
        linux_fixtures._write(
            self.root, "sys/class/hwmon/hwmon0/temp1_input", "200000\n"
        )
        sample = ThermalReader(SysRoot(self.root)).read()

        # 200 C is out of range, so the next coretemp sensor (48 C) wins.
        self.assertEqual(sample.cpu_c, 48.0)


class ThermalPressureTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-thermal-")
        linux_fixtures.build(self.root, "hybrid")
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_low_pressure_is_nominal(self):
        reader = ThermalPressure(self.sys)
        reader.prime()

        self.assertEqual(reader.read(), "Nominal")

    def test_high_pressure_is_critical(self):
        reader = ThermalPressure(self.sys)
        reader.prime()
        linux_fixtures._write(
            self.root,
            "proc/pressure/cpu",
            "some avg10=62.00 avg60=40.00 avg300=10.00 total=1\n",
        )

        self.assertEqual(reader.read(), "Critical")

    def test_throttle_counter_increase_is_moderate(self):
        linux_fixtures._write(
            self.root,
            "sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count",
            "1\n",
        )
        reader = ThermalPressure(self.sys)
        reader.prime()
        linux_fixtures._write(
            self.root,
            "sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count",
            "3\n",
        )

        self.assertEqual(reader.read(), "Moderate")

    def test_missing_psi_stays_nominal(self):
        linux_fixtures.build(self.root, "arm")
        reader = ThermalPressure(SysRoot(self.root))
        reader.prime()

        self.assertEqual(reader.read(), "Nominal")


if __name__ == "__main__":
    unittest.main()
