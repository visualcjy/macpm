"""Semantic colour roles for the built-in ``macpm`` theme.

The numeric themes (1-8) paint the whole UI in one ANSI accent colour.  The
ninth stop, ``native``, paints by role instead: bright-white box titles, near
white field names, dim secondary text, bright-white numbers, the original green
bars, cyan addresses and amber/red only for warnings/critical readings.  The
xterm 256-colour indices used here are fixed (16-255), independent of the
terminal profile.  ``mono`` (``--no-color``/``NO_COLOR``) keeps the old
no-colour behaviour.
"""

from __future__ import annotations

import re
from typing import Optional

from .tui import RESET_FG, _text_width

NATIVE = "native"

ACCENT = "accent"
BOX = "box"
TITLE = "title"
KEY = "key"
LABEL = "label"
VALUE = "value"
RATE = "rate"
HINT = "hint"
OK = "ok"
WARN = "warn"
CRIT = "crit"
ADDRESS = "address"
MUTED = "muted"

# Indices 16-255 are not remapped by ordinary terminal themes.
_PALETTE = {
    ACCENT: "\x1b[38;5;245m",   # frame lines: unchanged grey
    BOX: "\x1b[38;5;137m",      # category panel titles: brown
    TITLE: "\x1b[38;5;231m",    # machine/view titles: bright white
    KEY: "\x1b[38;5;252m",      # field names: near white
    LABEL: "\x1b[38;5;245m",    # ordinary text: dim white
    VALUE: "\x1b[38;5;231m",    # plain numbers: bright white
    RATE: "\x1b[38;5;75m",      # IO rates (B/s, kB/s, MB/s): blue
    HINT: "\x1b[38;5;220m",     # footer key hints: yellow
    OK: "\x1b[38;5;71m",        # normal bars: the original green
    WARN: "\x1b[38;5;179m",     # amber
    CRIT: "\x1b[38;5;167m",     # red
    ADDRESS: "\x1b[38;5;73m",   # IP addresses: muted cyan
    MUTED: "\x1b[38;5;240m",    # unavailable
}

# (warn, crit) boundaries, inclusive on the upper side.
THRESHOLDS = {
    "load": (60, 85),
    "temp": (75, 90),
    "ram": (75, 90),
    "fan": (70, 90),
    "proc_cpu": (50, 80),
    "proc_gpu": (50, 80),
    "proc_mem": (5, 15),
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# A number immediately followed by a unit ("36.0GB", "0.05W", "810M").
_NUMBER_UNIT = re.compile(
    r"^(\d+(?:\.\d+)?)(°C|[kMGT]?Hz|[kMGT]?B(?:/s|ytes/s)?|B/s|W|rpm|%|[kMG])$"
)
# Words may contain digits: "M3", "en0", "v3.2.0" stay one token.
_TOKEN = re.compile(
    r"°C"
    r"|[A-Za-z][A-Za-z0-9_.+-]*(?:/[A-Za-z]+)?"
    r"|\d+(?:\.\d+)?[A-Za-z][A-Za-z0-9_.+-]*(?:/[A-Za-z]+)?"
    r"|\d+(?:\.\d+)?"
    r"|[^\w\s]"
    r"|\s+"
)
_ADDRESS = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d+)?\b"
    r"|\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"
)
_UNIT = re.compile(
    r"^(?:°C|[kMGT]?Hz|[kMGT]?B(?:/s|ytes/s)?|B/s|[kMG]|W|rpm|%|iops|fps)$"
)
_RATE_UNIT = re.compile(r"^(?:[kMGT]?B/s|Bytes/s)$")
_HINT = re.compile(
    r"Ctrl\+R|←/→|↑/↓|1-5|\bq\b|\bs\b|\ba\b|\bp\b|\?|Space|Tab|Esc|Enter"
)

# Words that are labels, not values; they stay dim inside a title too.
_SECONDARY = {
    "avg", "peak", "throttle", "off", "no", "on", "cores", "r", "w",
}
_MUTED_WORDS = {"n/a", "none", "--"}


def severity(metric: str, value) -> str:
    """Role for ``value`` under ``metric``; unknown metrics are just values."""
    if value is None:
        return MUTED
    limits = THRESHOLDS.get(metric)
    if limits is None:
        return VALUE
    warn, crit = limits
    if value >= crit:
        return CRIT
    if value >= warn:
        return WARN
    return OK


def value_role(metric: str, value) -> str:
    """Like :func:`severity`, but normal readings use the number colour."""
    role = severity(metric, value)
    return VALUE if role == OK else role


def number_spans(text: str, base: str | None = None) -> list:
    """Split ``text`` so plain numbers use the VALUE role."""
    if not text:
        return [(text, base)]
    spans = []
    position = 0
    for match in _NUMBER.finditer(text):
        if match.start() > position:
            spans.append((text[position:match.start()], base))
        spans.append((match.group(0), VALUE))
        position = match.end()
    if position < len(text):
        spans.append((text[position:], base))
    return spans or [(text, base)]


def _plain_spans(text: str, base: str) -> list:
    spans = []
    last_number = None
    for token in _TOKEN.findall(text):
        word = token.strip(":()[],")
        lower = word.lower()
        split = _NUMBER_UNIT.match(token)
        if split:
            number, unit = split.groups()
            if _RATE_UNIT.match(unit):
                spans.append((number, RATE))
                spans.append((unit, RATE))
            else:
                spans.append((number, VALUE))
                spans.append((unit, LABEL))
            last_number = None
        elif token[0].isdigit():
            # "4E", "30GPU": letters and digits together count as a name.
            if any(c.isalpha() for c in token):
                spans.append((token, base))
                last_number = None
            else:
                spans.append((token, VALUE))
                last_number = len(spans) - 1
        elif lower in _MUTED_WORDS:
            spans.append((token, MUTED))
            last_number = None
        elif _UNIT.match(token):
            if _RATE_UNIT.match(token) and last_number is not None:
                # "0 Bytes/s", "3.9 MB/s": the number and unit both go blue.
                number, _ = spans[last_number]
                spans[last_number] = (number, RATE)
                spans.append((token, RATE))
            else:
                spans.append((token, LABEL))
            last_number = None
        elif lower in _SECONDARY:
            spans.append((token, LABEL))
            last_number = None
        elif token.isspace():
            spans.append((token, base))
        else:
            spans.append((token, base))
            last_number = None
    return spans


def metric_spans(text: str, base: str = KEY) -> list:
    """Tokenise a label/value line into roles.

    Field names use ``base`` (KEY by default, TITLE inside a box title),
    standalone numbers use VALUE, units and secondary words stay dim, and IP
    addresses get their own cyan role.  Digits that are part of a name
    (``M3``, ``en0``) stay with the name.
    """
    if not text:
        return [(text, base)]
    spans = []
    position = 0
    for match in _ADDRESS.finditer(text):
        spans.extend(_plain_spans(text[position:match.start()], base))
        spans.append((match.group(0), ADDRESS))
        position = match.end()
    spans.extend(_plain_spans(text[position:], base))
    return spans


def title_spans(text: str, title_role: str = TITLE) -> list:
    """A box title is only the part before the first ``(``; the parenthetical
    detail is ordinary text (numbers still white, rates still blue)."""
    marker = text.find("(")
    if marker < 0:
        return metric_spans(text, title_role)
    return metric_spans(text[:marker], title_role) + metric_spans(
        text[marker:], LABEL
    )


def footer_spans(text: str, base: str = ACCENT) -> list:
    """Split a footer line so key hints (q, 1-5, Ctrl+R, ...) turn yellow."""
    if not text:
        return [(text, base)]
    spans = []
    position = 0
    for match in _HINT.finditer(text):
        if match.start() > position:
            spans.append((text[position:match.start()], base))
        spans.append((match.group(0), HINT))
        position = match.end()
    if position < len(text):
        spans.append((text[position:], base))
    return spans


class Palette:
    """Resolve a role to an SGR sequence for the active theme."""

    def __init__(self, term, color: int = 2, native: bool = False, mono: bool = False):
        self.term = term
        self.color = color
        self.native = native
        self.mono = mono

    def accent(self) -> str:
        if self.mono:
            return ""
        if self.native:
            return _PALETTE[ACCENT]
        return self.term.color(self.color) if self.term is not None else ""

    def style(self, role: str | None = None) -> str:
        if self.mono:
            return ""
        if role == HINT:
            # Key hints stay yellow in every colour theme.
            return _PALETTE[HINT]
        if self.native:
            # No role means "the theme's base colour" (chart bars, padding);
            # only explicit VALUE spans turn numbers bright white.
            return self.accent() if role is None else _PALETTE.get(role, self.accent())
        return self.accent()


class UniformPalette:
    """Compatibility wrapper for callers that pass one SGR string."""

    native = False

    def __init__(self, color: str):
        self._color = color or ""

    def accent(self) -> str:
        return self._color

    def style(self, role: str | None = None) -> str:
        return self._color


def paint_spans(spans, palette, width: Optional[int] = None) -> str:
    """Render ``(text, role)`` spans, optionally padded to ``width`` cells."""
    used = sum(_text_width(text) for text, _ in spans)
    parts = []
    current = object()
    for text, role in spans:
        if role != current:
            parts.append(palette.style(role))
            current = role
        parts.append(text)
    if width is not None and used < width:
        if current is not None:
            parts.append(palette.style(None))
        parts.append(" " * (width - used))
    parts.append(RESET_FG)
    return "".join(parts)
