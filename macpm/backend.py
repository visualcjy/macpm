"""Platform selection.

Only this module decides which backend runs; every other module imports the
factory and stays platform-neutral.  Imports are lazy so a Linux install never
touches the macOS frameworks (whose ``ctypes.CDLL`` calls fail at import time).
"""

from __future__ import annotations

import os
import shutil
import sys

from .capabilities import Capabilities


def platform_name() -> str:
    """``macos`` or ``linux``.

    ``MACPM_PLATFORM`` overrides the detection so the Linux backend can be
    exercised on a Mac against a captured sysroot (``MACPM_SYSROOT``); it is a
    development aid, not a user-facing switch.
    """
    forced = os.environ.get("MACPM_PLATFORM", "").strip().lower()
    if forced in ("macos", "linux"):
        return forced
    return "macos" if sys.platform == "darwin" else "linux"


def is_linux() -> bool:
    return platform_name() == "linux"


def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:  # pragma: no cover - non-POSIX
        return False


def macos_capabilities() -> Capabilities:
    return Capabilities(
        platform="macos",
        root=is_root(),
    )


def create_sampler(interval: float):
    """Return the platform Sampler (macOS IOReport or Linux sysfs/proc)."""
    if is_linux():
        from .linux_backend.sampler import LinuxSampler

        return LinuxSampler(interval)
    from .sampler import Sampler

    return Sampler(interval)


def create_network_sampler():
    """Return the platform network sampler (nettop or /proc/net + ss)."""
    if is_linux():
        from .linux_backend.net import LinuxNetworkSampler

        return LinuxNetworkSampler()
    from .network import NetworkSampler

    return NetworkSampler()


def process_gpu_reader():
    """Callable returning ``{pid: gpu_ns}``, or None when unsupported."""
    if is_linux():
        from .linux_backend.gpu import shared_backend

        backend = shared_backend()
        if not backend.per_process_supported:
            return None
        return backend.per_process
    from .macos_gpu import read_gpu_times

    return read_gpu_times


def gpu_info_source():
    """Callable returning vendor card identity (name, driver, CUDA), or None."""
    if is_linux():
        from .linux_backend.gpu import shared_backend

        monitor = shared_backend()
        if monitor.backends:
            return monitor.info
    return None


def gpu_process_source():
    """Callable returning vendor compute processes for the GPU view, or None."""
    if is_linux():
        from .linux_backend.gpu import shared_backend

        monitor = shared_backend()
        if monitor.has_vendor_processes:
            return monitor.processes
    return None


def sensor_source():
    """``"smc"`` on macOS, ``"hwmon"`` on Linux."""
    return "hwmon" if is_linux() else "smc"


def default_capabilities() -> Capabilities:
    return macos_capabilities() if not is_linux() else _linux_capabilities()


def _linux_capabilities() -> Capabilities:
    """Cheap probe used before a sampler exists (tests, diagnostics)."""
    from .linux_backend.cpu import CpuSampler
    from .linux_backend.gpu import shared_backend
    from .linux_backend.power import RaplReader
    from .linux_backend.sysfs import SysRoot
    from .linux_backend.thermal import ThermalReader

    sysroot = SysRoot()
    cpu = CpuSampler(sysroot)
    gpu = shared_backend(sysroot).capabilities()
    thermal = ThermalReader(sysroot).read()
    e_cores, _ = cpu.cluster_counts()
    return Capabilities(
        platform="linux",
        root=is_root(),
        cpu_freq=cpu.has_frequency(),
        e_cluster=e_cores > 0,
        cpu_power=RaplReader(sysroot).available,
        gpu_util=gpu["util"],
        gpu_freq=gpu["freq"],
        gpu_power=gpu["power"],
        ane=False,
        temps=thermal.cpu_c is not None or thermal.gpu_c is not None,
        fans=bool(thermal.fans),
        proc_gpu=gpu["proc"],
        proc_net=bool(shutil.which("ss")),
        gpu_util_self_only=(
            not is_root() and shared_backend(sysroot).driver in ("i915", "xe")
        ),
    )
