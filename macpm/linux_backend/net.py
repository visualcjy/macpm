"""Network view backend for Linux.

Sockets come straight from ``/proc/net/{tcp,tcp6,udp,udp6}``: the kernel
exposes every socket and its state to any user, which is more accurate than
``nettop``'s CSV.  Ownership (PID) is resolved by matching the socket inode
against ``/proc/<pid>/fd``; that works for every process when macpm runs as
root and only for the current user otherwise.

Per-process byte counters do not exist in the kernel.  The closest unprivileged
source is the ``tcp_info`` dump that ``ss -ti`` prints (``bytes_acked`` /
``bytes_received`` per socket), joined back to the inode owner.  So the traffic
table is TCP-only, needs iproute2's ``ss`` and, without root, only lists the
current user's processes.
"""

from __future__ import annotations

import os
import re
import socket
import struct
import subprocess
import time
from typing import Callable, Optional

from ..netmodel import ProcessTraffic, SocketRow, _state_label
from .proc import socket_owners
from .sysfs import SysRoot

_PROC_NET = (
    ("/proc/net/tcp", "tcp", "4"),
    ("/proc/net/tcp6", "tcp", "6"),
    ("/proc/net/udp", "udp", "4"),
    ("/proc/net/udp6", "udp", "6"),
)

_STATE_NAMES = {
    "01": "Established",
    "02": "SynSent",
    "03": "SynRecv",
    "04": "FinWait1",
    "05": "FinWait2",
    "06": "TimeWait",
    "07": "Closed",
    "08": "CloseWait",
    "09": "LastAck",
    "0A": "Listen",
    "0B": "Closing",
    "0C": "NewSynRecv",
}

_INO_RE = re.compile(r"\bino:(\d+)")
_ACKED_RE = re.compile(r"\bbytes_acked:(\d+)")
_RECEIVED_RE = re.compile(r"\bbytes_received:(\d+)")

SS_TIMEOUT = 2.0


def _decode_address(value: str, family: str) -> tuple:
    """Decode ``0100007F:1F90`` into ``("127.0.0.1", 8080)``."""
    address, _, port = value.partition(":")
    try:
        number = int(port, 16)
    except ValueError:
        return "", 0
    try:
        if family == "4":
            raw = struct.pack("<I", int(address, 16))
            return socket.inet_ntoa(raw), number
        words = [int(address[index:index + 8], 16) for index in range(0, 32, 8)]
        raw = b"".join(struct.pack("<I", word) for word in words)
        return socket.inet_ntop(socket.AF_INET6, raw), number
    except (ValueError, OSError, struct.error):
        return "", 0


def parse_proc_net(text: str, proto: str, family: str) -> list:
    """Parse one ``/proc/net`` socket table into ``(SocketRow, inode)`` pairs."""
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        local_host, local_port = _decode_address(fields[1], family)
        remote_host, remote_port = _decode_address(fields[2], family)
        state = _STATE_NAMES.get(fields[3].upper(), fields[3].upper())
        try:
            inode = int(fields[9])
        except ValueError:
            inode = 0
        rows.append(
            (
                SocketRow(
                    proto=proto,
                    family=family,
                    local=f"{local_host}:{local_port}",
                    remote=f"{remote_host}:{remote_port}",
                    state=state if proto == "tcp" else "",
                    iface="",
                    pid=None,
                    process="",
                ),
                inode,
            )
        )
    return rows


def parse_ss_traffic(text: str) -> dict:
    """Return ``{inode: (rx_bytes, tx_bytes)}`` from ``ss -tanie`` output."""
    result = {}
    inode = None
    rx = tx = 0
    started = False

    def flush() -> None:
        if inode is not None:
            result[inode] = (rx, tx)

    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].strip():
            # A new socket header; the counters live on the indented lines.
            flush()
            started = True
            inode, rx, tx = None, 0, 0
            continue
        if not started:
            continue
        match = _INO_RE.search(line)
        if match:
            inode = int(match.group(1))
        match = _ACKED_RE.search(line)
        if match:
            tx = int(match.group(1))
        match = _RECEIVED_RE.search(line)
        if match:
            rx = int(match.group(1))
    flush()
    return result


def _run_ss() -> Optional[str]:
    try:
        result = subprocess.run(
            ["ss", "-tanie"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=SS_TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace")


class LinuxNetworkSampler:
    """Same interface as ``macpm.network.NetworkSampler`` for ``NetworkView``."""

    def __init__(
        self,
        sysroot: Optional[SysRoot] = None,
        clock: Callable[[], float] = time.monotonic,
        runner: Callable[[], Optional[str]] = _run_ss,
    ):
        self.sys = sysroot or SysRoot()
        self.clock = clock
        self.runner = runner
        self.sockets: list = []
        self.processes: dict = {}
        self.states: dict = {}
        self.listeners: list = []
        self.tcp_count = 0
        self.udp_count = 0
        self.sampled_at = 0.0
        self.error: Optional[str] = None
        self._seen: dict = {}
        self._conns: dict = {}
        self._owners: dict = {}
        self._owners_at = 0.0
        self.owner_interval = 2.0

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        self._seen.clear()

    # -- ownership ---------------------------------------------------------

    def _owner_map(self) -> dict:
        now = self.clock()
        if self._owners_at and now - self._owners_at < self.owner_interval:
            return self._owners
        self._owners = socket_owners(self.sys)
        self._owners_at = now
        return self._owners

    # -- traffic -----------------------------------------------------------

    def _traffic(
        self, key: tuple, rx: int, tx: int, now: float, conns: int
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
                    rx_bps = (
                        instant_rx if old_rx_bps == 0
                        else 0.5 * old_rx_bps + 0.5 * instant_rx
                    )
                    tx_bps = (
                        instant_tx if old_tx_bps == 0
                        else 0.5 * old_tx_bps + 0.5 * instant_tx
                    )
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

    def _traffic_snapshot(
        self, owners: dict, conns: dict, now: float, text: str
    ) -> dict:
        per_inode = parse_ss_traffic(text)
        totals: dict = {}
        for inode, (rx, tx) in per_inode.items():
            owner = owners.get(inode)
            if owner is None:
                continue
            pid, name = owner
            key = (pid, name)
            entry = totals.setdefault(key, [0, 0])
            entry[0] += rx
            entry[1] += tx
        processes = {
            key: self._traffic(key, rx, tx, now, conns.get(key, 0))
            for key, (rx, tx) in totals.items()
        }
        self._seen = {
            key: value for key, value in self._seen.items() if key in processes
        }
        return processes

    # -- sampling ----------------------------------------------------------

    def sample_full(self) -> bool:
        sockets = []
        for path, proto, family in _PROC_NET:
            text = self.sys.read(path, "")
            if text:
                sockets.extend(parse_proc_net(text, proto, family))
        if not sockets:
            self.error = "no socket data in /proc/net"
            return False

        owners = self._owner_map()
        attached = []
        for row, inode in sockets:
            owner = owners.get(inode)
            if owner is not None:
                row.pid, row.process = owner
            attached.append(row)

        now = self.clock()
        self.error = None
        self.sampled_at = now
        self.tcp_count = 0
        self.udp_count = 0
        states: dict = {}
        conns: dict = {}
        for row in attached:
            if row.proto == "tcp":
                self.tcp_count += 1
                label = _state_label(row.state)
                states[label] = states.get(label, 0) + 1
            else:
                self.udp_count += 1
            if row.pid is not None:
                key = (row.pid, row.process)
                conns[key] = conns.get(key, 0) + 1

        self.sockets = attached
        self.states = states
        self._conns = conns
        self.listeners = [row for row in attached if row.is_listen]
        text = self.runner()
        if text is None:
            self.error = "ss unavailable, per-process traffic needs iproute2"
            return True
        self.processes = self._traffic_snapshot(owners, conns, now, text)
        return True

    def sample_fast(self) -> bool:
        now = self.clock()
        owners = self._owner_map()
        text = self.runner()
        if text is None:
            self.error = "ss unavailable, per-process traffic needs iproute2"
            return False
        self.error = None
        self.sampled_at = now
        self.processes = self._traffic_snapshot(owners, self._conns, now, text)
        return True
