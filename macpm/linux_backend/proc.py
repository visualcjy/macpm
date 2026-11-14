"""Per-process GPU time and socket ownership from ``/proc``.

DRM drivers that implement the kernel's "DRM client usage stats" export
``drm-engine-<class>: <ns>`` in ``/proc/<pid>/fdinfo/<fd>``.  i915, xe and
amdgpu do, so the process table gets a GPU% column without any vendor tool.
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

from .sysfs import SysRoot

_ENGINE_RE = re.compile(r"^drm-engine-([A-Za-z0-9_-]+):\s*(\d+)\s*ns", re.MULTILINE)
_CLIENT_RE = re.compile(r"^drm-client-id:\s*(\d+)", re.MULTILINE)
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


def _engine_key(name: str) -> str:
    return _ENGINE_ALIASES.get(name.lower(), name.lower())


def _is_drm_target(target: str) -> bool:
    return "/dri/" in target or target.endswith("[drm]") or "drm" in target


def scan_drm_clients(
    sys: SysRoot, budget: float = 0.75
) -> dict:
    """Return ``{pid: {engine: cumulative_ns}}`` for every DRM client.

    ``budget`` caps the wall time spent walking fd tables so a machine with
    thousands of processes cannot stall the 1 Hz sampler; the scan simply
    stops early and the next sample continues.
    """
    result: dict = {}
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
        clients: dict = {}
        for fd in fds:
            try:
                target = os.readlink(sys.path(f"/{fd_dir}/{fd}"))
            except OSError:
                continue
            if not _is_drm_target(target):
                continue
            text = sys.read(f"{proc_path}/fdinfo/{fd}")
            if not text:
                continue
            client_match = _CLIENT_RE.search(text)
            client_id = client_match.group(1) if client_match else fd
            engines = clients.setdefault(client_id, {})
            for engine, nanos in _ENGINE_RE.findall(text):
                key = _engine_key(engine)
                value = int(nanos)
                # Duplicated fds share one DRM client and report the same
                # totals; keep the maximum instead of adding them twice.
                if value > engines.get(key, 0):
                    engines[key] = value
        if clients:
            totals: dict = {}
            for engines in clients.values():
                for engine, nanos in engines.items():
                    totals[engine] = totals.get(engine, 0) + nanos
            if totals:
                result[pid] = totals
        if time.monotonic() - started > budget:
            break
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
                target = os.readlink(sys.path(f"{fd_dir}/{fd}"))
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
