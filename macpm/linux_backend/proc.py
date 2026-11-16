"""Per-process GPU time and memory from ``/proc``.

DRM drivers that implement the kernel's "DRM client usage stats" export
``drm-engine-<class>: <ns>`` and the client's memory regions in
``/proc/<pid>/fdinfo/<fd>``.  i915, xe and amdgpu do, so the process table gets
a GPU% column without any vendor tool.

Memory is only charged to a process when the counters prove the bytes are its
own: a single visible holder, opened through a render node, every
``drm-shared-*`` counter present and zero, and a scan that reached the end.
Anything else reports ``n/a`` with a reason instead of a plausible-looking
number -- the bytes stay unattributed.  ``drm-shared-*`` and
``drm-resident-*`` count by different placements (preferred vs current), so
their difference is never used as "private" memory.

Reading another process' fdinfo needs ptrace-level permission, which root has;
as a normal user only macpm's own processes are visible and every other row
shows 0.

The same scan also powers the GPU utilisation gauge on Intel GPUs, which have
no ``gpu_busy_percent`` file: the sum of all clients' engine time divided by
wall time is the GPU busy ratio.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .sysfs import SysRoot

_ENGINE_RE = re.compile(r"^drm-engine-([A-Za-z0-9_-]+):\s*(\d+)\s*ns", re.MULTILINE)
_SOCKET_RE = re.compile(r"^socket:\[(\d+)\]$")

# Engines that count as "the GPU is busy".  Memory/copy engines are included
# because a DMA-heavy workload is still GPU work.
_ENGINE_ALIASES = {
    "render": "render",
    "rcs": "render",
    "gfx": "render",
    "compute": "compute",
    "ccs": "compute",
    "video": "video",
    "vcs": "video",
    "video-enhance": "video",
    "vecs": "video",
    "copy": "copy",
    "bcs": "copy",
    "enc": "video",
    "dec": "video",
}

# Why a process's memory is unknown.  ``attribute_drm_clients`` only sets one
# of these when it cannot prove the bytes are private; the UI then prints
# ``n/a`` instead of a guess.
REASON_LEGACY = "legacy"  # missing drm-shared-*: private vs shared is unknowable
REASON_SHARED_CLIENT = "shared_client"  # one client, more than one visible holder
REASON_SHARED_BUFFERS = "shared_buffers"  # a buffer another process can reach
REASON_PRIMARY = "primary"  # opened through cardN, not a render node
REASON_PARTIAL = "partial_scan"  # the scan budget ran out: a holder may be missing
REASON_INCOMPLETE = "incomplete"  # the process's fds/fdinfo were partly unreadable

_SIZE_FACTORS = {
    "": 1,
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000 ** 2,
    "mib": 1024 ** 2,
    "gb": 1000 ** 3,
    "gib": 1024 ** 3,
}


def _engine_key(name: str) -> str:
    return _ENGINE_ALIASES.get(name.lower(), name.lower())


def _is_drm_target(target: str) -> bool:
    return "/dri/" in target or target.endswith("[drm]") or "drm" in target


def _is_render_node(target: str) -> bool:
    return "/dri/renderD" in target


def _size_bytes(text: Optional[str]) -> Optional[int]:
    """Kernel size strings ("2048 KiB"); ``None`` when the value can't be read."""
    if not text:
        return None
    parts = text.strip().split()
    try:
        value = int(parts[0], 10)
    except (IndexError, ValueError):
        return None
    unit = parts[1].lower() if len(parts) > 1 else ""
    factor = _SIZE_FACTORS.get(unit)
    if factor is None:
        return None
    return value * factor


def _resident_bytes(fields: dict, regions: list) -> Optional[int]:
    """Sum the resident bytes of ``regions``; ``None`` if any of them is unreadable."""
    total = 0
    for region in regions:
        value = _size_bytes(fields.get("drm-resident-" + region))
        if value is None:
            return None
        total += value
    return total


def _shared_gate(driver: str, fields: dict, regions: list) -> Optional[int]:
    """Shared bytes for the client, or ``None`` when private memory can't be proven.

    Every ``drm-shared-*`` counter the driver is expected to expose must be
    present and parseable before a zero can be trusted: a missing counter must
    never turn reachable bytes into private bytes.
    """
    shared = {
        key[len("drm-shared-"):]: value
        for key, value in fields.items()
        if key.startswith("drm-shared-")
    }
    if driver == "amdgpu":
        required = ["vram", "gtt"]
    elif driver == "i915":
        required = ["system0"]
        if any(region.endswith("local0") for region in regions):
            required.append("local0")
    elif driver == "xe":
        required = ["gtt", "system"]
        required += [region for region in regions if re.fullmatch(r"vram\d+", region)]
    else:
        required = []
        if not shared:
            return None
    for name in required:
        if name not in shared:
            return None
    total = 0
    for text in shared.values():
        value = _size_bytes(text)
        if value is None:
            return None
        total += value
    return total


def parse_fdinfo(text: str) -> Optional[dict]:
    """One ``/proc/<pid>/fdinfo/<fd>`` file, or ``None`` when it has no DRM client."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    driver = fields.get("drm-driver")
    if not driver:
        return None

    regions = [
        key[len("drm-resident-"):]
        for key in fields
        if key.startswith("drm-resident-")
    ]
    vram_regions = [
        region
        for region in regions
        if region in ("vram", "vis-vram", "local0", "stolen-local0")
        or re.fullmatch(r"vram\d+", region)
    ]
    # GTT is GPU-accessible system memory.  i915 calls it system0 (and its
    # firmware-reserved part stolen-system0); amdgpu's resident-system is
    # CPU-only memory and is deliberately not charged.
    gtt_regions = [
        region for region in regions if region in ("gtt", "system0", "stolen-system0")
    ]

    engines = {}
    for key, value in fields.items():
        if not key.startswith("drm-engine-") or key.startswith("drm-engine-capacity-"):
            continue
        try:
            engines[_engine_key(key[len("drm-engine-"):])] = int(value.split()[0])
        except (IndexError, ValueError):
            engines = {}  # a torn counter makes this client's load unknown
            break

    return {
        "client_id": fields.get("drm-client-id") or "",
        "pdev": fields.get("drm-pdev") or "",
        "driver": driver,
        "vram": _resident_bytes(fields, vram_regions) if vram_regions else None,
        "gtt": _resident_bytes(fields, gtt_regions) if gtt_regions else None,
        "shared": _shared_gate(driver, fields, regions),
        "engines": engines,
    }


@dataclass
class DrmClient:
    """One DRM client held by one process.

    Duplicate file descriptors share a ``client_id`` and report the same
    totals; :func:`scan_drm` merges them so a process never counts twice.
    """

    pid: int
    client_id: str = ""
    pdev: str = ""
    driver: str = ""
    render_node: bool = False
    vram: Optional[int] = None
    gtt: Optional[int] = None
    shared: Optional[int] = None
    engines: dict = field(default_factory=dict)


@dataclass
class DrmScan:
    clients: list = field(default_factory=list)
    partial: bool = False
    unreadable: set = field(default_factory=set)


def _merge_client(existing: DrmClient, parsed: dict) -> None:
    """Fold another fd of the same client into an existing record."""
    for key in ("vram", "gtt", "shared"):
        if getattr(existing, key) is None and parsed.get(key) is not None:
            setattr(existing, key, parsed[key])
    for engine, nanos in parsed.get("engines", {}).items():
        if nanos > existing.engines.get(engine, 0):
            existing.engines[engine] = nanos


def scan_drm(sys: SysRoot, budget: float = 0.75) -> DrmScan:
    """Walk every process's DRM clients, with a wall-clock budget.

    ``budget`` caps the time spent walking fd tables so a machine with
    thousands of processes cannot stall the 1 Hz sampler.  When it runs out,
    ``partial`` is set: an unattributed byte count must then stay unknown or
    it could look like a complete total.
    """
    started = time.monotonic()
    clients: dict = {}
    unreadable = set()
    partial = False
    entries = [
        path
        for path in sys.glob("/proc/[0-9]*")
        if path.rsplit("/", 1)[-1].isdigit()
    ]
    for position, proc_path in enumerate(entries):
        pid = int(proc_path.rsplit("/", 1)[-1])
        fd_dir = proc_path + "/fd"
        try:
            fds = os.listdir(sys.path(fd_dir))
        except PermissionError:
            unreadable.add(pid)
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(sys.path(fd_dir + "/" + fd))
            except PermissionError:
                unreadable.add(pid)
                continue
            except OSError:
                continue
            if not _is_drm_target(target):
                continue
            text = sys.read(f"{proc_path}/fdinfo/{fd}")
            if text is None:
                unreadable.add(pid)
                continue
            parsed = parse_fdinfo(text)
            if parsed is None:
                continue
            parsed["client_id"] = parsed["client_id"] or fd
            key = (pid, parsed["client_id"])
            existing = clients.get(key)
            if existing is None:
                clients[key] = DrmClient(
                    pid=pid,
                    client_id=parsed["client_id"],
                    pdev=parsed["pdev"],
                    driver=parsed["driver"],
                    render_node=_is_render_node(target),
                    vram=parsed["vram"],
                    gtt=parsed["gtt"],
                    shared=parsed["shared"],
                    engines=dict(parsed["engines"]),
                )
            else:
                existing.render_node = existing.render_node or _is_render_node(target)
                _merge_client(existing, parsed)
        if time.monotonic() - started > budget:
            partial = position < len(entries) - 1
            break
    return DrmScan(list(clients.values()), partial=partial, unreadable=unreadable)


def attribute_drm_clients(scan: DrmScan, pdev: str = "") -> dict:
    """``{pid: {vram_bytes, gtt_bytes, reason}}`` for one card.

    ``pdev`` selects a card; an empty one means "the only card" (clients that
    report no ``drm-pdev``, or a machine with a single DRM device).
    """
    selected = [
        client for client in scan.clients if not pdev or client.pdev == pdev
    ]
    holders: dict = {}
    for client in scan.clients:
        holders.setdefault((client.pdev, client.client_id), set()).add(client.pid)

    by_pid: dict = {}
    for client in selected:
        by_pid.setdefault(client.pid, []).append(client)

    result = {}
    for pid, clients in by_pid.items():
        reason = ""
        if pid in scan.unreadable:
            reason = REASON_INCOMPLETE
        elif any(len(holders[(c.pdev, c.client_id)]) > 1 for c in clients):
            reason = REASON_SHARED_CLIENT
        elif any(c.shared is None for c in clients):
            reason = REASON_LEGACY
        elif any(c.shared for c in clients):
            reason = REASON_SHARED_BUFFERS
        elif not all(c.render_node for c in clients):
            reason = REASON_PRIMARY
        elif scan.partial:
            reason = REASON_PARTIAL

        vram = gtt = None
        if not reason:
            vrams = [client.vram for client in clients]
            gtts = [client.gtt for client in clients]
            if all(value is None for value in vrams):
                # An integrated GPU has no local memory: its GTT bytes are the
                # process's GPU memory, not spill.
                if any(value is None for value in gtts):
                    reason = REASON_LEGACY
                else:
                    gtt = sum(gtts)
            elif any(value is None for value in vrams) or any(
                value is None for value in gtts
            ):
                reason = REASON_LEGACY
            else:
                vram = sum(vrams)
                gtt = sum(gtts)

        result[pid] = {
            "vram_bytes": None if reason else vram,
            "gtt_bytes": None if reason else gtt,
            "reason": reason,
        }
    return result


def scan_drm_clients(sys: SysRoot, budget: float = 0.75) -> dict:
    """Return ``{pid: {engine: cumulative_ns}}`` for every DRM client.

    Kept for the callers that only need GPU time; the memory-aware callers use
    :func:`scan_drm` so one walk serves both.
    """
    result: dict = {}
    for client in scan_drm(sys, budget=budget).clients:
        totals = result.setdefault(client.pid, {})
        for engine, nanos in client.engines.items():
            totals[engine] = totals.get(engine, 0) + nanos
    return result


def per_process_gpu_ns(sys: SysRoot) -> dict:
    """``{pid: ns}`` using the busiest engine of each process."""
    return {
        pid: max(engines.values())
        for pid, engines in scan_drm_clients(sys).items()
        if engines
    }


def socket_owners(sys: SysRoot, budget: float = 1.0) -> dict:
    """Map socket inode to ``(pid, process name)`` by walking ``/proc/*/fd``."""
    owners: dict = {}
    started = time.monotonic()
    for proc_path in sys.glob("/proc/[0-9]*"):
        name = proc_path.rsplit("/", 1)[-1]
        if not name.isdigit():
            continue
        pid = int(name)
        fd_dir = proc_path + "/fd"
        try:
            fds = os.listdir(sys.path(fd_dir))
        except OSError:
            continue
        comm = ""
        for fd in fds:
            try:
                target = os.readlink(sys.path(f"/{fd_dir}/{fd}"))
            except OSError:
                continue
            match = _SOCKET_RE.match(target)
            if not match:
                continue
            if not comm:
                comm = (sys.read(f"/proc/{pid}/comm") or "").strip() or "?"
            owners[int(match.group(1))] = (pid, comm)
        if time.monotonic() - started > budget:
            break
    return owners
