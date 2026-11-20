"""Blessed renderer for the view 1/2 widget tree.

``DefaultView`` builds a tree of ``VSplit``/``HSplit``/``HGauge``/``HChart``/
``SensorPanel`` nodes (see :mod:`macpm.widgets`); this module paints it with
blessed/ANSI so views 1/2 share the palette and border helpers of views 3-5.
"""

from __future__ import annotations

from .theme import KEY, OK, TITLE, UniformPalette, metric_spans, paint_spans, severity, title_spans
from .tui import RESET_FG, _border, _bottom, _fit, _pad

_VBARS = "▁▂▃▄▅▆▇█"


def _hbar(percent: float, width: int) -> str:
    percent = 0.0 if percent is None else max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100.0))
    return "▉" * filled + "▏" * max(0, width - filled)


def _bordered(tile) -> bool:
    # Widgets record the choice in ``border``; fall back to ``border_color``
    # for simple objects (tests, external callers).
    border = getattr(tile, "border", None)
    if border is not None:
        return bool(border)
    return getattr(tile, "border_color", None) is not None


def _write(out: list, term, row: int, column: int, text: str, palette, role=None) -> None:
    # Always write, including spaces: the terminal keeps stale cells when a
    # bar shrinks, which looked like a horizontal streak at the chart top.
    if getattr(palette, "native", False):
        spans = metric_spans(text, role or KEY)
    else:
        spans = [(text, role)]
    out.append(term.move(row, column) + paint_spans(spans, palette))


def _write_spans(out: list, term, row: int, column: int, spans, palette, width=None) -> None:
    out.append(term.move(row, column) + paint_spans(spans, palette, width))


def _box(tile, row, col, width, height, out, term, palette) -> None:
    color = palette.accent()
    title_role = getattr(tile, "title_role", None) or TITLE
    title = getattr(tile, "title", "") or ""
    bottom = getattr(tile, "bottom_title", "") or ""
    line = _fit(f" {title} ", max(0, width - 2)) if title else ""
    out.append(term.move(row, col) + color + _border(width, line) + RESET_FG)
    if line:
        _write_spans(out, term, row, col + 1, title_spans(line, title_role), palette)
    if height > 1:
        out.append(
            term.move(row + height - 1, col) + color + _bottom(width, bottom) + RESET_FG
        )
        fitted_bottom = _fit(bottom, max(0, width - 2))
        if fitted_bottom:
            _write_spans(
                out, term, row + height - 1, col + 1,
                metric_spans(fitted_bottom, TITLE), palette,
            )
    for offset in range(1, max(1, height - 1)):
        out.append(term.move(row + offset, col) + color + "│" + RESET_FG)
        out.append(
            term.move(row + offset, col + width - 1) + color + "│" + RESET_FG
        )


def _heights(tile, total: int) -> list:
    items = len(tile.items)
    if not items:
        return []
    if type(tile).__name__ == "FillVSplit":
        base, extra = divmod(total, items)
        heights = [base] * items
        for offset in range(extra):
            heights[items - 1 - (offset % items)] += 1
        for index in range(min(2, items)):
            cap = getattr(tile.items[index], "max_height", 0)
            if cap:
                heights[index] = min(heights[index], cap)
        remaining = total - sum(heights)
        if remaining > 0:
            growable = list(range(min(2, items), items)) or [items - 1]
            for offset in range(remaining):
                heights[growable[offset % len(growable)]] += 1
        return heights
    minimums = [getattr(item, "min_height", 0) for item in tile.items]
    maximums = [getattr(item, "max_height", 0) for item in tile.items]
    reserved = sum(minimums)
    if reserved and total > reserved or any(maximums):
        heights = list(minimums)
        remaining = max(0, total - reserved)
        fills = [
            index for index, item in enumerate(tile.items)
            if getattr(item, "fill", False)
        ]
        if fills and remaining:
            share, extra = divmod(remaining, len(fills))
            for index in fills:
                heights[index] += share
            for index in fills[-extra:] if extra else []:
                heights[index] += 1
            remaining = 0
        while remaining:
            candidates = [
                index for index, height in enumerate(heights)
                if not maximums[index] or height < maximums[index]
            ]
            if not candidates:
                break
            for index in candidates:
                if not remaining:
                    break
                heights[index] += 1
                remaining -= 1
        if sum(heights) == total:
            return heights
    base, extra = divmod(total, items)
    heights = [base] * items
    # Give the spare rows to the last children first: the GPU gauges sit at
    # the bottom of the processor box and need three rows for a border + bar.
    for index in range(items - 1, items - 1 - extra, -1):
        heights[index] += 1
    return heights


def _widths(tile, total: int) -> list:
    items = len(tile.items)
    base = total // items
    widths = [base] * items
    widths[-1] += total - base * items
    return widths


def _chart_values(tile, width: int) -> list:
    """The visible window: the newest ``width`` samples, oldest first."""
    points = list(getattr(tile, "datapoints", []))
    return points[-width:] if width > 0 else []


def _chart(tile, row, col, width, height, out, term, palette) -> None:
    points = _chart_values(tile, width)
    # Newest sample sits at the right edge; older ones scroll out to the left.
    offset = width - len(points)
    for line in range(height):
        chars = []
        for x in range(width):
            value = points[x - offset] if x >= offset else 0.0
            filled = max(0.0, min(100.0, float(value))) / 100.0 * height
            level = filled - (height - 1 - line)
            if level >= 1:
                chars.append("█")
            elif level > 0:
                chars.append(_VBARS[min(7, max(0, int(level * 8) - 1))])
            else:
                chars.append(" ")
        _write(out, term, row + line, col, "".join(chars), palette, OK)


def _sensor(tile, row, col, width, height, out, term, palette) -> None:
    lines = list(getattr(tile, "lines", []) or [])
    styled = [list(line) for line in (getattr(tile, "styled_lines", []) or [])]
    levels = getattr(tile, "levels", None)
    if levels:
        fan_line = [(tile._fan_bars(max(0, width - 1)), None)]
        if styled:
            if len(styled) < height:
                styled.append(fan_line)
        elif len(lines) < height:
            lines.append(tile._fan_bars(max(0, width - 1)))
    for offset in range(height):
        if offset < len(styled):
            _write_spans(out, term, row + offset, col, styled[offset], palette, width)
        elif offset < len(lines):
            text = _pad(_fit(lines[offset], width), width)
            _write(out, term, row + offset, col, text, palette, KEY)
        else:
            _write_spans(out, term, row + offset, col, [], palette, width)


def _bar_role(palette, tile):
    if not getattr(palette, "native", False):
        return None
    metric = getattr(tile, "metric", None)
    if not metric:
        return None
    return severity(metric, getattr(tile, "value", 0))


def paint(tile, row, col, width, height, out, term, palette) -> None:
    if width <= 0 or height <= 0:
        return
    bordered = _bordered(tile) and width >= 3 and height >= 3
    if bordered:
        _box(tile, row, col, width, height, out, term, palette)
        row, col, width, height = row + 1, col + 1, width - 2, height - 2

    name = type(tile).__name__
    items = getattr(tile, "items", [])
    if items:
        if name in ("VSplit", "FillVSplit"):
            y = row
            for item, child_height in zip(items, _heights(tile, height)):
                paint(item, y, col, width, child_height, out, term, palette)
                y += child_height
        else:
            x = col
            widths = _widths(tile, width)
            for index, (item, child_width) in enumerate(zip(items, widths)):
                # A one-column gutter keeps two adjacent bars from reading as
                # one long bar.  Bordered panels already have their own frame,
                # so they stay flush.
                gutter = 1 if index < len(items) - 1 and not _bordered(item) else 0
                paint(item, row, x, max(0, child_width - gutter), height, out, term, palette)
                x += child_width
        return

    if name == "HGauge":
        start = 0
        title = getattr(tile, "title", "") or ""
        if title and not bordered:
            _write(out, term, row, col, _pad(_fit(title, width), width), palette, KEY)
            start = 1
        if getattr(tile, "plain", False):
            return
        value = getattr(tile, "value", 0)
        role = _bar_role(palette, tile)
        # Fill every row the layout gave us.  View 2 stretches the E/P usage
        # gauges on purpose; the boxes with fixed content height (Memory) cap
        # theirs so this does not repeat a reading there.
        for line in range(start, height):
            _write(out, term, row + line, col, _hbar(value, width), palette, role)
    elif name == "VGauge":
        start = 0
        title = getattr(tile, "title", "") or ""
        if title and not bordered:
            _write(out, term, row, col, _pad(_fit(title, width), width), palette, KEY)
            start = 1
        if getattr(tile, "plain", False):
            return
        value = getattr(tile, "value", 0)
        role = _bar_role(palette, tile)
        inner = max(0, height - start)
        filled = inner * max(0.0, min(100.0, float(value))) / 100.0
        # A vertical bar, not full-width stripes: the height follows the value
        # like the original per-core gauges.  A partial row uses the block
        # gradient so a 1-2 row box still shows 20% or 70%.
        margin = 1 if width >= 5 else 0
        bar_width = max(1, width - 2 * margin)
        bar_col = col + margin
        for index in range(inner):
            line = start + index
            level = filled - (inner - 1 - index)
            if level >= 1:
                char = "█"
            elif level > 0:
                char = _VBARS[min(7, max(0, int(level * 8) - 1))]
            else:
                char = " "
            _write(out, term, row + line, bar_col, char * bar_width, palette, role)
    elif name == "HChart":
        start = 0
        title = getattr(tile, "title", "") or ""
        if title and not bordered:
            _write(out, term, row, col, _pad(_fit(title, width), width), palette, KEY)
            start = 1
        _chart(tile, row + start, col, width, height - start, out, term, palette)
    elif name == "SensorPanel":
        _sensor(tile, row, col, width, height, out, term, palette)
    else:
        title = getattr(tile, "title", "") or ""
        if title:
            _write(out, term, row, col, _fit(title, width), palette, KEY)


def render_tiles(tile, term, color) -> None:
    """Paint ``tile`` over the terminal, leaving the footer row free."""
    palette = color if hasattr(color, "style") else UniformPalette(color)
    out: list = []
    paint(tile, 0, 0, term.width, max(1, term.height - 1), out, term, palette)
    print("".join(out), end="", flush=True)
