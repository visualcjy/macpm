"""Linux backend: CPU, GPU, power, thermal and network sampling from sysfs/proc.

Every reader accepts a :class:`~macpm.linux_backend.sysfs.SysRoot`, which lets
tests and ``MACPM_SYSROOT`` replay a captured machine.  Nothing here imports
macOS frameworks, so the package is safe to import on any platform.
"""

from __future__ import annotations

from .cpu import CpuSampler, classify_cores
from .gpu import GpuBackend, GpuSample, shared_backend
from .net import LinuxNetworkSampler
from .power import EnergySampler, RaplReader
from .proc import per_process_gpu_ns, scan_drm_clients, socket_owners
from .sampler import LinuxSampler
from .soc import get_soc_info
from .sysfs import SysRoot
from .thermal import ThermalPressure, ThermalReader

__all__ = [
    "CpuSampler",
    "EnergySampler",
    "GpuBackend",
    "GpuSample",
    "LinuxNetworkSampler",
    "LinuxSampler",
    "RaplReader",
    "SysRoot",
    "ThermalPressure",
    "ThermalReader",
    "classify_cores",
    "get_soc_info",
    "per_process_gpu_ns",
    "scan_drm_clients",
    "shared_backend",
    "socket_owners",
]
