"""Platform-neutral network view data model and formatting helpers.

The macOS sampler parses ``nettop`` and the Linux sampler reads ``/proc/net``;
both produce :class:`SocketRow`/:class:`ProcessTraffic` so
``macpm.network.NetworkView`` stays identical on both systems.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

STATE_LABELS = {
    "Established": "ESTABLISHED",
    "Listen": "LISTEN",
    "CloseWait": "CLOSE_WAIT",
    "TimeWait": "TIME_WAIT",
    "FinWait1": "FIN_WAIT_1",
    "FinWait2": "FIN_WAIT_2",
    "LastAck": "LAST_ACK",
    "SynSent": "SYN_SENT",
    "SynRecv": "SYN_RECV",
    "Closed": "CLOSED",
    "Closing": "CLOSING",
}

_PID_RE = re.compile(r"^(.*)\.(\d+)$")
# IPv4 uses ``host:port``; nettop prints IPv6 as ``host.port``.
_ADDR_RE = re.compile(r"^(.*)[.:](\d+)$")


@dataclass
class SocketRow:
    proto: str  # "tcp" or "udp"
    family: str  # "4" or "6"
    local: str
    remote: str
    state: str  # nettop's CamelCase state, empty for UDP
    iface: str
    pid: Optional[int]
    process: str
    rx: int = 0
    tx: int = 0

    @property
    def is_listen(self) -> bool:
        return self.proto == "tcp" and self.state == "Listen"


@dataclass
class ProcessTraffic:
    pid: int
    name: str
    conns: int = 0
    rx: int = 0
    tx: int = 0
    rx_bps: float = 0.0
    tx_bps: float = 0.0


def _state_label(state: str) -> str:
    return STATE_LABELS.get(state, state.upper())


def _split_pid(label: str) -> tuple:
    match = _PID_RE.match(label)
    if match is None:
        return None, label
    return int(match.group(2)), match.group(1)


def _split_addr(addr: str) -> tuple:
    match = _ADDR_RE.match(addr.strip())
    if match is None:
        return addr.strip(), ""
    return match.group(1), match.group(2)


def _display_addr(addr: str) -> str:
    """Normalise the mixed IPv4/IPv6 address notation for display."""
    host, port = _split_addr(addr)
    if not port:
        return addr
    if host in ("*", "0.0.0.0", "::", "[::]"):
        return f"*:{port}"
    if ":" in host:
        return f"[{host.strip('[]')}]:{port}"
    return f"{host}:{port}"


def _port_of(addr: str) -> int:
    try:
        return int(_split_addr(addr)[1])
    except ValueError:
        return 0


def _format_rate(bps: float) -> str:
    """Format a byte-per-second value as a short ``1.2M`` cell."""
    if bps < 1:
        return "0"
    value = float(bps)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
        value /= 1024
    return "0"
