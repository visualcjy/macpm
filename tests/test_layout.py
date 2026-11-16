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


def render_grid(output, width, height):
    """Replay cursor-move/text output into a character grid."""
    grid = [[" "] * width for _ in range(height)]
    row = column = index = 0
    while index < len(output):
        if output[index] == "\x1b":
            end = index + 1
            while end < len(output) and not output[end].isalpha():
                end += 1
            if end < len(output) and output[end] == "H":
                sequence = output[index + 2:end]
                if ";" in sequence:
                    row_text, column_text = sequence.split(";")
                    row, column = int(row_text) - 1, int(column_text) - 1
                index = end + 1
                continue
            index = end + 1
            continue
        character = output[index]
        if character == "\n":
            row, column = row + 1, 0
        elif 0 <= row < height and 0 <= column < width:
            grid[row][column] = character
            column += 1
        else:
            column += 1
        index += 1
    return ["".join(line) for line in grid]


class DefaultViewExtrasTests(unittest.TestCase):
    def make_view(self, show_cores=False, gauges=2, items=5):
        view = DefaultView.__new__(DefaultView)
        view.show_cores = show_cores
        view.ui = types.SimpleNamespace(items=[None] * items)
        view.processor_gauges = [None] * gauges
        view.netinfo = None
        view._term = None
        return view

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

    def test_extras_draw_a_plain_footer(self):
        view = self.make_view()
        view._term = FakeTerminal(80, 30)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view._draw_extras(types.SimpleNamespace(color=2))
        grid = render_grid(buffer.getvalue(), 80, 30)

        self.assertTrue(grid[29].startswith("  macpm"), grid[29])
        self.assertIn("q quit", grid[29])
        self.assertNotIn("└", grid[29])
        self.assertNotIn("┘", grid[29])

    def test_identity_rides_the_network_box_bottom_border(self):
        from macpm.blessed_engine import render_tiles
        from macpm.widgets import HSplit

        tile = HSplit(title="Network IO", border_color=2)
        tile.bottom_title = " en0 192.168.1.23/24  ·  203.0.113.45 Shanghai "
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            render_tiles(tile, FakeTerminal(70, 5), "")
        grid = render_grid(buffer.getvalue(), 70, 5)

        self.assertTrue(
            grid[3].startswith("└ en0 192.168.1.23/24"), grid[3]
        )
        self.assertTrue(grid[3].endswith("┘"), grid[3])

    def test_side_by_side_gauges_keep_a_gutter(self):
        from macpm.blessed_engine import render_tiles
        from macpm.widgets import HGauge, HSplit

        tile = HSplit(HGauge(val=100), HGauge(val=100))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            render_tiles(tile, FakeTerminal(20, 2), "")
        grid = render_grid(buffer.getvalue(), 20, 2)

        self.assertEqual(grid[0][9], " ")
        self.assertIn("▉", grid[0][:9])
        self.assertIn("▉", grid[0][10:])

    def test_hgauge_fills_the_rows_it_is_given(self):
        from macpm.blessed_engine import render_tiles
        from macpm.widgets import HGauge

        # View 2 stretches the E/P usage gauges; filling every row is the
        # original look and must stay that way.
        tile = HGauge(val=100)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            render_tiles(tile, FakeTerminal(10, 4), "")
        grid = render_grid(buffer.getvalue(), 10, 4)

        for row in range(3):
            self.assertIn("▉", grid[row])


    def test_memory_box_keeps_one_bar_and_gives_the_rest_to_detail(self):
        from macpm.blessed_engine import _heights
        from macpm.widgets import HGauge, SensorPanel, VSplit

        split = VSplit(
            HGauge(min_height=2, max_height=2), SensorPanel(fill=True)
        )

        self.assertEqual(_heights(split, 4), [2, 2])
        self.assertEqual(_heights(split, 5), [2, 3])


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
