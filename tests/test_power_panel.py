"""Power panel titles: component naming, n/a and the warming dash."""

import sys
import unittest

_argv, sys.argv = sys.argv, ["macpm"]
try:
    from macpm.macpm import power_label
finally:
    sys.argv = _argv


class PowerLabelTests(unittest.TestCase):
    def test_both_counters_report_the_plain_package_power(self):
        self.assertEqual(power_label(True, True), "Power")

    def test_missing_cpu_names_the_remainder(self):
        self.assertEqual(power_label(False, True), "Power (GPU only)")

    def test_missing_gpu_names_the_remainder(self):
        self.assertEqual(power_label(True, False), "Power (CPU only)")

    def test_neither_counter_falls_back_to_the_plain_name(self):
        self.assertEqual(power_label(False, False), "Power")


if __name__ == "__main__":
    unittest.main()
