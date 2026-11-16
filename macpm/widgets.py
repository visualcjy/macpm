"""Plain widget tree for views 1/2 (replaces the dashing objects).

``DefaultView`` builds ``VSplit``/``HSplit``/``HGauge``/``VGauge``/``HChart``
and ``SensorPanel`` nodes, updates their ``value``/``datapoints`` every sample
and hands the tree to :mod:`macpm.blessed_engine` for painting.  Keeping the
model here lets macpm drop the dashing dependency while preserving the same
layout rules (the split/height logic mirrors what dashing did).
"""

from __future__ import annotations


def _fit_title(title: str, width: int) -> str:
    """Shorten ``title`` to ``width`` columns, ending with one ellipsis."""
    if width <= 0:
        return ""
    if len(title) <= width:
        return title
    if width == 1:
        return "…"
    return title[: width - 1] + "…"


class Tile:
    """A rectangle in the layout tree; leaves carry values or history."""

    def __init__(
        self,
        title: str = "",
        val: float = 0,
        color=None,
        border_color=None,
        border=None,
        min_height: int = 0,
        max_height: int = 0,
        plain: bool = False,
        fill: bool = False,
    ):
        self.title = title
        self.value = val
        self.color = color
        self.border_color = border_color
        # A panel draws a border when it was given a border colour; gauges and
        # charts are borderless labels painted inside their parent box.
        self.border = bool(border_color is not None if border is None else border)
        self.min_height = max(0, int(min_height))
        self.max_height = max(0, int(max_height))
        self.plain = bool(plain)
        self.fill = bool(fill)
        self.items: list = []


class _Split(Tile):
    def __init__(self, *items, **kwargs):
        super().__init__(**kwargs)
        if len(items) == 1 and isinstance(items[0], (list, tuple)):
            items = tuple(items[0])
        self.items = list(items)


class VSplit(_Split):
    """Stack children vertically."""


class HSplit(_Split):
    """Place children side by side."""


class FillVSplit(VSplit):
    """VSplit that uses the rows dashing would have left blank.

    The first panel keeps ``height // count`` rows (the processor box relies
    on it for the network-info row); the remainder is spread over the other
    panels so the footer sits directly below the last one.
    """

    def _layout(self, total_height: int) -> list:
        count = len(self.items)
        if not count:
            return []
        base, extra = divmod(total_height, count)
        heights = [base] * count
        for offset in range(extra):
            heights[count - 1 - (offset % count)] += 1
        # The first two panels are the processor box and the Memory/Temps row:
        # their content is a fixed number of lines, so extra rows there only
        # became blank space or a repeated RAM bar.  Cap them and hand the
        # surplus to the charts below (items 2..n).
        for index in range(min(2, count)):
            cap = getattr(self.items[index], "max_height", 0)
            if cap:
                heights[index] = min(heights[index], cap)
        remaining = total_height - sum(heights)
        if remaining > 0:
            growable = list(range(min(2, count), count)) or [count - 1]
            for offset in range(remaining):
                heights[growable[offset % len(growable)]] += 1
        return heights


class HGauge(Tile):
    """Horizontal fill bar."""


class VGauge(Tile):
    """Vertical fill bar."""


class HChart(Tile):
    """History chart; newest sample at the right edge."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapoints: list = []

    def append(self, value: float) -> None:
        self.datapoints.append(value)


class SensorPanel(Tile):
    """Bordered text box for temperatures and fan speeds."""

    def __init__(self, color=2, lines=None, levels=None, **kwargs):
        super().__init__(color=color, **kwargs)
        self.lines = list(lines or [])
        self.levels = list(levels or [])

    def _fan_bars(self, width: int) -> str:
        half = max(0, (width - 2) // 2)
        cells = []
        for index in range(2):
            if index >= len(self.levels):
                cells.append(" " * half)
                continue
            label, level = self.levels[index]
            if level is None:
                cells.append(_fit_title(f"{label} --", half).ljust(half))
                continue
            level = max(0.0, min(100.0, level))
            bar_width = max(1, half - len(label) - 1)
            filled = int(round(bar_width * level / 100))
            bar = "▉" * filled + "▏" * (bar_width - filled)
            cells.append(_fit_title(f"{label} {bar}", half).ljust(half))
        return "  ".join(cells)
