"""Rootless network view: sockets, listeners and per-process traffic.

On macOS the data comes from ``nettop`` in its CSV logging mode (``-L``).
Unlike ``netstat`` and ``psutil.net_connections``, ``nettop`` runs
unprivileged, can see sockets owned by other users (including root daemons)
and reports cumulative byte counters for every process and connection.
``nettop``'s CSV format is not documented, so everything is parsed by column
name and the view degrades to an error message instead of crashing.

On Linux ``macpm.linux_backend.net`` provides the same interface from
``/proc/net/*`` plus ``ss -tanie`` socket counters; this module keeps the
platform-neutral ``NetworkView`` and the macOS sampler.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import time
from typing import Callable, Optional

from blessed import Terminal

from .netinfo import NetworkInfoService, format_netinfo
from .netmodel import (
    ProcessTraffic,
    SocketRow,
    _display_addr,
    _format_rate,
    _port_of,
    _split_pid,
    _state_label,
)
from .tui import RESET_FG, _border, _bottom, _center, _pad, footer

NETTOP = "/usr/bin/nettop"

_FULL_ARGS = ["-x", "-n", "-L", "1"]
_FAST_ARGS = ["-P", "-x", "-n", "-L", "1"]
_TIMEOUT = 2.0

# ``nettop`` labels a connection row as ``tcp4 10.0.0.2:1234<->1.2.3.4:80`` and
# a per-process summary row as ``Google Chrome H.925``.
_CONN_RE = re.compile(r"^(tcp|udp)(4|6)\s+(\S+?)<->(\S+)$")

_TRAFFIC_SORTS = ["rx", "tx", "conns", "pid", "name"]
_LISTENER_SORTS = ["port", "process", "pid"]
_FOOTER = footer("q quit  1-5 view  Tab focus  s sort  ↑/↓ scroll  Space pause  ←/→ colour")
_FOOTER_PAUSED = footer("q quit  1-5 view  Tab focus  s sort  ↑/↓ scroll  Space resume  ←/→ colour")

_SORT_LABELS = {
    "rx": "↓/s",
    "tx": "↑/s",
    "conns": "CONN",
    "pid": "PID",
    "name": "NAME",
    "port": "ADDR",
    "process": "PROCESS",
}


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _run_nettop(args: list[str]) -> Optional[str]:
    """Run nettop once and return its stdout, or None when it is unavailable."""
    try:
        result = subprocess.run(
            [NETTOP, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace")


def _parse_nettop(text: str) -> tuple[list[SocketRow], dict[tuple[int, str], tuple[int, int]]]:
    """Parse one ``nettop -L`` sample.

    Returns the sockets plus the per-process byte counters.  Connection rows
    are emitted after the process row they belong to, which is how a PID is
    attached to a socket.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], {}
    index = {name: position for position, name in enumerate(header)}
    try:
        i_iface = index["interface"]
        i_state = index["state"]
        i_in = index["bytes_in"]
        i_out = index["bytes_out"]
    except KeyError:
        return [], {}

    try:
        rows = list(reader)
    except csv.Error:
        return [], {}

    needed = max(i_iface, i_state, i_in, i_out)
    current_pid: Optional[int] = None
    current_name = ""
    sockets: list[SocketRow] = []
    processes: dict[tuple[int, str], tuple[int, int]] = {}

    for row in rows:
        if len(row) <= needed:
            continue
        label = row[1].strip()
        if not label:
            continue
        match = _CONN_RE.match(label)
        if match is None:
            pid, name = _split_pid(label)
            current_pid, current_name = pid, name
            if pid is not None:
                processes[(pid, name)] = (_to_int(row[i_in]), _to_int(row[i_out]))
            continue
        proto, family, local, remote = match.groups()
        sockets.append(
            SocketRow(
                proto=proto,
                family=family,
                local=local,
                remote=remote,
                state=row[i_state].strip(),
                iface=row[i_iface].strip(),
                pid=current_pid,
                process=current_name,
                rx=_to_int(row[i_in]),
                tx=_to_int(row[i_out]),
            )
        )
    return sockets, processes


class NetworkSampler:
    """Two-rate sampler: full socket snapshots plus fast process snapshots."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        runner: Callable[[list[str]], Optional[str]] = _run_nettop,
    ):
        self.clock = clock
        self.runner = runner
        self.sockets: list[SocketRow] = []
        self.processes: dict[tuple[int, str], ProcessTraffic] = {}
        self.states: dict[str, int] = {}
        self.listeners: list[SocketRow] = []
        self.tcp_count = 0
        self.udp_count = 0
        self.sampled_at = 0.0
        self.error: Optional[str] = None
        self._seen: dict[tuple[int, str], tuple[float, int, int, float, float]] = {}
        self._conns: dict[tuple[int, str], int] = {}

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Drop rate baselines but keep the last snapshot for display."""
        self._seen.clear()

    def _traffic(
        self,
        key: tuple[int, str],
        rx: int,
        tx: int,
        now: float,
        conns: int,
    ) -> ProcessTraffic:
        pid, name = key
        rx_bps = tx_bps = 0.0
        previous = self._seen.get(key)
        if previous is not None:
            old_time, old_rx, old_tx, old_rx_bps, old_tx_bps = previous
            elapsed = now - old_time
            if elapsed > 0.2:
                if rx >= old_rx and tx >= old_tx:
                    instant_rx = (rx - old_rx) / elapsed
                    instant_tx = (tx - old_tx) / elapsed
                    # A light EMA keeps the table from jumping every sample.
                    rx_bps = instant_rx if old_rx_bps == 0 else 0.5 * old_rx_bps + 0.5 * instant_rx
                    tx_bps = instant_tx if old_tx_bps == 0 else 0.5 * old_tx_bps + 0.5 * instant_tx
                # A counter that went backwards means the process restarted:
                # start again from zero instead of reporting a huge rate.
            else:
                rx_bps, tx_bps = old_rx_bps, old_tx_bps
        self._seen[key] = (now, rx, tx, rx_bps, tx_bps)
        return ProcessTraffic(
            pid=pid,
            name=name,
            conns=conns,
            rx=rx,
            tx=tx,
            rx_bps=rx_bps,
            tx_bps=tx_bps,
        )

    # -- sampling ----------------------------------------------------------

    def sample_full(self) -> bool:
        text = self.runner(_FULL_ARGS)
        if text is None:
            self.error = "nettop unavailable"
            return False
        sockets, counters = _parse_nettop(text)
        if not sockets and not counters:
            self.error = "no socket data"
            return False
        now = self.clock()
        self.error = None
        self.sockets = sockets
        self.sampled_at = now
        self.tcp_count = 0
        self.udp_count = 0
        states: dict[str, int] = {}
        conns: dict[tuple[int, str], int] = {}
        for socket in sockets:
            if socket.proto == "tcp":
                self.tcp_count += 1
                label = _state_label(socket.state)
                states[label] = states.get(label, 0) + 1
            else:
                self.udp_count += 1
            if socket.pid is not None:
                key = (socket.pid, socket.process)
                conns[key] = conns.get(key, 0) + 1
        self.states = states
        self.listeners = [socket for socket in sockets if socket.is_listen]
        self._conns = conns
        self.processes = {
            key: self._traffic(key, rx, tx, now, conns.get(key, 0))
            for key, (rx, tx) in counters.items()
        }
        # Forget rate baselines for processes that no longer exist so a long
        # run does not accumulate stale entries.
        self._seen = {
            key: value for key, value in self._seen.items() if key in self.processes
        }
        return True

    def sample_fast(self) -> bool:
        text = self.runner(_FAST_ARGS)
        if text is None:
            self.error = "nettop unavailable"
            return False
        _, counters = _parse_nettop(text)
        if not counters:
            self.error = "no process data"
            return False
        now = self.clock()
        self.error = None
        self.sampled_at = now
        self.processes = {
            key: self._traffic(key, rx, tx, now, self._conns.get(key, 0))
            for key, (rx, tx) in counters.items()
        }
        return True


class NetworkView:
    """Network panel shown on key 4."""

    summary_height = 5

    def __init__(
        self,
        color: int = 2,
        full_interval: float = 5.0,
        fast_interval: float = 2.0,
        public_ip: bool = True,
        sampler: Optional[NetworkSampler] = None,
        netinfo: Optional[NetworkInfoService] = None,
    ):
        self.color = color
        self.full_interval = max(2.0, float(full_interval))
        self.fast_interval = max(1.0, float(fast_interval))
        if sampler is None:
            from .backend import create_network_sampler
            sampler = create_network_sampler()
        self.sampler = sampler
        self.netinfo = netinfo or NetworkInfoService(public=public_ip)
        self.focus = 0  # 0 = traffic, 1 = listeners
        self.traffic_sort = "rx"
        self.traffic_desc = True
        self.listener_sort = "port"
        self.listener_desc = False
        self.offsets = {0: 0, 1: 0}
        self.bodies = {0: 0, 1: 0}
        self.panes = (0,)
        self.paused = False
        self.ready = False
        self._last_full = 0.0
        self._last_fast = 0.0
        self._term: Optional[Terminal] = None
        self._network: dict = {}

    # -- lifecycle / keys --------------------------------------------------

    def prime(self) -> None:
        """Take a baseline snapshot and show the tables immediately."""
        # The public lookup runs in the background; the local read is cheap.
        self.netinfo.refresh_local(force=True)
        self.netinfo.refresh_public()
        self.sampler.reset()
        self.sampler.sample_full()
        now = time.monotonic()
        self._last_full = now
        self._last_fast = now
        self.ready = False
        self.paused = False
        self.offsets = {0: 0, 1: 0}

    def sample(self) -> None:
        if self.paused:
            return
        self.netinfo.refresh_local()
        self.netinfo.refresh_public()
        now = time.monotonic()
        if now - self._last_full >= self.full_interval:
            self._last_full = now
            self._last_fast = now
            if self.sampler.sample_full():
                self.ready = True
        elif now - self._last_fast >= self.fast_interval:
            self._last_fast = now
            if self.sampler.sample_fast():
                self.ready = True

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if not self.paused:
            # Do not average the paused period into the next traffic rates.
            self.sampler.reset()
            self._last_full = 0.0
            self._last_fast = 0.0

    def cycle_focus(self) -> None:
        if len(self.panes) < 2:
            self.focus = 0
            return
        index = self.panes.index(self.focus) if self.focus in self.panes else -1
        self.focus = self.panes[(index + 1) % len(self.panes)]

    def cycle_sort(self) -> None:
        if self.focus == 1 and 1 in self.panes:
            index = _LISTENER_SORTS.index(self.listener_sort) + 1
            self.listener_sort = _LISTENER_SORTS[index % len(_LISTENER_SORTS)]
            self.listener_desc = self.listener_sort == "pid"
            self.offsets[1] = 0
            return
        index = _TRAFFIC_SORTS.index(self.traffic_sort) + 1
        self.traffic_sort = _TRAFFIC_SORTS[index % len(_TRAFFIC_SORTS)]
        self.traffic_desc = self.traffic_sort in ("rx", "tx", "conns")
        self.offsets[0] = 0

    def scroll(self, delta: int) -> None:
        body = self.bodies.get(self.focus, 0)
        if body <= 0:
            return
        rows = self._traffic_rows() if self.focus == 0 else self._listener_rows()
        self.offsets[self.focus] = max(
            0, min(max(0, len(rows) - body), self.offsets[self.focus] + delta)
        )

    # -- rows --------------------------------------------------------------

    def _traffic_rows(self) -> list[ProcessTraffic]:
        key = {
            "rx": lambda row: (row.rx_bps, row.pid),
            "tx": lambda row: (row.tx_bps, row.pid),
            "conns": lambda row: (row.conns, row.pid),
            "pid": lambda row: row.pid,
            "name": lambda row: (row.name.lower(), row.pid),
        }[self.traffic_sort]
        return sorted(self.sampler.processes.values(), key=key, reverse=self.traffic_desc)

    def _listener_rows(self) -> list[SocketRow]:
        key = {
            "port": lambda row: (_port_of(row.local), row.pid or 0),
            "process": lambda row: (row.process.lower(), _port_of(row.local)),
            "pid": lambda row: (row.pid or 0, _port_of(row.local)),
        }[self.listener_sort]
        return sorted(self.sampler.listeners, key=key, reverse=self.listener_desc)

    # -- layout ------------------------------------------------------------

    def _layout(self, width: int, height: int):
        """Return ``(traffic, listeners)`` geometries, listeners may be None.

        A geometry is ``(row, column, width, height)``.  The summary box uses
        the first five rows and the footer is the last row.
        """
        available = height - self.summary_height - 1
        if available < 4:
            return None
        wide = width >= 110 and available >= 12
        stacked = available >= 12 and width >= 60
        if wide:
            left = max(38, int(width * 0.42))
            traffic = (self.summary_height, left, width - left, available)
            listeners = (self.summary_height, 0, left, available)
            return traffic, listeners
        if stacked:
            traffic_height = max(5, int(available * 0.55))
            traffic_height = min(traffic_height, available - 4)
            listen_height = available - traffic_height
            traffic = (self.summary_height, 0, width, traffic_height)
            listeners = (
                self.summary_height + traffic_height,
                0,
                width,
                listen_height,
            )
            return traffic, listeners
        traffic = (self.summary_height, 0, width, available)
        return traffic, None

    def _traffic_widths(self, inner: int) -> tuple[int, int, int, int]:
        pid_w, conn_w, rate_w = 6, 4, 7
        spaces = 4
        process_w = inner - (pid_w + conn_w + 2 * rate_w + spaces)
        if process_w < 8:
            conn_w = 0
            spaces = 3
            process_w = inner - (pid_w + 2 * rate_w + spaces)
        if process_w < 8:
            rate_w = 6
            process_w = inner - (pid_w + conn_w + 2 * rate_w + spaces)
        return pid_w, max(4, process_w), conn_w, rate_w

    def _listener_widths(self, inner: int) -> tuple[int, int, int]:
        addr_w = min(26, max(12, inner - 22))
        pid_w = 6
        process_w = max(6, inner - addr_w - pid_w - 2)
        return addr_w, pid_w, process_w

    def _traffic_header(self, widths) -> str:
        pid_w, process_w, conn_w, rate_w = widths
        cells = [f"{'PID':>{pid_w}}", _pad("PROCESS", process_w)]
        if conn_w:
            cells.append(f"{'CONN':>{conn_w}}")
        cells += [f"{'↓/s':>{rate_w}}", f"{'↑/s':>{rate_w}}"]
        return " ".join(cells)

    def _traffic_text(self, row: ProcessTraffic, widths) -> str:
        pid_w, process_w, conn_w, rate_w = widths
        cells = [f"{row.pid:>{pid_w}}", _pad(row.name, process_w)]
        if conn_w:
            cells.append(f"{row.conns:>{conn_w}}")
        cells += [f"{_format_rate(row.rx_bps):>{rate_w}}", f"{_format_rate(row.tx_bps):>{rate_w}}"]
        return " ".join(cells)

    def _listener_header(self, widths) -> str:
        addr_w, pid_w, process_w = widths
        return " ".join(
            [
                _pad("ADDR", addr_w),
                f"{'PID':>{pid_w}}",
                _pad("PROCESS", process_w),
            ]
        )

    def _listener_text(self, row: SocketRow, widths) -> str:
        addr_w, pid_w, process_w = widths
        pid = "-" if row.pid is None else str(row.pid)
        return " ".join(
            [
                _pad(_display_addr(row.local), addr_w),
                f"{pid:>{pid_w}}",
                _pad(row.process, process_w),
            ]
        )

    # -- rendering ---------------------------------------------------------

    def _pane(
        self,
        width: int,
        height: int,
        title: str,
        focused: bool,
        header: str,
        rows: list[str],
        offset: int,
        placeholder: str = "",
        right_title: str = "",
    ) -> list[str]:
        inner = max(0, width - 4)
        marker = "▶ " if focused else ""
        lines = [_border(width, f" {marker}{title} ", right_title)]
        lines.append("│ " + _pad(header, inner) + " │")
        body = max(0, height - 3)
        for index in range(body):
            if placeholder:
                text = _center(placeholder, inner) if index == 0 else ""
            else:
                position = offset + index
                text = rows[position] if position < len(rows) else ""
            lines.append("│ " + _pad(text, inner) + " │")
        lines.append(_bottom(width, ""))
        return lines

    def _traffic_title(self, rows: list[ProcessTraffic]) -> str:
        arrow = "↓" if self.traffic_desc else "↑"
        return (
            f"traffic by process {len(rows)} · "
            f"{_SORT_LABELS[self.traffic_sort]} {arrow}"
        )

    def _listener_title(self, rows: list[SocketRow]) -> str:
        arrow = "↓" if self.listener_desc else "↑"
        return (
            f"listening {len(rows)} · {_SORT_LABELS[self.listener_sort]} {arrow}"
        )

    def _range_title(self, pane: int, total: int, body: int) -> str:
        if total <= body or body <= 0:
            return ""
        offset = self.offsets[pane]
        last = min(offset + body, total)
        return f" {offset + 1}-{last}/{total} "

    def _identity_line(self) -> str:
        local, public = self.netinfo.snapshot()
        return format_netinfo(local, public)

    def _summary_lines(self) -> tuple[str, str]:
        states = self.sampler.states
        # Fixed-width fields keep the labels in the same columns while the
        # counts and rates change.
        line1 = (
            f"TCP {self.sampler.tcp_count:>4}  "
            f"EST {states.get('ESTABLISHED', 0):>4}  "
            f"LISTEN {states.get('LISTEN', 0):>4}  "
            f"CLOSE_WAIT {states.get('CLOSE_WAIT', 0):>4}  "
            f"TIME_WAIT {states.get('TIME_WAIT', 0):>4}  "
            f"UDP {self.sampler.udp_count:>4}"
        )
        down = _format_rate(self._network.get("ibyte_rate", 0))
        up = _format_rate(self._network.get("obyte_rate", 0))
        line2 = (
            f"↓ {down:>6}/s   "
            f"↑ {up:>6}/s   "
            f"sockets {self.sampler.tcp_count + self.sampler.udp_count:>4}   "
            f"processes {len(self.sampler.processes):>4}"
        )
        if self.sampler.error:
            line2 += f"   ! {self.sampler.error}"
        return line1, line2

    def _footer(self) -> str:
        if self.paused:
            return _FOOTER_PAUSED
        return _FOOTER

    def display(self, parse=None) -> None:
        term = self._term
        if term is None:
            term = self._term = Terminal()
        width, height = term.width, term.height
        if width < 40 or height < 12:
            return
        self._network = ((parse or {}).get("network") or {})

        layout = self._layout(width, height)
        if layout is None:
            return
        traffic_geom, listener_geom = layout
        panes = [0] if listener_geom is None else [0, 1]
        self.panes = tuple(panes)
        if self.focus not in self.panes:
            self.focus = self.panes[0]
        self.bodies[0] = max(0, traffic_geom[3] - 3)
        self.bodies[1] = max(0, listener_geom[3] - 3) if listener_geom else 0

        traffic_rows = self._traffic_rows()
        listener_rows = self._listener_rows()
        self.offsets[0] = max(0, min(self.offsets[0], max(0, len(traffic_rows) - self.bodies[0])))
        self.offsets[1] = max(0, min(self.offsets[1], max(0, len(listener_rows) - self.bodies[1])))

        accent = term.color(self.color)
        reset = RESET_FG
        out: list[str] = []

        def emit(row: int, column: int, text: str) -> None:
            out.append(term.move(row, column) + accent + text + reset)

        line1, line2 = self._summary_lines()
        inner = max(0, width - 4)
        emit(0, 0, _border(width, " Network "))
        emit(1, 0, "│ " + _pad(self._identity_line(), inner) + " │")
        emit(2, 0, "│ " + _pad(line1, inner) + " │")
        emit(3, 0, "│ " + _pad(line2, inner) + " │")
        emit(4, 0, _bottom(width, ""))

        traffic_widths = self._traffic_widths(traffic_geom[2] - 4)
        traffic_texts = [
            self._traffic_text(row, traffic_widths) for row in traffic_rows
        ]
        traffic_lines = self._pane(
            traffic_geom[2],
            traffic_geom[3],
            self._traffic_title(traffic_rows),
            self.focus == 0,
            self._traffic_header(traffic_widths),
            traffic_texts,
            self.offsets[0],
            placeholder="" if self.ready else (self.sampler.error or "collecting…"),
            right_title=self._range_title(0, len(traffic_rows), traffic_geom[3] - 3),
        )

        if listener_geom is not None:
            listener_widths = self._listener_widths(listener_geom[2] - 4)
            listener_texts = [
                self._listener_text(row, listener_widths) for row in listener_rows
            ]
            listener_lines = self._pane(
                listener_geom[2],
                listener_geom[3],
                self._listener_title(listener_rows),
                self.focus == 1,
                self._listener_header(listener_widths),
                listener_texts,
                self.offsets[1],
                right_title=self._range_title(1, len(listener_rows), listener_geom[3] - 3),
            )
        else:
            listener_lines = []

        for index, line in enumerate(traffic_lines):
            emit(traffic_geom[0] + index, traffic_geom[1], line)
        for index, line in enumerate(listener_lines):
            emit(listener_geom[0] + index, listener_geom[1], line)

        emit(height - 1, 0, _bottom(width, self._footer()))
        print("".join(out), end="", flush=True)
