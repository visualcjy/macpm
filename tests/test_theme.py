"""Semantic roles: thresholds, number spans and the native palette."""

import contextlib
import io
import unittest

from macpm.blessed_engine import render_tiles
from macpm.theme import (
    ACCENT,
    CRIT,
    LABEL,
    ADDRESS,
    BOX,
    HINT,
    KEY,
    MUTED,
    OK,
    RATE,
    TITLE,
    VALUE,
    WARN,
    Palette,
    footer_spans,
    metric_spans,
    title_spans,
    number_spans,
    severity,
    value_role,
)
from macpm.widgets import HGauge, SensorPanel, VSplit


class FakeTerm:
    width, height = 40, 6

    def move(self, row, column):
        return ""

    def color(self, value):
        return "\x1b[3{}m".format(value)


class SeverityTests(unittest.TestCase):
    def test_load_boundaries(self):
        self.assertEqual(severity("load", 59.9), OK)
        self.assertEqual(severity("load", 60), WARN)
        self.assertEqual(severity("load", 85), CRIT)

    def test_value_role_keeps_normal_readings_violet(self):
        self.assertEqual(severity("load", 30), OK)
        self.assertEqual(value_role("load", 30), VALUE)
        self.assertEqual(value_role("load", 90), CRIT)

    def test_names_with_digits_stay_names(self):
        spans = metric_spans("M3 en0 45")
        roles = {text: role for text, role in spans if text.strip()}
        self.assertEqual(roles["M3"], KEY)
        self.assertEqual(roles["en0"], KEY)
        self.assertEqual(roles["45"], VALUE)

    def test_rates_are_blue_but_sizes_are_not(self):
        spans = metric_spans("Read 3.9 MB/s  RAM 23.1/36.0GB")
        roles = {text: role for text, role in spans if text.strip()}
        self.assertEqual(roles["3.9"], RATE)
        self.assertEqual(roles["MB/s"], RATE)
        self.assertEqual(roles["23.1"], VALUE)
        self.assertEqual(roles["GB"], LABEL)

    def test_separated_rate_units_colour_the_number_too(self):
        spans = metric_spans("in : 0 Bytes/s")
        roles = {text: role for text, role in spans if text.strip()}
        self.assertEqual(roles["0"], RATE)
        self.assertEqual(roles["Bytes/s"], RATE)

    def test_box_title_excludes_parenthetical(self):
        spans = title_spans("Apple M3 Max (cores: 4E+10P+30GPU)", BOX)
        roles = {text: role for text, role in spans if text.strip()}
        self.assertEqual(roles["Apple"], BOX)
        self.assertEqual(roles["M3"], BOX)
        self.assertEqual(roles["Max"], BOX)
        self.assertEqual(roles["cores"], LABEL)
        self.assertEqual(roles["4E+10P+30GPU"], LABEL)

    def test_footer_keys_are_yellow(self):
        spans = footer_spans(" macpm v3.2.0 · q quit  1-5 view  Ctrl+R reset ")
        roles = {text: role for text, role in spans if text.strip()}
        self.assertEqual(roles["q"], HINT)
        self.assertEqual(roles["1-5"], HINT)
        self.assertEqual(roles["Ctrl+R"], HINT)
        self.assertTrue(any("macpm" in text and role == "accent" for text, role in spans))

    def test_ip_addresses_get_their_own_role(self):
        spans = metric_spans("en0 192.168.150.116/24")
        roles = {text: role for text, role in spans if text.strip()}
        self.assertEqual(roles["en0"], KEY)
        self.assertEqual(roles["192.168.150.116/24"], ADDRESS)

    def test_temperature_and_unknown(self):
        self.assertEqual(severity("temp", 74.9), OK)
        self.assertEqual(severity("temp", 90), CRIT)
        self.assertEqual(severity("clock", 999), VALUE)
        self.assertEqual(severity("temp", None), MUTED)


class SpanTests(unittest.TestCase):
    def test_metric_tokens_use_names_values_units(self):
        roles = [role for _, role in metric_spans("CPU 45% @ 1988 MHz")]
        self.assertEqual(
            roles,
            [KEY, KEY, VALUE, LABEL, KEY, KEY, KEY, VALUE, KEY, LABEL],
        )

    def test_numbers_are_split_out(self):
        spans = number_spans("CPU 42.5% @ 1800 MHz", LABEL)
        self.assertEqual(
            spans,
            [
                ("CPU ", LABEL),
                ("42.5", VALUE),
                ("% @ ", LABEL),
                ("1800", VALUE),
                (" MHz", LABEL),
            ],
        )


class PaletteTests(unittest.TestCase):
    def test_native_roles_are_fixed_256_colours(self):
        palette = Palette(FakeTerm(), native=True)
        self.assertEqual(palette.accent(), "\x1b[38;5;245m")
        self.assertEqual(palette.style(TITLE), "\x1b[38;5;231m")
        self.assertEqual(palette.style(KEY), "\x1b[38;5;252m")
        self.assertEqual(palette.style(OK), "\x1b[38;5;71m")
        self.assertEqual(palette.style(ADDRESS), "\x1b[38;5;73m")
        self.assertEqual(palette.style(BOX), "\x1b[38;5;137m")
        self.assertEqual(palette.style(RATE), "\x1b[38;5;75m")
        self.assertEqual(palette.style(HINT), "\x1b[38;5;220m")
        self.assertEqual(palette.style(VALUE), "\x1b[38;5;231m")
        self.assertEqual(palette.style(CRIT), "\x1b[38;5;167m")
        self.assertEqual(palette.style(None), "\x1b[38;5;245m")

    def test_mono_emits_nothing(self):
        palette = Palette(FakeTerm(), native=True, mono=True)
        self.assertEqual(palette.accent(), "")
        self.assertEqual(palette.style(CRIT), "")

    def test_numeric_theme_keeps_one_colour(self):
        palette = Palette(FakeTerm(), color=2)
        self.assertEqual(palette.style(CRIT), palette.accent())


class NativeRenderTests(unittest.TestCase):
    def render(self, metric, value):
        tree = HGauge(title="load 95%", val=value, metric=metric)
        term = FakeTerm()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_tiles(tree, term, Palette(term, native=True))
        return out.getvalue()

    def test_gauge_bar_uses_the_threshold_role(self):
        data = self.render("load", 95)
        self.assertIn("\x1b[38;5;167m", data)  # crit
        self.assertIn("\x1b[38;5;231m", data)  # the number in the title

    def test_category_box_title_is_brown(self):
        panel = SensorPanel(title="Memory", border_color=2)
        panel.title_role = BOX
        term = FakeTerm()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_tiles(VSplit(panel, border_color=2), term, Palette(term, native=True))
        self.assertIn("\x1b[38;5;137m", out.getvalue())

    def test_plain_string_colour_still_works(self):
        tree = HGauge(title="CPU", val=50)
        term = FakeTerm()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_tiles(tree, term, "\x1b[32m")
        self.assertIn("\x1b[32m", out.getvalue())


if __name__ == "__main__":
    unittest.main()
