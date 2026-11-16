"""ANE usage: residency from IOReport, plus the gauge text that shows it."""

import sys
import types
import unittest

from macpm.metrics import ane_usage

# macpm.macpm parses argv while it imports (the entry point lives there), so
# give it a plausible command line before importing the helper.
_argv, sys.argv = sys.argv, ["macpm"]
try:
    from macpm.macpm import ane_gauge_text
finally:
    sys.argv = _argv


def channel(states):
    return types.SimpleNamespace(states=states)


class AneUsageTests(unittest.TestCase):
    def test_running_share_is_the_utilisation(self):
        raw = {"status": channel([("Off", 100), ("Running", 300)])}

        self.assertAlmostEqual(ane_usage(raw), 75.0)

    def test_idle_is_zero(self):
        raw = {"status": channel([("Off", 24_000_000), ("Running", 0)])}

        self.assertEqual(ane_usage(raw), 0.0)

    def test_missing_or_stateless_channel_is_unknown(self):
        self.assertIsNone(ane_usage({}))
        self.assertIsNone(ane_usage({"status": types.SimpleNamespace(states=None)}))
        self.assertIsNone(ane_usage({"status": channel([])}))

    def test_empty_interval_is_unknown(self):
        raw = {"status": channel([("Off", 0), ("Running", 0)])}

        self.assertIsNone(ane_usage(raw))

    def test_negative_residency_is_clamped_and_names_are_case_folded(self):
        raw = {"status": channel([("off", -10), (" RUNNING ", 50)])}

        self.assertAlmostEqual(ane_usage(raw), 100.0)


class AneGaugeTextTests(unittest.TestCase):
    def test_residency_drives_the_percentage_without_watts(self):
        title, percent = ane_gauge_text(
            {"ane_W": 0.0, "ane_usage": 42.0}, interval=1.0
        )

        self.assertEqual(title, "ANE Usage: 42%")
        self.assertEqual(percent, 42)

    def test_measured_power_is_appended(self):
        title, percent = ane_gauge_text(
            {"ane_W": 1.25, "ane_usage": 100.0}, interval=1.0
        )

        self.assertEqual(title, "ANE Usage: 100% @ 1.2 W")
        self.assertEqual(percent, 100)

    def test_power_ratio_is_the_fallback(self):
        title, percent = ane_gauge_text(
            {"ane_W": 8.0, "ane_usage": None}, interval=1.0, max_power=16.0
        )

        self.assertEqual(title, "ANE Usage: 50% @ 8.0 W")
        self.assertEqual(percent, 50)

    def test_usage_is_clamped(self):
        _, percent = ane_gauge_text({"ane_W": 0.0, "ane_usage": 140.0}, interval=1.0)

        self.assertEqual(percent, 100)


if __name__ == "__main__":
    unittest.main()
