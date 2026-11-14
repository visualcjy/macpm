"""Filesystem helpers for the Linux backend.

Every Linux reader goes through :class:`SysRoot` so the whole backend can be
pointed at a fixture tree with ``MACPM_SYSROOT`` for tests or debugging:

    MACPM_SYSROOT=/tmp/fakeroot python3 -m macpm --dump-capabilities

Nothing here needs root; whether a read succeeds is decided by the kernel.
"""

from __future__ import annotations

import glob
import os
from typing import Optional

# Interface names that are not real hardware; counting them double-counts
# traffic (containers, VMs, bridges) or inflates the rate (loopback).
_VIRTUAL_NET_PREFIXES = (
    "lo",
    "veth",
    "docker",
    "br-",
    "virbr",
    "tun",
    "tap",
    "wg",
    "zt",
    "bond",
    "dummy",
    "vmnet",
    "ppp",
    "sit",
    "ip6tnl",
)

# Disk names that alias another block device; counting both double-counts.
_VIRTUAL_DISK_PREFIXES = ("loop", "ram", "dm-", "sr", "fd", "md")


class SysRoot:
    """Read-only access to ``/proc`` and ``/sys`` under an optional prefix."""

    def __init__(self, root: Optional[str] = None):
        if root is None:
            root = os.environ.get("MACPM_SYSROOT", "")
        self.root = root.rstrip("/") if root else ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SysRoot({self.root or '/'})"

    def path(self, path: str) -> str:
        return self.root + path if self.root else path

    def read(self, path: str, default: Optional[str] = None) -> Optional[str]:
        try:
            with open(self.path(path), "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return default

    def read_bytes(self, path: str) -> Optional[bytes]:
        try:
            with open(self.path(path), "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def lines(self, path: str) -> list:
        text = self.read(path)
        return text.splitlines() if text else []

    def read_int(self, path: str, default: Optional[int] = None) -> Optional[int]:
        text = self.read(path)
        if text is None:
            return default
        try:
            return int(text.strip().split()[0])
        except (ValueError, IndexError):
            return default

    def read_float(self, path: str, default: Optional[float] = None) -> Optional[float]:
        text = self.read(path)
        if text is None:
            return default
        try:
            return float(text.strip().split()[0])
        except (ValueError, IndexError):
            return default

    def exists(self, path: str) -> bool:
        return os.path.exists(self.path(path))

    def glob(self, pattern: str) -> list:
        """Matches as logical paths (root prefix stripped), so every other
        accessor (``read``/``exists``/``listdir``) applies the prefix once."""
        matches = sorted(glob.glob(self.path(pattern)))
        if not self.root:
            return matches
        prefix = self.root
        return [
            match[len(prefix):] if match.startswith(prefix) else match
            for match in matches
        ]

    def listdir(self, path: str) -> list:
        try:
            return sorted(os.listdir(self.path(path)))
        except OSError:
            return []

    def link_target(self, path: str) -> str:
        try:
            return os.path.basename(os.readlink(self.path(path)).rstrip("/"))
        except OSError:
            return ""


def is_virtual_net(name: str) -> bool:
    return name.startswith(_VIRTUAL_NET_PREFIXES)


def is_virtual_disk(name: str) -> bool:
    return name.startswith(_VIRTUAL_DISK_PREFIXES)
