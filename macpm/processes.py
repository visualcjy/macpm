"""Per-process CPU / memory / GPU usage and the full-screen process table."""

from __future__ import annotations

import ctypes
import os
import pwd
import re
import subprocess
import time
from ctypes import POINTER, byref, c_char_p, c_longlong, c_uint32, c_void_p
from dataclasses import dataclass
from typing import Optional

import psutil
from blessed import Terminal

from .tui import RESET_FG, _border, _bottom, _center, _fit, _format_mem, _pad, _text_width

# The helpers are re-exported here for callers that imported them from this
# module before they moved to ``macpm.tui``.

# -- GPU time per process ------------------------------------------------
#
# The GPU's IOAccelerator user clients expose an accumulatedGPUtime counter in
# the IORegistry.  Walking them gives GPU usage per process without root, using
# the same per-process GPU counter the system tools read.

_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_IOKIT = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

_CF.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
_CF.CFStringCreateWithCString.restype = c_void_p
_CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, ctypes.c_long, c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFArrayGetCount.argtypes = [c_void_p]
_CF.CFArrayGetCount.restype = ctypes.c_long
_CF.CFArrayGetValueAtIndex.argtypes = [c_void_p, ctypes.c_long]
_CF.CFArrayGetValueAtIndex.restype = c_void_p
_CF.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
_CF.CFDictionaryGetValue.restype = c_void_p
_CF.CFNumberGetValue.argtypes = [c_void_p, ctypes.c_int, c_void_p]
_CF.CFNumberGetValue.restype = ctypes.c_bool
_CF.CFGetTypeID.argtypes = [c_void_p]
_CF.CFGetTypeID.restype = ctypes.c_ulong
_CF.CFStringGetTypeID.restype = ctypes.c_ulong
_CF.CFArrayGetTypeID.restype = ctypes.c_ulong
_CF.CFDictionaryGetTypeID.restype = ctypes.c_ulong
_CF.CFNumberGetTypeID.restype = ctypes.c_ulong
_CF.CFRelease.argtypes = [c_void_p]

_IOKIT.IOServiceMatching.argtypes = [c_char_p]
_IOKIT.IOServiceMatching.restype = c_void_p
_IOKIT.IOServiceGetMatchingServices.argtypes = [c_uint32, c_void_p, POINTER(c_uint32)]
_IOKIT.IOServiceGetMatchingServices.restype = ctypes.c_int
_IOKIT.IOIteratorNext.argtypes = [c_uint32]
_IOKIT.IOIteratorNext.restype = c_uint32
_IOKIT.IOObjectRelease.argtypes = [c_uint32]
_IOKIT.IORegistryEntryGetChildIterator.argtypes = [c_uint32, c_char_p, POINTER(c_uint32)]
_IOKIT.IORegistryEntryGetChildIterator.restype = ctypes.c_int
_IOKIT.IORegistryEntryCreateCFProperty.argtypes = [c_uint32, c_void_p, c_void_p, c_uint32]
_IOKIT.IORegistryEntryCreateCFProperty.restype = c_void_p

_UTF8 = 0x08000100
_CF_NUMBER_SINT64 = 4


def _cfstr(value: str) -> int:
    return _CF.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)


def _cf_string(ref) -> str:
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(256)
    if _CF.CFStringGetCString(ref, buf, len(buf), _UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


def _cf_number(ref) -> Optional[int]:
    out = c_longlong(0)
    if _CF.CFNumberGetValue(ref, _CF_NUMBER_SINT64, byref(out)):
        return out.value
    return None


_CREATOR_KEY = _cfstr("IOUserClientCreator")
_USAGE_KEY = _cfstr("AppUsage")
_GPU_TIME_KEY = _cfstr("accumulatedGPUTime")


def _client_pid(client) -> Optional[int]:
    prop = _IOKIT.IORegistryEntryCreateCFProperty(client, _CREATOR_KEY, None, 0)
    if not prop:
        return None
    try:
        if _CF.CFGetTypeID(prop) != _CF.CFStringGetTypeID():
            return None
        creator = _cf_string(prop)
    finally:
        _CF.CFRelease(prop)
    if not creator.startswith("pid "):
        return None
    try:
        return int(creator[4:].split(",", 1)[0])
    except ValueError:
        return None


def _client_gpu_ns(client) -> int:
    prop = _IOKIT.IORegistryEntryCreateCFProperty(client, _USAGE_KEY, None, 0)
    if not prop:
        return 0
    try:
        if _CF.CFGetTypeID(prop) != _CF.CFArrayGetTypeID():
            return 0
        total = 0
        for index in range(_CF.CFArrayGetCount(prop)):
            entry = _CF.CFArrayGetValueAtIndex(prop, index)
            if not entry or _CF.CFGetTypeID(entry) != _CF.CFDictionaryGetTypeID():
                continue
            value = _CF.CFDictionaryGetValue(entry, _GPU_TIME_KEY)
            if value and _CF.CFGetTypeID(value) == _CF.CFNumberGetTypeID():
                ns = _cf_number(value)
                if ns is not None and ns >= 0:
                    total += ns
        return total
    finally:
        _CF.CFRelease(prop)


def read_gpu_times() -> Optional[dict[int, int]]:
    """Accumulated GPU time in ns per pid, or None when the registry fails."""
    matching = _IOKIT.IOServiceMatching(b"IOAccelerator")
    if not matching:
        return None
    iterator = c_uint32()
    if _IOKIT.IOServiceGetMatchingServices(0, matching, byref(iterator)) != 0:
        return None

    totals: dict[int, int] = {}
    try:
        while True:
            service = _IOKIT.IOIteratorNext(iterator)
            if not service:
                break
            try:
                children = c_uint32()
                if _IOKIT.IORegistryEntryGetChildIterator(
                    service, b"IOService", byref(children)
                ) != 0:
                    continue
                try:
                    while True:
                        client = _IOKIT.IOIteratorNext(children)
                        if not client:
                            break
                        try:
                            try:
                                pid = _client_pid(client)
                                if pid is not None:
                                    totals[pid] = totals.get(pid, 0) + _client_gpu_ns(client)
                            except Exception:
                                pass
                        finally:
                            _IOKIT.IOObjectRelease(client)
                finally:
                    _IOKIT.IOObjectRelease(children)
            finally:
                _IOKIT.IOObjectRelease(service)
    finally:
        _IOKIT.IOObjectRelease(iterator)
    return totals


# -- fallback for processes psutil cannot read ---------------------------


_PS_TIME = re.compile(
    r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)$"
)


def _parse_ps_time(text: str) -> Optional[float]:
    match = _PS_TIME.match(text.strip())
    if not match:
        return None
    parts = match.groupdict()
    return (
        int(parts["days"] or 0) * 86400
        + int(parts["hours"] or 0) * 3600
        + int(parts["minutes"]) * 60
        + float(parts["seconds"])
    )


def _parse_ps(text: str) -> list[tuple[int, int, int, float, str]]:
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        pid, uid, rss, cputime, name = fields
        seconds = _parse_ps_time(cputime)
        if seconds is None:
            continue
        try:
            rows.append((int(pid), int(uid), int(rss), seconds, name.strip()))
        except ValueError:
            continue
    return rows


def _read_ps() -> Optional[list[tuple[int, int, int, float, str]]]:
    try:
        child = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,uid=,rss=,time=,ucomm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError:
        return None
    try:
        output, _ = child.communicate(timeout=3.0)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        return None
    if child.returncode != 0:
        return None
    # Do not show the transient `ps` process itself.
    return [row for row in _parse_ps(output.decode("utf-8", "ignore")) if row[0] != child.pid]


# -- sampling -------------------------------------------------------------


@dataclass
class ProcessRow:
    pid: int
    name: str
    user: str
    cpu_pct: float
    mem_bytes: int
    gpu_pct: float


class ProcessSampler:
    """CPU/memory/GPU rates from two process snapshots."""

    ATTRS = ["pid", "name", "uids", "cpu_times", "memory_info", "create_time"]

    def __init__(self):
        self._prev_cpu: dict[tuple[int, float], float] = {}
        self._prev_gpu: dict[int, int] = {}
        self._prev_ps_cpu: dict[int, float] = {}
        self._prev_time: Optional[float] = None
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._users: dict[int, str] = {}
        try:
            self.gpu_available = read_gpu_times() is not None
        except Exception:
            self.gpu_available = False
        self.error: Optional[str] = None

    def _username(self, uid: Optional[int]) -> str:
        if uid is None:
            return "?"
        name = self._users.get(uid)
        if name is None:
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = str(uid)
            self._users[uid] = name
        return name

    def reset(self) -> None:
        self._prev_cpu.clear()
        self._prev_gpu.clear()
        self._prev_ps_cpu.clear()
        self._prev_time = None

    def sample(self) -> list[ProcessRow]:
        now = time.monotonic()
        elapsed = now - self._prev_time if self._prev_time is not None else 0.0
        try:
            gpu_times = read_gpu_times() if self.gpu_available else None
        except Exception:
            gpu_times = None
        self.error = None
        rows: list[ProcessRow] = []
        prev_cpu: dict[tuple[int, float], float] = {}
        max_cpu = 100.0 * self._cpu_count

        try:
            for proc in psutil.process_iter(self.ATTRS, ad_value=None):
                try:
                    info = proc.info
                except psutil.Error:
                    continue
                if not info:
                    continue
                pid = info.get("pid")
                cpu_times = info.get("cpu_times")
                if pid is None or cpu_times is None:
                    continue

                key = (pid, info.get("create_time") or 0.0)
                cpu_seconds = float(cpu_times.user + cpu_times.system)
                previous = self._prev_cpu.get(key)
                if previous is not None and elapsed > 0:
                    cpu_pct = max(
                        0.0, min(max_cpu, (cpu_seconds - previous) / elapsed * 100)
                    )
                else:
                    cpu_pct = 0.0
                prev_cpu[key] = cpu_seconds

                mem_info = info.get("memory_info")
                mem_bytes = mem_info.rss if mem_info is not None else 0

                gpu_pct = 0.0
                if gpu_times and elapsed > 0:
                    current_gpu = gpu_times.get(pid)
                    previous_gpu = self._prev_gpu.get(pid)
                    if (
                        current_gpu is not None
                        and previous_gpu is not None
                        and current_gpu >= previous_gpu
                    ):
                        gpu_pct = min(
                            100.0, (current_gpu - previous_gpu) / elapsed / 1e9 * 100
                        )

                uids = info.get("uids")
                name = info.get("name") or ""
                if pid == os.getpid():
                    name = "macpm"
                rows.append(
                    ProcessRow(
                        pid=pid,
                        name=name,
                        user=self._username(uids.real if uids is not None else None),
                        cpu_pct=cpu_pct,
                        mem_bytes=mem_bytes,
                        gpu_pct=gpu_pct,
                    )
                )
        except Exception as error:  # psutil can fail while processes come and go
            self.error = str(error)

        # psutil cannot read CPU/memory of other users' processes; `ps` can.
        ps_rows = _read_ps()
        if ps_rows is None:
            self._prev_ps_cpu = {}
        else:
            seen = {row.pid for row in rows}
            prev_ps = self._prev_ps_cpu
            next_ps: dict[int, float] = {}
            for pid, uid, rss, cpu_seconds, name in ps_rows:
                next_ps[pid] = cpu_seconds
                if pid in seen:
                    continue
                previous_ps = prev_ps.get(pid)
                if previous_ps is not None and elapsed > 0:
                    cpu_pct = max(
                        0.0, min(max_cpu, (cpu_seconds - previous_ps) / elapsed * 100)
                    )
                else:
                    cpu_pct = 0.0

                gpu_pct = 0.0
                if gpu_times and elapsed > 0:
                    current_gpu = gpu_times.get(pid)
                    previous_gpu = self._prev_gpu.get(pid)
                    if (
                        current_gpu is not None
                        and previous_gpu is not None
                        and current_gpu >= previous_gpu
                    ):
                        gpu_pct = min(
                            100.0, (current_gpu - previous_gpu) / elapsed / 1e9 * 100
                        )

                rows.append(
                    ProcessRow(
                        pid=pid,
                        name=name,
                        user=self._username(uid),
                        cpu_pct=cpu_pct,
                        mem_bytes=rss * 1024,
                        gpu_pct=gpu_pct,
                    )
                )
            self._prev_ps_cpu = next_ps

        self._prev_cpu = prev_cpu
        self._prev_gpu = gpu_times or {}
        self._prev_time = now
        return rows


# -- full-screen process table -------------------------------------------

_HEADERS = {
    "pid": "PID",
    "name": "NAME",
    "user": "USER",
    "cpu": "CPU%",
    "mem": "MEM",
    "gpu": "GPU%",
}
_WIDTHS = {"pid": 6, "user": 10, "cpu": 7, "mem": 7, "gpu": 6}
_NUMERIC = {"pid", "cpu", "mem", "gpu"}
_SORT_CYCLE = ["cpu", "mem", "gpu", "pid", "name", "user"]
_FOOTER = " q quit  1-4 view  s sort  ↑/↓ scroll  Space pause  ←/→ colour "
_FOOTER_PAUSED = " q quit  1-4 view  s sort  ↑/↓ scroll  Space resume  ←/→ colour "


class ProcessView:
    """Two-box process table shown on key 3."""

    def __init__(
        self, color: int = 2, soc_title: str = "", refresh_interval: float = 2.0
    ):
        self.color = color
        self.soc_title = soc_title
        self.refresh_interval = max(0.5, float(refresh_interval))
        self._last_sample = 0.0
        self.sampler = ProcessSampler()
        self.rows: list[ProcessRow] = []
        self._sorted: Optional[list[ProcessRow]] = None
        self.sort = "cpu"
        self.desc = True
        self.offset = 0
        self.ready = False
        self.paused = False
        self.summary: list[str] = []
        self._term = None
        self._body_height = 0

    # -- lifecycle / keys --------------------------------------------------

    def prime(self) -> None:
        """Reset the baselines and show "collecting" until the next sample."""
        self.sampler.reset()
        self.rows = self.sampler.sample()
        self._sorted = None
        self._last_sample = time.monotonic()
        self.ready = False
        self.paused = False
        self.offset = 0

    def sample(self) -> None:
        if self.paused:
            return
        now = time.monotonic()
        # The first sample after entering the view is immediate; afterwards the
        # process table refreshes less often than the summary to keep macpm's
        # own CPU usage low.
        if self.ready and now - self._last_sample < self.refresh_interval:
            return
        self._last_sample = now
        self.rows = self.sampler.sample()
        self._sorted = None
        self.ready = True

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if not self.paused:
            # Do not average the paused period into the next CPU/GPU rates.
            self.sampler.reset()
            self._last_sample = 0.0

    def cycle_sort(self) -> None:
        cycle = [
            key for key in _SORT_CYCLE
            if key != "gpu" or self.sampler.gpu_available
        ]
        index = (cycle.index(self.sort) + 1) % len(cycle) if self.sort in cycle else 0
        self.sort = cycle[index]
        self.desc = self.sort in ("cpu", "mem", "gpu")
        self._sorted = None
        self.offset = 0

    def scroll(self, delta: int) -> None:
        max_offset = max(0, len(self.rows) - self._body_height)
        self.offset = max(0, min(max_offset, self.offset + delta))

    # -- layout ------------------------------------------------------------

    def _layout(self, width: int) -> tuple[list[str], dict[str, int]]:
        columns = ["pid", "name", "user", "cpu", "mem", "gpu"]
        if not self.sampler.gpu_available:
            columns.remove("gpu")
        widths = dict(_WIDTHS)
        inner = width - 4
        while True:
            fixed = sum(widths[key] for key in columns if key != "name")
            fixed += max(0, len(columns) - 1)
            name_width = inner - fixed
            if name_width >= 10:
                break
            removable = [
                key for key in ("gpu", "user", "mem", "cpu")
                if key in columns and key not in ("name", "pid", self.sort)
            ]
            if not removable:
                break
            columns.remove(removable[0])
        widths["name"] = max(8, name_width)
        return columns, widths

    def _sort_key(self, row: ProcessRow):
        value = {
            "pid": row.pid,
            "name": row.name.lower(),
            "user": row.user.lower(),
            "cpu": row.cpu_pct,
            "mem": row.mem_bytes,
            "gpu": row.gpu_pct,
        }[self.sort]
        return (value, row.pid)

    # -- rendering ---------------------------------------------------------

    def _cell(self, value, width: int, right: bool) -> str:
        return _pad(str(value), width, right)

    def _row_text(self, row: ProcessRow, columns: list[str], widths: dict[str, int]) -> str:
        values = {
            "pid": row.pid,
            "name": row.name,
            "user": row.user,
            "cpu": f"{row.cpu_pct:.1f}",
            "mem": _format_mem(row.mem_bytes),
            "gpu": f"{row.gpu_pct:.1f}" if self.sampler.gpu_available else "--",
        }
        return " ".join(
            self._cell(values[key], widths[key], key in _NUMERIC) for key in columns
        )

    def _header_text(self, columns: list[str], widths: dict[str, int]) -> str:
        cells = []
        for key in columns:
            label = _HEADERS[key]
            if key == self.sort:
                label += " ↓" if self.desc else " ↑"
            cells.append(self._cell(label, widths[key], key in _NUMERIC))
        return " ".join(cells)

    def display(self, summary=None) -> None:
        term = self._term
        if term is None:
            term = self._term = Terminal()
        if summary is not None:
            self.summary = list(summary)
        width, height = term.width, term.height
        if width < 24 or height < 10:
            return

        if self._sorted is None:
            self._sorted = sorted(self.rows, key=self._sort_key, reverse=self.desc)
        rows = self._sorted
        columns, widths = self._layout(width)

        # top box: borders + three summary lines; process box: borders + header
        top_height = 5
        body_height = max(0, height - top_height - 3)
        self._body_height = body_height
        self.offset = max(0, min(self.offset, max(0, len(rows) - body_height)))

        accent = term.color(self.color)
        reset = RESET_FG
        content_width = max(0, width - 4)

        out = [term.move(0, 0), accent, _border(width, f" {self.soc_title} "), reset]
        for index in range(3):
            text = self.summary[index] if index < len(self.summary) else ""
            out += [
                term.move(1 + index, 0),
                accent,
                "│ " + _pad(text, content_width) + " │",
                reset,
            ]
        out += [term.move(4, 0), accent, _bottom(width, ""), reset]

        proc_title = f" proc {len(rows)} "
        if self.paused:
            proc_title = f" proc {len(rows)} (paused) "
        out += [
            term.move(5, 0),
            accent,
            _border(width, proc_title, " s sort "),
            reset,
        ]

        header = _pad(self._header_text(columns, widths), content_width)
        out += [
            term.move(6, 0),
            accent,
            "│ " + header + " │",
            reset,
        ]

        for index in range(body_height):
            entry = index + self.offset
            if not self.ready:
                content = _center("collecting…", content_width)
            elif entry < len(rows):
                content = _pad(self._row_text(rows[entry], columns, widths), content_width)
            else:
                content = " " * content_width
            out.append(term.move(7 + index, 0))
            out.append(accent + "│ " + content + " │" + reset)

        footer = _FOOTER_PAUSED if self.paused else _FOOTER
        out += [term.move(height - 1, 0), accent, _bottom(width, footer), reset]
        out.append(term.move(height - 2, 0))
        print("".join(out), end="", flush=True)
