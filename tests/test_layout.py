import sys
import unittest

# macpm.macpm parses sys.argv at import time.
_argv, sys.argv = sys.argv, ["macpm"]
try:
    from macpm.macpm import _fit_title, get_process_summary
finally:
    sys.argv = _argv


class FitTitleTests(unittest.TestCase):
    def test_short_title_is_unchanged(self):
        self.assertEqual(_fit_title("Power", 20), "Power")

    def test_title_that_fits_exactly_is_unchanged(self):
        self.assertEqual(_fit_title("Power", 5), "Power")

    def test_long_title_is_clipped_with_ellipsis(self):
        title = _fit_title("CPU+GPU+ANE Power: 4.20W (avg: 4.20W)", 12)

        self.assertEqual(len(title), 12)
        self.assertTrue(title.endswith("…"))
        self.assertTrue(title.startswith("CPU+GPU+"))

    def test_tiny_widths(self):
        self.assertEqual(_fit_title("Power", 0), "")
        self.assertEqual(_fit_title("Power", 1), "…")


class ProcessSummaryTests(unittest.TestCase):
    def test_summary_has_cpu_gpu_and_ram(self):
        parse = {
            "timestamp": 1.0,
            "thermal_pressure": "Nominal",
            "processor": {
                "clusters": [
                    {
                        "name": "E-Cluster",
                        "freq_hz": 2000e6,
                        "idle_ratio": 0.5,
                        "cpus": [
                            {"cpu": i, "freq_hz": 2000e6, "idle_ratio": 0.5}
                            for i in range(4)
                        ],
                    },
                    {
                        "name": "P-Cluster",
                        "freq_hz": 3200e6,
                        "idle_ratio": 0.7,
                        "cpus": [
                            {"cpu": i, "freq_hz": 3200e6, "idle_ratio": 0.7}
                            for i in range(6)
                        ],
                    },
                ],
                "ane_energy": 0,
                "cpu_energy": 0,
                "gpu_energy": 0,
                "combined_power": 0,
            },
            "gpu": {"freq_hz": 1200, "idle_ratio": 0.8},
        }

        lines = get_process_summary(parse)

        self.assertEqual(len(lines), 3)
        self.assertIn("CPU ", lines[0])
        self.assertIn("@ 2000 MHz", lines[0])
        self.assertIn("@ 3200 MHz", lines[0])
        self.assertIn("GPU ", lines[1])
        self.assertIn("@ 1200 MHz", lines[1])
        self.assertIn("RAM", lines[1])
        self.assertIn("swap", lines[1])
        self.assertIn("wired", lines[2])
        self.assertIn("active", lines[2])
        self.assertIn("inactive", lines[2])


if __name__ == "__main__":
    unittest.main()
