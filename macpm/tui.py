"""Small text helpers shared by the full-screen views.

The views draw their frames by hand instead of leaving the layout to
``dashing`` so that they can clip every row to the terminal width.  The
helpers here are width-aware: East Asian wide characters count as two cells.
"""

from __future__ import annotations

import unicodedata


# ``Terminal.normal`` is SGR 0, which also clears the background colour.
# curses sets a black background when it enters the alternate screen; resetting
# it exposes the terminal profile's background (often grey).  Reset only the
# foreground so the panel background stays as curses set it.
RESET_FG = "\x1b[39m"


def _text_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _text_width(text) <= width:
        return text
    if width == 1:
        return "…"
    used = 0
    kept = []
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + char_width > width - 1:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + "…"


def _pad(text: str, width: int, right: bool = False) -> str:
    text = _fit(text, width)
    padding = " " * max(0, width - _text_width(text))
    return padding + text if right else text + padding


def _center(text: str, width: int) -> str:
    text = _fit(text, width)
    left = max(0, (width - _text_width(text)) // 2)
    return " " * left + text + " " * max(0, width - left - _text_width(text))


def _format_mem(value: int) -> str:
    size = float(value)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.0f}{unit}" if size >= 10 else f"{size:.1f}{unit}"
        size /= 1024
    return "0B"


def _border(width: int, left: str, right: str = "") -> str:
    inner = max(0, width - 2)
    left = _fit(left, inner)
    if _text_width(left) + _text_width(right) > inner:
        right = ""
    return "┌" + left + "─" * max(0, inner - _text_width(left) - _text_width(right)) + right + "┐"


def app_label() -> str:
    """``macpm v3.0`` — shown at the start of every footer."""
    from . import __version__

    return f"macpm v{__version__}"


def footer(text: str) -> str:
    """Prefix a hint line with the app name and version."""
    return f" {app_label()} · {text.strip()} "


def _bottom(width: int, text: str) -> str:
    inner = max(0, width - 2)
    text = _fit(text, inner)
    return "└" + text + "─" * max(0, inner - _text_width(text)) + "┘"
