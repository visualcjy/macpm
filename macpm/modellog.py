"""Read per-request statistics from a model server's own log.

Splash writes one line per request (``Started`` / ``TTFT`` / ``Done``) with the
same numbers its dashboard shows, so the terminal can show real history
instead of guessing from polls.  Only the bytes appended since the last read
are parsed; a truncated or rotated file restarts from the beginning.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

STARTED = re.compile(r"^(\d\d:\d\d:\d\d) Started · request (\d+) · input ([\d,]+) · cached ([\d,]+)")
TTFT = re.compile(r"^(\d\d:\d\d:\d\d) TTFT of request (\d+): ([\d.]+) ms")
DONE = re.compile(
    r"^(\d\d:\d\d:\d\d) Done · request (\d+) · input ([\d,]+) · cached ([\d,]+) "
    r"· output ([\d,]+).*?TTFT ([\d.]+)s · ([\d.]+) tok/s"
)

DEFAULT_SPLASH_LOG = "~/Library/Logs/Splash/server.log"


def _number(text: str) -> Optional[float]:
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def _clock(text: str) -> float:
    """``HH:MM:SS`` today → epoch seconds (logs are same-day)."""
    now = datetime.now()
    try:
        hour, minute, second = (int(part) for part in text.split(":"))
    except (ValueError, AttributeError):
        return time.time()
    return now.replace(hour=hour, minute=minute, second=second, microsecond=0).timestamp()


class LogTail:
    """Return only the lines appended to ``path`` since the previous call."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self.offset = 0

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def read(self) -> list:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:          # truncated or rotated
            self.offset = 0
        if size == self.offset:
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                lines = handle.read().splitlines()
                self.offset = handle.tell()
                return lines
        except OSError:
            return []


class SplashLogReader:
    """Turn Splash log lines into completed-request rows."""

    def __init__(self, path: str = DEFAULT_SPLASH_LOG):
        self.tail = LogTail(path)
        self.pending: dict = {}

    def poll(self) -> list:
        rows = []
        for line in self.tail.read():
            line = line.strip()
            match = STARTED.match(line)
            if match:
                clock, rid, prompt, cached = match.groups()
                self.pending[rid] = {
                    "started": _clock(clock),
                    "prompt": _number(prompt),
                    "cached": _number(cached),
                }
                continue
            match = TTFT.match(line)
            if match:
                _clock_text, rid, ms = match.groups()
                entry = self.pending.setdefault(rid, {})
                entry["first_ms"] = _number(ms)
                continue
            match = DONE.match(line)
            if match:
                (clock, rid, prompt, cached, output,
                 ttft_s, rate) = match.groups()
                entry = self.pending.pop(rid, {})
                started = entry.get("started") or _clock(clock)
                finished = _clock(clock)
                rows.append({
                    "finished": finished,
                    "started": started,
                    "duration": max(0.0, finished - started),
                    "prompt": _number(prompt),
                    "cached": _number(cached),
                    "output": _number(output),
                    "first_ms": entry.get("first_ms")
                                 or (_number(ttft_s) or 0.0) * 1000.0,
                    "tps": _number(rate),
                })
        return rows
