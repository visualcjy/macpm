"""SoC/CPU identity and display scales for Linux.

Linux has no ``sysctl machdep.*`` or ``ioreg``; the model name comes from the
device tree (arm64 boards), DMI (x86 laptops/desktops) or ``/proc/cpuinfo``.
The power scales are heuristics used only to size the gauges and charts: when
RAPL advertises a package power limit it is used as the CPU ceiling, otherwise
a core-count estimate is used and macpm grows the ceiling automatically when a
reading exceeds it.
"""

from __future__ import annotations

import re
from typing import Optional

from .cpu import CpuSampler
from .sysfs import SysRoot

_DMI_PRODUCT = "/sys/class/dmi/id/product_name"
_DMI_SYS_VENDOR = "/sys/class/dmi/id/sys_vendor"
_DMI_BOARD = "/sys/class/dmi/id/board_name"
_DEVICE_TREE_MODEL = "/sys/firmware/devicetree/base/model"

_RAPL_GLOB = "/sys/class/powercap/intel-rapl:*/constraint_0_max_power_uw"
_GPU_HWMON_GLOB = "/sys/class/hwmon/hwmon*/power1_cap"

_CPUINFO_MODEL_PATTERNS = (
    re.compile(r"^model name\s*:\s*(.+)$", re.MULTILINE),
    re.compile(r"^Model\s*:\s*(.+)$", re.MULTILINE),
    re.compile(r"^Hardware\s*:\s*(.+)$", re.MULTILINE),
    re.compile(r"^cpu model\s*:\s*(.+)$", re.MULTILINE),
)


def _read_nul_string(sys: SysRoot, path: str) -> str:
    raw = sys.read_bytes(path)
    if not raw:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def cpu_model(sys: SysRoot) -> str:
    """Best-effort human readable CPU/board name."""
    text = sys.read("/proc/cpuinfo") or ""
    for pattern in _CPUINFO_MODEL_PATTERNS:
        match = pattern.search(text)
        if match and match.group(1).strip():
            return _tidy_cpu_name(match.group(1).strip())

    model = _read_nul_string(sys, _DEVICE_TREE_MODEL)
    if model:
        return model

    product = (sys.read(_DMI_PRODUCT) or "").strip()
    vendor = (sys.read(_DMI_SYS_VENDOR) or "").strip()
    if product:
        return f"{vendor} {product}".strip() if vendor and vendor not in product else product

    board = (sys.read(_DMI_BOARD) or "").strip()
    return board or "Linux"


def _tidy_cpu_name(name: str) -> str:
    """Drop the vendor's marketing noise so the title fits a box."""
    cleaned = re.sub(r"\((?:R|TM)\)", "", name)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s*(?:\d+-Core Processor|\d+-Core|CPU)\s*$", "", cleaned)
    return cleaned.strip() or name


def _rapl_limit_watts(sys: SysRoot) -> Optional[int]:
    for path in sys.glob(_RAPL_GLOB):
        micro_watts = sys.read_int(path)
        if micro_watts and micro_watts > 0:
            return max(1, int(round(micro_watts / 1_000_000)))
    return None


def _gpu_power_cap_watts(sys: SysRoot) -> Optional[int]:
    for path in sys.glob(_GPU_HWMON_GLOB):
        micro_watts = sys.read_int(path)
        if micro_watts and micro_watts > 0:
            return max(1, int(round(micro_watts / 1_000_000)))
    return None


def get_soc_info(
    sysroot: Optional[SysRoot] = None, cpu: Optional[CpuSampler] = None
) -> dict:
    sys = sysroot or SysRoot()
    cpu = cpu or CpuSampler(sys)
    e_cores, p_cores = cpu.cluster_counts()
    core_count = cpu.cpu_count

    cpu_limit = _rapl_limit_watts(sys)
    if cpu_limit is None:
        # Rough package estimate, only used to scale the power gauge.
        cpu_limit = max(15, int(round(core_count * 5.0)))

    gpu_limit = _gpu_power_cap_watts(sys) or 60

    return {
        "name": cpu_model(sys),
        "core_count": core_count,
        "cpu_max_power": cpu_limit,
        "gpu_max_power": gpu_limit,
        "cpu_max_bw": 70,
        "gpu_max_bw": 70,
        "e_core_count": e_cores,
        "p_core_count": p_cores,
        "gpu_core_count": "?",
        "topology_method": cpu.method,
    }
