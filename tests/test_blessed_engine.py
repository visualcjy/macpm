"""The blessed painter: chart window, split math and terminal resizing."""

import contextlib
import io
import unittest

from macpm.blessed_engine import _chart_values, _heights, _widths, render_tiles


class FillVSplit:
    def __init__(self, *items):
        self.items = list(items)


class HSplit:
    def __init__(self, *items):
        self.items = list(items)


class HGauge:
    def __init__(self, value=50):
        self.value = value
        self.title = ""
        self.border_color = None


class FakeTerm:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.moves = []

    def move(self, row, column):
        self.moves.append((row, column))
        return ""

    def color(self, value):
        return ""


class ChartWindowTests(unittest.TestCase):
    def test_window_is_the_newest_samples(self):
        class Chart:
            datapoints = list(range(20))

        self.assertEqual(_chart_values(Chart(), 5), [15, 16, 17, 18, 19])
        self.assertEqual(_chart_values(Chart(), 30), list(range(20)))


class SplitTests(unittest.TestCase):
    def test_heights_and_widths_fill_the_box(self):
        tree = FillVSplit(HGauge(), HGauge(), HGauge())

        self.assertEqual(sum(_heights(tree, 24)), 24)
        self.assertEqual(sum(_widths(tree, 80)), 80)


class ResizeTests(unittest.TestCase):
    def _render(self, width, height):
        tree = FillVSplit(HSplit(HGauge(30), HGauge(60), HGauge(90)), HGauge(10))
        term = FakeTerm(width, height)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_tiles(tree, term, "")
        self.assertTrue(out.getvalue())
        for row, column in term.moves:
            self.assertGreaterEqual(row, 0)
            self.assertLess(row, height)
            self.assertGreaterEqual(column, 0)
            self.assertLess(column, width)

    def test_renders_at_several_terminal_sizes(self):
        for width, height in ((80, 24), (120, 44), (160, 50)):
            self._render(width, height)


if __name__ == "__main__":
    unittest.main()
