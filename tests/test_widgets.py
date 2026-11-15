"""The plain widget tree that replaced dashing for views 1/2."""

import unittest

from macpm.widgets import (
    FillVSplit,
    HChart,
    HGauge,
    HSplit,
    SensorPanel,
    VGauge,
    VSplit,
)


class SplitTests(unittest.TestCase):
    def test_fill_layout_uses_the_remainder(self):
        split = FillVSplit(HGauge(), HGauge(), HGauge())

        self.assertEqual(split._layout(24), [8, 8, 8])
        self.assertEqual(sum(split._layout(25)), 25)

    def test_splits_accept_a_list_or_positional_items(self):
        self.assertEqual(len(VSplit([HGauge(), HGauge()]).items), 2)
        self.assertEqual(len(HSplit(HGauge(), HGauge()).items), 2)


class LeafTests(unittest.TestCase):
    def test_chart_keeps_history(self):
        chart = HChart(title="io")

        chart.append(10)
        chart.append(20)

        self.assertEqual(chart.datapoints, [10, 20])

    def test_gauges_hold_a_value(self):
        gauge = HGauge(title="CPU", val=0)

        gauge.value = 42

        self.assertEqual(gauge.value, 42)

    def test_sensor_panel_bars(self):
        panel = SensorPanel(title="Temps", levels=[("F1", 50), ("F2", 100)])

        bars = panel._fan_bars(20)

        self.assertIn("F1", bars)
        self.assertIn("F2", bars)


class BorderTests(unittest.TestCase):
    def test_border_follows_border_colour(self):
        self.assertTrue(VSplit(title="Memory", border_color=2).border)
        self.assertFalse(HGauge(title="CPU", color=2).border)
        self.assertFalse(HGauge(title="CPU", color=2, border_color=2).items)


if __name__ == "__main__":
    unittest.main()
