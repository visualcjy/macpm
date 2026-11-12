import contextlib
import io
import sys
import types
import unittest

# macpm.macpm parses sys.argv at import time.
_argv, sys.argv = sys.argv, ["macpm"]
try:
    from macpm.macpm import DefaultView, SensorPanel, _fit_title, get_process_summary
finally:
    sys.argv = _argv

from macpm.netinfo import LocalNetwork, PublicNetwork


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


class FakeTerminal:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.normal = ""

    def color(self, value):
        return ""

    def move(self, row, column):
        return f"\x1b[{row + 1};{column + 1}H"


class DefaultViewExtrasTests(unittest.TestCase):
    def make_view(self, show_cores=False, gauges=2, items=5):
        view = DefaultView.__new__(DefaultView)
        view.show_cores = show_cores
        view.ui = types.SimpleNamespace(items=[None] * items)
        view.processor_gauges = [None] * gauges
        view.netinfo = None
        view._term = None
        return view

    def test_spare_row_in_the_standard_layout(self):
        view = self.make_view()

        self.assertEqual(view._processor_spare_row(30), 3)
        self.assertIsNone(view._processor_spare_row(24))
        self.assertEqual(view._processor_spare_row(36), 5)

    def test_spare_row_in_the_per_core_layout(self):
        view = self.make_view(show_cores=True, gauges=6, items=2)

        self.assertEqual(view._processor_spare_row(30), 25)

    def test_processor_width_in_both_layouts(self):
        view = self.make_view(show_cores=True, gauges=6, items=2)
        self.assertEqual(view._processor_width(120), 60)

        view = self.make_view()
        self.assertEqual(view._processor_width(120), 120)

    def test_fan_bars_omit_the_percentage(self):
        panel = SensorPanel(color=2)
        panel.levels = [("F1", 50.0), ("F2", 100.0)]

        line = panel._fan_bars(30)

        self.assertNotIn("%", line)
        self.assertIn("F1", line)
        self.assertIn("F2", line)
        self.assertLessEqual(len(line), 30)

    def test_extras_show_identity_and_footer(self):
        view = self.make_view()
        view._term = FakeTerminal(80, 30)
        view.netinfo = types.SimpleNamespace(
            snapshot=lambda: (
                LocalNetwork("en0", "192.168.1.23", 24, "192.168.1.1"),
                PublicNetwork("203.0.113.45", "Shanghai", "", "CN"),
            )
        )

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view._draw_extras(types.SimpleNamespace(color=2))
        output = buffer.getvalue()

        self.assertIn("192.168.1.23/24", output)
        self.assertIn("203.0.113.45", output)
        self.assertIn("q quit", output)


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
