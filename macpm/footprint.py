"""Per-process GPU memory on macOS, from ``footprint(1)``.

An Apple GPU has no VRAM of its own: it allocates out of unified RAM, and the
IOAccelerator only reports the total every process holds.  The per-process
split lives in ``footprint``'s ``(graphics)`` categories -- the Metal buffers
and textures macOS charges to that task.

Measured here: one call costs 15-17 ms, so the GPU view measures a handful of
targets in a background thread instead of blocking the refresh loop.  Two
macOS details are easy to get wrong:

* ``-j`` takes a *file* (``-j <path>``) and writes JSON there; it is not
  stdout, and passing ``-p`` right after ``-j`` silently sends the table to
  stdout instead.
* Without root ``footprint`` can only inspect the caller's own processes; the
  rest exit non-zero, and their memory stays unknown (``n/a``), never zero.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

import psutil

GRAPHICS_SUFFIX = "(graphics)"
_FOOTPRINT = shutil.which("footprint") or "/usr/bin/footprint"


def available() -> bool:
    return os.path.exists(_FOOTPRINT)


def graphics_bytes(process: dict) -> Optional[int]:
    """Dirty bytes of a process's ``(graphics)`` categories.

    ``0`` is a real answer (a process that never touched Metal).  ``None``
    means the document did not carry categories, or a value could not be read;
    that must stay unknown rather than become a smaller number.
    """
    categories = process.get("categories")
    if not isinstance(categories, dict):
        return None
    total = 0
    for name, stats in categories.items():
        if not name.endswith(GRAPHICS_SUFFIX):
            continue
        if not isinstance(stats, dict):
            return None
        dirty = stats.get("dirty")
        if isinstance(dirty, bool) or not isinstance(dirty, int) or dirty < 0:
            return None
        total += dirty
    return total


def parse_footprint(document) -> dict:
    """``{pid: graphics_bytes}`` from one ``footprint -j`` document."""
    result = {}
    processes = document.get("processes") if isinstance(document, dict) else None
    for process in processes or []:
        if not isinstance(process, dict):
            continue
        pid = process.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            continue
        result[pid] = graphics_bytes(process)
    return result


def read_process_graphics(pid: int, timeout: float = 10.0) -> Optional[int]:
    """One process's graphics bytes, or ``None`` when it cannot be measured.

    Fails for other users' processes without root and for processes that exit
    mid-read; both are ordinary, not an error worth reporting.
    """
    handle, path = tempfile.mkstemp(prefix="macpm-footprint-", suffix=".json")
    os.close(handle)
    try:
        completed = subprocess.run(
            [_FOOTPRINT, "-f", "bytes", "-j", path, "-p", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        if completed.returncode != 0:
            return None
        with open(path, "r", encoding="utf-8") as document:
            parsed = parse_footprint(json.load(document))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return parsed.get(pid)


def _process_start_time(pid: int) -> Optional[float]:
    """Process creation time, so a reused PID never inherits an old reading."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


@dataclass
class _Entry:
    start_time: Optional[float]
    value: Optional[int] = None
    measured_at: float = 0.0
    failures: int = 0


class FootprintPoller:
    """Background measurements for the processes the GPU view is showing.

    ``set_targets`` is called from the view each refresh; the thread measures
    at most :attr:`MAX_TARGETS` per pass and skips a process that is already
    fresh.  Repeated failures (another user's process, a sandboxed app) back
    off for a minute instead of hammering ``footprint``.
    """

    INTERVAL = 2.0
    IDLE_INTERVAL = 30.0
    PASS_INTERVAL = 0.5
    FAIL_LIMIT = 3
    BACKOFF = 60.0
    MAX_TARGETS = 32
    # The GPU view refreshes every ~2 s while it is on screen.  When it has
    # not handed over targets for a while the thread stops measuring, so
    # leaving view 5 does not keep spawning footprint(1) in the background.
    STALE_AFTER = 10.0

    def __init__(self, measure=None, start_time=None, clock=time.monotonic, start=True):
        self._measure = measure or read_process_graphics
        self._start_time = start_time or _process_start_time
        self._clock = clock
        self._entries: dict = {}
        self._targets: dict = {}
        self._targets_at = 0.0
        # Rotates the idle targets when there are more of them than
        # MAX_TARGETS, so a process beyond the cap still gets measured.
        self._rotation = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if start:
            self.start()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="macpm-footprint", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def set_targets(self, targets) -> None:
        """``targets`` is ``[(pid, busy)]``; anything else is forgotten."""
        with self._lock:
            self._targets = {
                pid: bool(busy) for pid, busy in targets if isinstance(pid, int)
            }
            self._targets_at = time.monotonic()
            live = set(self._targets)
            for pid in list(self._entries):
                if pid not in live:
                    self._entries.pop(pid, None)

    def get(self, pid: int) -> Optional[int]:
        with self._lock:
            entry = self._entries.get(pid)
            return entry.value if entry is not None else None

    def poll_once(self) -> None:
        with self._lock:
            items = sorted(
                self._targets.items(), key=lambda item: (not item[1], item[0])
            )
        busy = [item for item in items if item[1]]
        idle = [item for item in items if not item[1]]
        if len(items) > self.MAX_TARGETS and idle:
            slots = max(0, self.MAX_TARGETS - len(busy))
            if slots:
                start = self._rotation % len(idle)
                idle = idle[start:] + idle[:start]
                self._rotation = (start + slots) % len(idle)
        targets = (busy + idle)[: self.MAX_TARGETS]
        now = self._clock()
        for pid, busy in targets:
            if self._stop.is_set():
                return
            # Check identity before freshness: a reused PID must not keep the
            # previous process's number just because it was measured recently.
            identity = self._start_time(pid)
            if identity is None:
                continue
            with self._lock:
                entry = self._entries.get(pid)
                if entry is None or entry.start_time != identity:
                    entry = _Entry(start_time=identity)
                    self._entries[pid] = entry
                interval = self.INTERVAL if busy else self.IDLE_INTERVAL
                if entry.value is not None and now - entry.measured_at < interval:
                    continue
                if (
                    entry.failures >= self.FAIL_LIMIT
                    and now - entry.measured_at < self.BACKOFF
                ):
                    continue
            value = self._measure(pid)
            with self._lock:
                if self._entries.get(pid) is not entry:
                    continue  # target changed while measuring
                entry.measured_at = self._clock()
                if value is None:
                    entry.failures += 1
                else:
                    entry.value = value
                    entry.failures = 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() - self._targets_at < self.STALE_AFTER:
                try:
                    self.poll_once()
                except Exception:  # pragma: no cover - the thread must not die
                    pass
            self._stop.wait(self.PASS_INTERVAL)
