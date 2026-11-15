"""dashing 0.1 vs 0.2 colour compatibility.

0.1 stored colours as plain attributes; 0.2 turned them into read-only
properties backed by ``Tile.set_color``, which only accepts RGB/strings.
"""

import sys
import unittest
from unittest import mock

# macpm.macpm parses the command line at import time (the TUI reads a global).
with mock.patch.object(sys, "argv", ["macpm"]):
    import macpm.macpm as macpm_mod


class FakeRGB:
    def __init__(self, r, g, b):
        self.r = r
        self.g = g
        self.b = b

    def __repr__(self):
        return f"RGB({self.r},{self.g},{self.b})"


class LegacyTile:
    def __init__(self):
        self.color = None
        self.border_color = None


class ModernTile:
    def __init__(self):
        self._text_color = None
        self._border_color = None
        self.calls = []

    @property
    def border_color(self):
        return self._border_color

    def set_color(self, name, value):
        if not isinstance(value, FakeRGB):
            raise ValueError("Invalid color type")
        setattr(self, "_" + name, value)
        self.calls.append(name)


class DashingCompatTests(unittest.TestCase):
    def setUp(self):
        self._v2 = macpm_mod._DASHING_V2
        self._rgb = getattr(macpm_mod.dashing, "RGB", None)

    def tearDown(self):
        macpm_mod._DASHING_V2 = self._v2
        try:
            if self._rgb is None:
                del macpm_mod.dashing.RGB
            else:
                macpm_mod.dashing.RGB = self._rgb
        except AttributeError:
            pass

    def test_legacy_tiles_keep_plain_attributes(self):
        macpm_mod._DASHING_V2 = False
        tile = LegacyTile()

        macpm_mod._set_tile_colors(tile, 3)

        self.assertEqual(tile.color, 3)
        self.assertEqual(tile.border_color, 3)

    def test_modern_tiles_use_set_color_with_rgb(self):
        macpm_mod._DASHING_V2 = True
        macpm_mod.dashing.RGB = FakeRGB
        tile = ModernTile()

        macpm_mod._set_tile_colors(tile, 3)

        self.assertEqual(tile.calls, ["text_color", "border_color"])
        self.assertEqual((tile._text_color.r, tile._text_color.g, tile._text_color.b),
                         (255, 255, 0))

    def test_theme_eight_maps_to_grey(self):
        macpm_mod._DASHING_V2 = True
        macpm_mod.dashing.RGB = FakeRGB
        tile = ModernTile()

        macpm_mod._set_tile_color(tile, "border_color", 8)

        self.assertEqual((tile._border_color.r, tile._border_color.g,
                          tile._border_color.b), (128, 128, 128))

    def test_unknown_tiles_are_ignored(self):
        macpm_mod._DASHING_V2 = True
        macpm_mod.dashing.RGB = FakeRGB

        macpm_mod._set_tile_color(object(), "color", 2)  # must not raise


if __name__ == "__main__":
    unittest.main()
