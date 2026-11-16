"""Per-process CPU / memory / GPU usage and the full-screen process table."""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import psutil
from blessed import Terminal

from .backend import platform_name, process_gpu_reader
from .tui import (  # noqa: F401 - _fit/_text_width are re-exported for tests
    RESET_FG,
    _border,
    _bottom,
    _center,
    _fit,
    _format_mem,
    _pad,
    _text_width,
    footer,
)

# The helpers are re-exported here for callers that imported them from this
# module before they moved to ``macpm.tui``.

# -- GPU time per process ------------------------------------------------
#
# macOS walks the IOAccelerator user clients (macpm.macos_gpu); Linux reads
# DRM fdinfo (macpm.linux_backend.proc).  ``backend.process_gpu_reader`` picks
# the right one and returns None when the platform has no usable counter.

def _ps_arguments() -> list:
    """``ps`` invocation that works on both BSD and procps."""
    if platform_name() == "macos":
        return ["/bin/ps", "-axo", "pid=,uid=,rss=,time=,ucomm="]
    executable = shutil.which("ps") or "/bin/ps"
    # ``ucomm`` is BSD-only; ``comm`` is the portable name.
    return [executable, "-eo", "pid=,uid=,rss=,time=,comm="]


# -- fallback for processes psutil cannot read ---------------------------


_PS_TIME = re.compile(
    r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)$"
)


def _parse_ps_time(text: str) -> Optional[float]:
    text = text.strip()
    try:
        return float(text)  # procps ``times=`` prints plain seconds
    except ValueError:
        pass
    match = _PS_TIME.match(text)
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
            _ps_arguments(),
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
    # Vendor *smi tools report the owning card and memory but usually no SM
    # percentage; ``util_known`` tells the GPU view whether ``gpu_pct`` means
    # anything (otherwise it renders a dash).
    gpu_card: Optional[str] = None
    gpu_mem_mb: Optional[float] = None
    util_known: bool = True
    model: Optional[str] = None
    gpu_list: Optional[list] = None
    # GPU-accessible system memory (GTT on amdgpu, system0 on i915).  Never
    # added to ``gpu_mem_mb``: it is a second place the bytes can live.
    gpu_shared_mb: Optional[float] = None
    # Why ``gpu_mem_mb`` is unknown (see linux_backend.proc.REASON_*).
    gpu_note: Optional[str] = None


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _argv_name(raw: bytes) -> str:
    """Process name from ``/proc/<pid>/cmdline``: argv[0] without its path."""
    if not raw:
        return ""
    first = raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return first.rsplit("/", 1)[-1]


def _parse_proc_stat(raw: bytes):
    """``(comm, starttime, cpu_ticks)`` from one ``/proc/<pid>/stat`` line.

    The comm field is wrapped in parentheses and may itself contain spaces or
    parentheses, so everything is split after the last ``)``.
    """
    close = raw.rfind(b")")
    open_paren = raw.find(b"(")
    if close <= open_paren:
        return None
    comm = raw[open_paren + 1:close].decode("utf-8", "replace")
    tail = raw[close + 2:].split()
    if len(tail) < 22:
        return None
    try:
        utime = int(tail[11])
        stime = int(tail[12])
        starttime = int(tail[19])
    except ValueError:
        return None
    return comm, starttime, utime + stime


class ProcessSampler:
    """CPU/memory/GPU rates from two process snapshots."""

    ATTRS = ["pid", "name", "uids", "cpu_times", "memory_info", "create_time"]
    # Only the busiest processes are built into rows; scanning 1000+ /proc
    # entries for names and memory was the slow part of the process view.
    LIMIT = 60

    def __init__(self):
        self._prev_cpu: dict[tuple[int, float], float] = {}
        self._prev_gpu: dict[int, int] = {}
        self._prev_ps_cpu: dict[int, float] = {}
        self._prev_time: Optional[float] = None
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._users: dict[int, str] = {}
        self._gpu_reader = None
        try:
            self._gpu_reader = process_gpu_reader()
        except Exception:
            self._gpu_reader = None
        self.gpu_available = self._gpu_reader is not None
        # PIDs that reported a GPU client in the latest sample.  The GPU view
        # uses this to decide which processes are worth a footprint measurement
        # instead of scanning every process on the machine.
        self.gpu_pids: set = set()
        self.error: Optional[str] = None

    def _gpu_times(self):
        if self._gpu_reader is None:
            return None
        try:
            return self._gpu_reader()
        except Exception:
            return None

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
        if platform_name() == "linux":
            return self._sample_linux()
        now = time.monotonic()
        elapsed = now - self._prev_time if self._prev_time is not None else 0.0
        gpu_times = self._gpu_times()
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
        self.gpu_pids = set(gpu_times or {})
        return rows


    def _sample_linux(self) -> list[ProcessRow]:
        """Rank every PID by CPU ticks from /proc, then read only the top ones."""
        now = time.monotonic()
        elapsed = now - self._prev_time if self._prev_time is not None else 0.0
        gpu_times = self._gpu_times() if self.gpu_available else None
        self.error = None
        max_cpu = 100.0 * self._cpu_count

        candidates = []
        prev_cpu = {}
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                try:
                    with open(f"/proc/{pid}/stat", "rb") as handle:
                        raw = handle.read()
                except OSError:
                    continue
                parsed = _parse_proc_stat(raw)
                if parsed is None:
                    continue
                name, starttime, ticks = parsed
                key = (pid, starttime)
                previous = self._prev_cpu.get(key)
                if previous is not None and elapsed > 0:
                    cpu_pct = max(
                        0.0, min(max_cpu, (ticks - previous) / _CLK_TCK / elapsed * 100)
                    )
                else:
                    cpu_pct = 0.0
                prev_cpu[key] = ticks
                candidates.append((cpu_pct, pid, name))
        except Exception as error:
            self.error = str(error)

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        # The table is ranked by CPU, but a process can hold a GPU while using
        # almost no CPU (an inference worker between requests).  Keep every
        # process with GPU time, not only the busiest CPU ones.
        chosen = list(candidates[:self.LIMIT])
        chosen_pids = {pid for _, pid, _ in chosen}
        if gpu_times:
            for item in candidates[self.LIMIT:]:
                if item[1] in gpu_times and item[1] not in chosen_pids:
                    chosen.append(item)
                    chosen_pids.add(item[1])
        rows = []
        for cpu_pct, pid, name in chosen:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as handle:
                    name = _argv_name(handle.read()) or name
            except OSError:
                pass
            mem_bytes = 0
            try:
                with open(f"/proc/{pid}/statm", "r", encoding="ascii") as handle:
                    mem_bytes = int(handle.read().split()[1]) * _PAGE_SIZE
            except (OSError, IndexError, ValueError):
                pass
            uid = None
            try:
                uid = os.stat(f"/proc/{pid}").st_uid
            except OSError:
                pass
            gpu_pct = 0.0
            if gpu_times is not None and elapsed > 0:
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
                    name="macpm" if pid == os.getpid() else (name or ""),
                    user=self._username(uid),
                    cpu_pct=cpu_pct,
                    mem_bytes=mem_bytes,
                    gpu_pct=gpu_pct,
                )
            )

        self._prev_cpu = prev_cpu
        self._prev_gpu = gpu_times or {}
        self._prev_time = now
        self.gpu_pids = set(gpu_times or {})
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
_WIDTHS = {"pid": 7, "user": 10, "cpu": 7, "mem": 7, "gpu": 6}
_NUMERIC = {"pid", "cpu", "mem", "gpu"}
_SORT_CYCLE = ["cpu", "mem", "gpu", "pid", "name", "user"]
_FOOTER = footer("q quit  1-5 view  s sort  ↑/↓ scroll  Space pause  ←/→ colour")
_FOOTER_PAUSED = footer("q quit  1-5 view  s sort  ↑/↓ scroll  Space resume  ←/→ colour")


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
