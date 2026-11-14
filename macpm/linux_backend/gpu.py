"""GPU utilisation, frequency and power for Linux.

There is no cross-vendor GPU API on Linux, so the backend detects the driver
once and uses whatever the kernel exports:

* ``amdgpu`` — ``gpu_busy_percent``, ``pp_dpm_sclk`` and hwmon power, all
  readable without root.
* ``i915`` / ``xe`` — frequency from the card's ``gt*`` files and utilisation
  from the sum of every DRM client's engine time in ``fdinfo`` (needs root to
  see other processes; macpm's own client is always visible).
* ``nvidia`` — ``nvidia-smi``.

Anything else (nouveau, vendor ARM GPUs, Apple GPU under Asahi) reports
``n/a`` until a driver starts exporting counters.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from .proc import scan_drm_clients
from .sysfs import SysRoot

_DRM_GLOB = "/sys/class/drm/card[0-9]*"
_SMCLK_RE = re.compile(r"(\d+)\s*Mhz", re.IGNORECASE)
_CARD_RE = re.compile(r"^card\d+$")
_NVIDIA_INFO_GLOB = "/proc/driver/nvidia/gpus/*/information"



@dataclass
class GpuSample:
    available: bool = False
    name: str = ""
    driver: str = ""
    busy_pct: Optional[float] = None
    freq_mhz: Optional[float] = None
    max_freq_mhz: Optional[float] = None
    power_w: Optional[float] = None
    mem_used_mb: Optional[float] = None
    mem_total_mb: Optional[float] = None
    temp_c: Optional[float] = None
    index: int = 0


def _driver_name(sys: SysRoot, card: str) -> str:
    return sys.link_target(card + "/device/driver")


def _cards(sys: SysRoot) -> list:
    cards = []
    for path in sys.glob(_DRM_GLOB):
        if not _CARD_RE.match(path.rsplit("/", 1)[-1]):
            continue
        device = path + "/device"
        if not sys.exists(device):
            continue
        cards.append(
            {
                "path": path,
                "device": device,
                "driver": _driver_name(sys, path),
                "vendor": (sys.read(device + "/vendor") or "").strip(),
            }
        )
    return cards


def _hwmon_power(sys: SysRoot, device: str) -> Optional[float]:
    for path in sys.glob(device + "/hwmon/hwmon*/power1_average"):
        micro_watts = sys.read_int(path)
        if micro_watts and micro_watts > 0:
            return micro_watts / 1_000_000.0
    for path in sys.glob(device + "/hwmon/hwmon*/power1_input"):
        micro_watts = sys.read_int(path)
        if micro_watts and micro_watts > 0:
            return micro_watts / 1_000_000.0
    return None


def _vram_mb(sys: SysRoot, device: str) -> tuple:
    """``(used_mb, total_mb)`` from the driver's VRAM files, or (None, None)."""
    for used_name, total_name in (
        ("mem_info_vram_used", "mem_info_vram_total"),
        ("mem_info_vis_vram_used", "mem_info_vis_vram_total"),
    ):
        total = sys.read_int(device + "/" + total_name)
        used = sys.read_int(device + "/" + used_name)
        if total:
            return (
                used / (1024.0 * 1024.0) if used is not None else None,
                total / (1024.0 * 1024.0),
            )
    total = sys.read_int(device + "/lmem_total_bytes")
    if total:
        return None, total / (1024.0 * 1024.0)
    return None, None


def _hwmon_temp(sys: SysRoot, device: str) -> Optional[float]:
    for path in sys.glob(device + "/hwmon/hwmon*/temp1_input"):
        milli_c = sys.read_int(path)
        if milli_c is not None and 0 < milli_c < 130000:
            return milli_c / 1000.0
    return None


_BUSY_FILES = ("gpu_busy_percent", "gpu_busy", "busy_percent", "utilization", "load")


def _generic_busy(sys: SysRoot, device: str) -> Optional[float]:
    """Common utilisation file names used by out-of-tree accelerator drivers."""
    for name in _BUSY_FILES:
        value = sys.read_int(device + "/" + name)
        if value is not None and 0 <= value <= 100:
            return float(value)
    return None


def _generic_freq(sys: SysRoot, device: str) -> Optional[float]:
    value = sys.read_int(device + "/gt_cur_freq_mhz")
    if value:
        return float(value)
    for path in sys.glob(device + "/devfreq/*/cur_freq"):
        hertz = sys.read_int(path)
        if hertz:
            return hertz / 1_000_000.0
    return None


def _active_smclk(sys: SysRoot, device: str) -> Optional[float]:
    text = sys.read(device + "/pp_dpm_sclk") or ""
    active = None
    for line in text.splitlines():
        match = _SMCLK_RE.search(line)
        if not match:
            continue
        if line.rstrip().endswith("*"):
            return float(match.group(1))
        if active is None:
            active = float(match.group(1))
    return active


# Vendor management CLIs that speak an nvidia-smi-style CSV dialect.  The
# query switch and the property names differ per vendor, so each tool maps its
# own fields onto macpm's normalised row keys.
_FIELD_ORDER = (
    "index",
    "name",
    "busy",
    "mem_used",
    "mem_total",
    "temp",
    "power",
    "freq",
    "max_freq",
)

_SMI_TOOLS = {
    "iluvatar": {
        "binary": "ixsmi",
        "env": "MACPM_IXSMI",
        "glob": ("/usr/local/corex-*/bin/ixsmi",),
        "flag": "--query-gpu",
        "fields": {
            "index": "index",
            "name": "name",
            "busy": "utilization.gpu",
            "mem_used": "memory.used",
            "mem_total": "memory.total",
            "temp": "temperature.gpu",
            "power": "gpu.power.draw",
            "freq": "clocks.current.sm",
            "max_freq": "clocks.max.sm",
        },
        "alt_power": "power.draw",
        "info_fields": (
            "name,driver_version,cuda_version,count,vbios_version,"
            "pcie.link.gen.current,pcie.link.width.current,serial"
        ),
        "info_fallback": "name,driver_version,count",
        "pmon": ("pmon", "-c", "1"),
    },
    "denglin": {
        "binary": "dlsmi",
        "env": "MACPM_DLSMI",
        "glob": ("/usr/bin/dlsmi", "/usr/local/*/bin/dlsmi"),
        "flag": "--query-gpu",
        "fields": {
            "index": "index",
            "name": "name",
            "busy": "utilization.gpu",
            "mem_used": "memory.used",
            "mem_total": "memory.total",
            "temp": "temperature.gpu",
            "power": "power.draw",
            "freq": "clocks.current.sm",
            "max_freq": "clocks.max.sm",
        },
        "alt_power": "gpu.power.draw",
        "info_fields": (
            "name,driver_version,count,vbios_version,"
            "pcie.link.gen.current,pcie.link.width.current,serial"
        ),
        "info_fallback": "name,driver_version,count",
        "pmon": None,
    },
    "teco": {
        "binary": "teco-smi",
        "env": "MACPM_TECO",
        "glob": ("/opt/tecoai/bin/teco-smi", "/usr/local/teco*/bin/teco-smi"),
        "flag": "--query-device",
        "fields": {
            "index": "index",
            "name": "name",
            "busy": "utilization.spe",
            "mem_used": "memory.used",
            "mem_total": "memory.total",
            "temp": "temperature",
            "power": "power.chip",
            "freq": "clocks.spe",
            "max_freq": "clocks.max.spe",
        },
        "alt_power": "power.hbm",
        "info_fields": (
            "name,driver_version,count,vbios_version,"
            "pcie.link.gen.current,pcie.link.width.current,serial"
        ),
        "info_fallback": "name,driver_version,count",
        "pmon": None,
        # teco pmon repeats every process for every device, so summing the
        # per-device memory would multiply it by the card count.
        "sum_memory": False,
        # teco-smi lists physical cards, but each holds several SDAA dies and
        # the serials do not reveal the board grouping.
        "derive_cards": False,
    },
}

# Display adapters that are not compute devices (BMC/legacy framebuffers).
_IGNORED_DRIVERS = {"ast", "mgag200", "simpledrm", "efifb", "vesafb", "bochs"}

_smi_cache: dict = {}


def reset_smi_cache(name: str = "") -> None:
    """Forget cached vendor queries (all tools, or one)."""
    if name:
        _smi_cache.pop(name, None)
    else:
        _smi_cache.clear()


# Backwards compatible name from the first Iluvatar-only version.
reset_ixsmi_cache = reset_smi_cache


def smi_tool(name: str) -> str:
    """Locate a vendor CLI: explicit env var, PATH, then its install dirs."""
    config = _SMI_TOOLS.get(name)
    if not config:
        return ""
    configured = os.environ.get(config["env"], "")
    if configured:
        return configured
    found = shutil.which(config["binary"])
    if found:
        return found
    for pattern in config["glob"]:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return ""


def _run_tool(command: str, *args: str, timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            [command, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace")


def _number(text: str):
    """``"13 W"``/``"500 MHz"``/``"0 %"``/``"N/A"`` -> float or None."""
    token = (text or "").strip().split()[0] if (text or "").strip() else ""
    try:
        return float(token.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _smi_parse(text: str) -> list:
    rows = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        rows.append(
            {
                "index": index,
                "name": parts[1],
                "busy": _number(parts[2]),
                "mem_used": _number(parts[3]),
                "mem_total": _number(parts[4]),
                "temp": _number(parts[5]),
                "power": _number(parts[6]),
                "freq": _number(parts[7]) if len(parts) > 7 else None,
                "max_freq": _number(parts[8]) if len(parts) > 8 else None,
            }
        )
    return rows


def smi_rows(name: str) -> list:
    """All cards from one vendor tool, cached for two seconds."""
    config = _SMI_TOOLS.get(name)
    if not config:
        return []
    cache = _smi_cache.setdefault(
        name, {"at": -1e9, "rows": [], "path": "", "pmon": None}
    )
    now = time.monotonic()
    if cache["rows"] and now - cache["at"] < 2.0:
        return cache["rows"]

    tool = cache["path"] or smi_tool(name)
    cache["path"] = tool
    rows = []
    if tool:
        # Older/newer builds disagree on the power property name; try both.
        for power_field in (config["fields"]["power"], config["alt_power"]):
            fields = dict(config["fields"], power=power_field)
            query = ",".join(fields[key] for key in _FIELD_ORDER)
            text = _run_tool(
                tool, f"{config['flag']}={query}", "--format=csv,noheader"
            )
            rows = _smi_parse(text)
            if rows:
                break
    cache["at"] = now
    cache["rows"] = rows
    return rows


def smi_info(tool_name: str) -> dict:
    """Identity of the cards: name, versions, PCIe and dies per board."""
    config = _SMI_TOOLS.get(tool_name)
    tool = smi_tool(tool_name)
    if not config or not tool:
        return {}
    cache = _smi_cache.setdefault(
        tool_name, {"at": -1e9, "rows": [], "path": "", "pmon": None}
    )
    if "info" in cache:
        return cache["info"]

    without_cuda = config["info_fields"].replace(",cuda_version", "")
    info = {}
    for fields in (config["info_fields"], without_cuda, config["info_fallback"]):
        text = _run_tool(
            tool, f"{config['flag']}={fields}", "--format=csv,noheader"
        )
        if not text.strip():
            continue
        keys = [part.strip() for part in fields.split(",")]
        rows = [
            [part.strip() for part in line.split(",")]
            for line in text.splitlines()
            if line.strip()
        ]
        first = dict(zip(keys, rows[0]))

        serials = set()
        if "serial" in keys:
            index = keys.index("serial")
            for row in rows:
                if index < len(row):
                    serial = row[index]
                    if serial and serial not in ("N/A", "[N/A]", "None"):
                        serials.add(serial)

        count = _number(first.get("count", ""))
        cards = len(serials) or None
        dies = None
        if config.get("derive_cards", True):
            if count and cards and count % cards == 0:
                dies = int(count // cards)
        else:
            cards = None
        info = {
            "vendor": tool_name,
            "name": first.get("name", ""),
            "driver": first.get("driver_version", ""),
            "cuda": first.get("cuda_version", ""),
            "count": count,
            "cards": cards,
            "dies": dies,
            "vbios": first.get("vbios_version", ""),
            "pcie_gen": _number(first.get("pcie.link.gen.current", "")),
            "pcie_width": _number(first.get("pcie.link.width.current", "")),
        }
        break
    cache["info"] = info
    return info


def ixsmi_rows() -> list:
    """Backwards compatible alias used by the first Iluvatar tests."""
    return smi_rows("iluvatar")


class VendorSmiBackend:
    """One accelerator card, sampled through its vendor ``*smi`` tool."""

    def __init__(
        self,
        sysroot: SysRoot,
        index: int,
        name: str,
        tool_name: str = "iluvatar",
        tool: str = "",
    ):
        self.sys = sysroot
        self.gpu_index = index
        self.name = name or "Accelerator"
        self.card = ""
        self.tool_name = tool_name
        self.driver = tool_name
        self.tool = tool or smi_tool(tool_name)
        self._previous_pmon: dict = {}
        self._pmon_at = 0.0
        self._accumulated: dict = {}

    @property
    def available(self) -> bool:
        return bool(self.tool)

    @property
    def per_process_supported(self) -> bool:
        return bool(self.tool) and _SMI_TOOLS[self.tool_name]["pmon"] is not None

    def capabilities(self) -> dict:
        row = self._row()
        return {
            "util": row is not None and row["busy"] is not None,
            "freq": row is not None and row["freq"] is not None,
            "power": row is not None and row["power"] is not None,
            # Every vendor CLI can at least list the compute processes.
            "proc": True,
        }

    def _row(self):
        for row in smi_rows(self.tool_name):
            if row["index"] == self.gpu_index:
                return row
        return None

    def sample(self, elapsed: float) -> GpuSample:
        row = self._row()
        if row is None:
            return GpuSample(available=True, name=self.name, driver=self.driver)
        return GpuSample(
            available=True,
            name=row["name"] or self.name,
            driver=self.driver,
            busy_pct=row["busy"],
            freq_mhz=row["freq"],
            max_freq_mhz=row["max_freq"],
            power_w=row["power"],
            mem_used_mb=row["mem_used"],
            mem_total_mb=row["mem_total"],
            temp_c=row["temp"],
        )

    def per_process(self) -> dict:
        """Cumulative nanosecond equivalents accumulated from ``ixsmi pmon``."""
        if not _pmon_supported(self.tool_name):
            return {}
        now = time.monotonic()
        elapsed = now - self._pmon_at if self._pmon_at else 0.0
        pmon = _SMI_TOOLS[self.tool_name]["pmon"]
        text = _run_tool(self.tool, *pmon)
        current = {}
        for line in text.splitlines():
            if line.lstrip().startswith("#") or not line.strip():
                continue
            fields = line.split()
            if len(fields) < 3:
                continue
            try:
                pid = int(fields[1])
            except ValueError:
                continue
            percent = _number(fields[3]) if len(fields) > 3 else None
            if percent is None:
                percent = _number(fields[4]) if len(fields) > 4 else None
            if percent is None:
                continue
            current[pid] = max(0.0, min(100.0, percent))
        if elapsed > 0 and current:
            for pid, percent in current.items():
                self._accumulated[pid] = self._accumulated.get(pid, 0.0) + (
                    percent / 100.0 * elapsed * 1e9
                )
        self._pmon_at = now
        return {pid: int(ns) for pid, ns in self._accumulated.items()}


# Backwards compatible name from the first Iluvatar-only version.
IxsmiBackend = VendorSmiBackend


def _pmon_supported(tool_name: str) -> bool:
    """Whether the vendor's per-process monitor reports an SM percentage."""
    config = _SMI_TOOLS.get(tool_name)
    if not config or config["pmon"] is None:
        return False
    cache = _smi_cache.setdefault(
        tool_name, {"at": -1e9, "rows": [], "path": "", "pmon": None}
    )
    if cache["pmon"] is not None:
        return cache["pmon"]
    tool = cache["path"] or smi_tool(tool_name)
    supported = False
    if tool:
        text = _run_tool(tool, *config["pmon"])
        for line in text.splitlines():
            if line.lstrip().startswith("#") or not line.strip():
                continue
            fields = line.split()
            if len(fields) > 3 and _number(fields[3]) is not None:
                supported = True
                break
    cache["pmon"] = supported
    return supported


def _smi_backends(sys: SysRoot) -> list:
    backends = []
    for tool_name in _SMI_TOOLS:
        tool = smi_tool(tool_name)
        if not tool:
            continue
        for row in smi_rows(tool_name):
            backends.append(
                VendorSmiBackend(sys, row["index"], row["name"], tool_name, tool)
            )
    return backends


class GpuBackend:
    """One detected GPU; ``available`` is False when nothing usable exists."""

    def __init__(
        self,
        sysroot: Optional[SysRoot] = None,
        driver: str = "",
        card: str = "",
        name: str = "",
        gpu_index: int = -1,
    ):
        self.sys = sysroot or SysRoot()
        self.driver = driver
        self.card = card
        self.name = name
        self.gpu_index = gpu_index
        self._previous_totals: Optional[dict] = None
        self._clients_at = 0.0
        self._clients: dict = {}
        self._nvidia_at = 0.0
        self._nvidia_sample: Optional[GpuSample] = None
        self.scan_interval = 0.8
        self.smi_interval = 2.0

    # -- detection ---------------------------------------------------------

    @classmethod
    def detect(cls, sysroot: Optional[SysRoot] = None) -> "GpuBackend":
        """The single best card (kept for callers that only want one GPU)."""
        monitor = GpuMonitor.detect(sysroot)
        if not monitor.backends:
            return cls(sysroot)
        priority = {"amdgpu": 0, "i915": 1, "xe": 1, "radeon": 2}
        return sorted(
            monitor.backends,
            key=lambda backend: (
                priority.get(backend.driver, 3),
                backend.gpu_index,
                backend.card,
            ),
        )[0]

    @property
    def available(self) -> bool:
        return bool(self.driver)

    @property
    def per_process_supported(self) -> bool:
        return self.driver in ("amdgpu", "i915", "xe")

    def capabilities(self) -> dict:
        """Static report of what this driver exposes (used before sampling)."""
        if not self.driver:
            return {"util": False, "freq": False, "power": False, "proc": False}
        device = (self.card or "") + "/device"

        if self.driver == "nvidia":
            smi = bool(nvidia_smi_path())
            return {"util": smi, "freq": smi, "power": smi, "proc": smi}
        if self.driver == "amdgpu":
            return {
                "util": self.sys.exists(device + "/gpu_busy_percent"),
                "freq": self.sys.exists(device + "/pp_dpm_sclk"),
                "power": bool(self.sys.glob(device + "/hwmon/hwmon*/power1_average")),
                "proc": True,
            }
        if self.driver in ("i915", "xe"):
            freq = self.sys.exists(device + "/gt_cur_freq_mhz") or self.sys.exists(
                device + "/tile0/gt0/freq0/cur_freq"
            )
            return {
                "util": True,
                "freq": freq,
                "power": bool(self.sys.glob(device + "/hwmon/hwmon*/power1_average")),
                "proc": True,
            }
        return {
            "util": any(self.sys.exists(device + "/" + name) for name in _BUSY_FILES),
            "freq": self.sys.exists(device + "/gt_cur_freq_mhz")
            or bool(self.sys.glob(device + "/devfreq/*/cur_freq")),
            "power": bool(self.sys.glob(device + "/hwmon/hwmon*/power1_average")),
            "proc": False,
        }

    # -- sampling ----------------------------------------------------------

    def sample(self, elapsed: float) -> GpuSample:
        if not self.driver:
            return GpuSample(available=False, name=self.name, driver=self.driver)
        if self.driver == "amdgpu":
            return self._sample_amdgpu()
        if self.driver in ("i915", "xe"):
            return self._sample_intel(elapsed)
        if self.driver == "nvidia":
            return self._sample_nvidia()
        return self._sample_generic()

    def _sample_generic(self) -> GpuSample:
        """Best effort for drivers macpm has no specific support for.

        The card is still reported (so multi-GPU hosts list all their
        accelerators) and any of the usual sysfs counters are picked up.
        """
        device = (self.card or "") + "/device"
        used_mb, total_mb = _vram_mb(self.sys, device)
        return GpuSample(
            available=True,
            name=self.name or "GPU",
            driver=self.driver,
            busy_pct=_generic_busy(self.sys, device),
            freq_mhz=_generic_freq(self.sys, device),
            power_w=_hwmon_power(self.sys, device),
            mem_used_mb=used_mb,
            mem_total_mb=total_mb,
            temp_c=_hwmon_temp(self.sys, device),
        )

    def _sample_amdgpu(self) -> GpuSample:
        device = (self.card or "") + "/device"
        busy = self.sys.read_int(device + "/gpu_busy_percent")
        used_mb, total_mb = _vram_mb(self.sys, device)
        return GpuSample(
            available=True,
            name=self.name,
            driver=self.driver,
            busy_pct=float(busy) if busy is not None else None,
            freq_mhz=_active_smclk(self.sys, device),
            max_freq_mhz=None,
            power_w=_hwmon_power(self.sys, device),
            mem_used_mb=used_mb,
            mem_total_mb=total_mb,
            temp_c=_hwmon_temp(self.sys, device),
        )

    def _sample_intel(self, elapsed: float) -> GpuSample:
        device = (self.card or "") + "/device"
        freq = self.sys.read_int(device + "/gt_cur_freq_mhz")
        maximum = self.sys.read_int(device + "/gt_max_freq_mhz")
        if freq is None:
            freq = self.sys.read_int(device + "/tile0/gt0/freq0/cur_freq")
            maximum = self.sys.read_int(device + "/tile0/gt0/freq0/max_freq")
        used_mb, total_mb = _vram_mb(self.sys, device)
        return GpuSample(
            available=True,
            name=self.name,
            driver=self.driver,
            busy_pct=self._clients_busy(elapsed),
            freq_mhz=float(freq) if freq else None,
            max_freq_mhz=float(maximum) if maximum else None,
            power_w=_hwmon_power(self.sys, device),
            mem_used_mb=used_mb,
            mem_total_mb=total_mb,
            temp_c=_hwmon_temp(self.sys, device),
        )

    def _clients_busy(self, elapsed: float) -> Optional[float]:
        clients = self._drm_clients()
        totals: dict = {}
        for engines in clients.values():
            for engine, nanos in engines.items():
                totals[engine] = totals.get(engine, 0) + nanos
        previous = self._previous_totals
        self._previous_totals = totals
        if previous is None:
            return None
        best = 0
        for engine, nanos in totals.items():
            delta = nanos - previous.get(engine, 0)
            if delta > best:
                best = delta
        if best <= 0:
            return 0.0
        return max(0.0, min(100.0, best / (max(elapsed, 1e-3) * 1e9) * 100.0))

    def _drm_clients(self) -> dict:
        now = time.monotonic()
        if self._clients_at and now - self._clients_at < self.scan_interval:
            return self._clients
        self._clients = scan_drm_clients(self.sys)
        self._clients_at = now
        return self._clients

    def per_process(self) -> dict:
        """``{pid: ns}`` from the cached DRM client scan (busiest engine)."""
        clients = self._drm_clients()
        return {
            pid: max(engines.values())
            for pid, engines in clients.items()
            if engines
        }

    def _sample_nvidia(self) -> GpuSample:
        rows = _nvidia_rows(self.sys)
        if not rows:
            return GpuSample(
                available=True, name=self.name or "NVIDIA GPU", driver="nvidia"
            )
        row = None
        for candidate in rows:
            if candidate["index"] == self.gpu_index:
                row = candidate
                break
        if row is None:
            position = self.gpu_index if 0 <= self.gpu_index < len(rows) else 0
            row = rows[position]
        return GpuSample(
            available=True,
            name=row["name"] or self.name or "NVIDIA GPU",
            driver="nvidia",
            busy_pct=row["busy"],
            freq_mhz=row["freq"],
            power_w=row["power"],
            mem_used_mb=row["mem_used"],
            mem_total_mb=row["mem_total"],
            temp_c=row["temp"],
        )


def _display_name(sys: SysRoot, card: dict) -> str:
    driver = card["driver"] or "gpu"
    product = (sys.read(card["device"] + "/product_name") or "").strip()
    if product:
        return product
    label = {
        "amdgpu": "AMD GPU",
        "i915": "Intel GPU",
        "xe": "Intel GPU",
        "radeon": "AMD GPU",
        "nouveau": "NVIDIA GPU",
    }.get(driver, driver)
    if driver in ("amdgpu", "i915", "xe", "radeon", "nouveau"):
        return f"{label} ({driver})"
    return f"{label}"


def nvidia_smi_path() -> str:
    """Locate ``nvidia-smi``: env override, PATH, then the WSL mount."""
    configured = os.environ.get("MACPM_NVIDIA_SMI", "")
    if configured:
        return configured
    found = shutil.which("nvidia-smi")
    if found:
        return found
    wsl = "/usr/lib/wsl/lib/nvidia-smi"
    return wsl if os.path.exists(wsl) else ""


def _run_smi(*args: str) -> str:
    tool = nvidia_smi_path()
    if not tool:
        return ""
    try:
        result = subprocess.run(
            [tool, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace")


_NVIDIA_QUERY = (
    "index,name,utilization.gpu,clocks.current.graphics,power.draw,"
    "memory.used,memory.total,temperature.gpu"
)
_nvidia_cache = {"at": -1e9, "rows": [], "info": {}, "info_at": -1e9,
                 "processes": [], "processes_at": -1e9}


def reset_nvidia_cache() -> None:
    """Forget the cached ``nvidia-smi`` rows (tests, hot-plug)."""
    _nvidia_cache.update(
        {"at": -1e9, "rows": [], "info": {}, "info_at": -1e9,
         "processes": [], "processes_at": -1e9}
    )


_NVIDIA_INFO_FIELDS = (
    "name,driver_version,cuda_version,count,vbios_version,"
    "pcie.link.gen.current,pcie.link.width.current,"
    "pcie.link.gen.max,pcie.link.width.max,serial"
)
_NVIDIA_INFO_FALLBACKS = (
    _NVIDIA_INFO_FIELDS,
    _NVIDIA_INFO_FIELDS.replace("cuda_version,", ""),
    _NVIDIA_INFO_FIELDS.replace("cuda_version,", "").replace(
        "pcie.link.gen.max,pcie.link.width.max,", ""
    ),
    "name,driver_version,count",
)


def _nvidia_info() -> dict:
    """Card identity from nvidia-smi; dies per card derived from serials."""
    if not nvidia_smi_path():
        return {}
    now = time.monotonic()
    if _nvidia_cache["info"] and now - _nvidia_cache["info_at"] < 30.0:
        return _nvidia_cache["info"]

    info = {}
    for fields in _NVIDIA_INFO_FALLBACKS:
        text = _run_smi(f"--query-gpu={fields}", "--format=csv,noheader")
        rows = [
            [part.strip() for part in line.split(",")]
            for line in text.splitlines()
            if line.strip()
        ]
        if not rows:
            continue
        keys = fields.split(",")
        first = dict(zip(keys, rows[0]))
        serials = {
            row[keys.index("serial")]
            for row in rows
            if "serial" in keys
            and len(row) > keys.index("serial")
            and row[keys.index("serial")] not in ("", "N/A", "[N/A]", "None")
        }
        count = _number(first.get("count", ""))
        cards = len(serials) or None
        dies = int(count // cards) if count and cards and count % cards == 0 else None
        gen = _number(first.get("pcie.link.gen.max", "")) or _number(
            first.get("pcie.link.gen.current", "")
        )
        width = _number(first.get("pcie.link.width.max", "")) or _number(
            first.get("pcie.link.width.current", "")
        )
        info = {
            "vendor": "nvidia",
            "name": first.get("name", ""),
            "driver": first.get("driver_version", ""),
            "cuda": first.get("cuda_version", ""),
            "count": count,
            "cards": cards,
            "dies": dies,
            "vbios": first.get("vbios_version", ""),
            "pcie_gen": gen,
            "pcie_width": width,
        }
        break
    _nvidia_cache["info"] = info
    _nvidia_cache["info_at"] = now
    return info


def _nvidia_processes() -> list:
    """Compute processes per card from ``nvidia-smi`` (cached for two seconds)."""
    if not nvidia_smi_path():
        return []
    now = time.monotonic()
    if now - _nvidia_cache["processes_at"] < 2.0:
        return _nvidia_cache["processes"]

    index_of_uuid = {}
    for line in _run_smi("--query-gpu=index,uuid", "--format=csv,noheader").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 2:
            try:
                index_of_uuid[parts[1]] = int(parts[0])
            except ValueError:
                pass

    sm_by_pid = {}
    pmon = _run_smi("pmon", "-c", "1")
    for line in pmon.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[1])
        except ValueError:
            continue
        percent = _number(fields[3]) if len(fields) > 3 else None
        if percent is not None:
            sm_by_pid[pid] = percent

    records = []
    apps = _run_smi(
        "--query-compute-apps=pid,process_name,used_memory,gpu_uuid",
        "--format=csv,noheader",
    )
    for line in apps.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        gpu = index_of_uuid.get(parts[3])
        records.append(
            {
                "tool": "nvidia",
                "gpu": gpu,
                "pid": pid,
                "name": parts[1] or str(pid),
                "util_pct": sm_by_pid.get(pid),
                "mem_mb": _number(parts[2]),
                "mem_pct": None,
            }
        )
    _nvidia_cache["processes"] = records
    _nvidia_cache["processes_at"] = now
    return records


def _nvidia_rows(sys: SysRoot) -> list:
    """One ``nvidia-smi`` call for every card, cached for two seconds."""
    now = time.monotonic()
    if _nvidia_cache["rows"] and now - _nvidia_cache["at"] < 2.0:
        return _nvidia_cache["rows"]
    text = _run_smi(f"--query-gpu={_NVIDIA_QUERY}", "--format=csv,noheader,nounits")
    rows = []
    for line in text.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 8:
            continue

        def number(index: int):
            try:
                return float(parts[index])
            except (TypeError, ValueError):
                return None

        try:
            index = int(parts[0])
        except ValueError:
            index = len(rows)
        rows.append(
            {
                "index": index,
                "name": parts[1],
                "busy": number(2),
                "freq": number(3),
                "power": number(4),
                "mem_used": number(5),
                "mem_total": number(6),
                "temp": number(7),
            }
        )
    _nvidia_cache["at"] = now
    _nvidia_cache["rows"] = rows
    return rows


def _nvidia_backends(sys: SysRoot) -> list:
    if not sys.glob(_NVIDIA_INFO_GLOB) and not nvidia_smi_path():
        return []
    rows = _nvidia_rows(sys)
    if rows:
        return [
            GpuBackend(sys, "nvidia", "", row["name"], gpu_index=row["index"])
            for row in rows
        ]
    backends = []
    for index, path in enumerate(sys.glob(_NVIDIA_INFO_GLOB)):
        name = "NVIDIA GPU"
        for line in sys.lines(path):
            if line.lower().startswith("model:"):
                name = line.split(":", 1)[1].strip()
                break
        backends.append(GpuBackend(sys, "nvidia", "", name, gpu_index=index))
    return backends


def _smi_processes(tool_name: str) -> list:
    """Compute processes as ``{gpu, pid, name, util_pct, mem_mb}`` records."""
    config = _SMI_TOOLS.get(tool_name)
    tool = smi_tool(tool_name)
    if not config or not tool:
        return []

    records = []
    if tool_name == "iluvatar":
        memory = {}
        text = _run_tool(
            tool,
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader",
        )
        for line in text.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            # One process can own several contexts (one per card); ixsmi
            # reports them as separate rows, so sum them per PID.
            memory[pid] = (memory.get(pid) or 0.0) + (_number(parts[2]) or 0.0)
        text = _run_tool(tool, "pmon", "-c", "1")
        assigned = set()
        for line in text.splitlines():
            if line.lstrip().startswith("#") or not line.strip():
                continue
            fields = line.split()
            if len(fields) < 3:
                continue
            try:
                gpu = int(fields[0])
                pid = int(fields[1])
            except ValueError:
                continue
            util = _number(fields[3]) if len(fields) > 3 else None
            # Attach the summed memory once, to the first card seen, so the
            # grouping step does not count it twice.
            mem_mb = None
            if pid in memory and pid not in assigned:
                mem_mb = memory[pid]
                assigned.add(pid)
            records.append(
                {
                    "tool": tool_name,
                    "gpu": gpu,
                    "pid": pid,
                    "name": " ".join(fields[8:]) if len(fields) > 8 else str(pid),
                    "util_pct": util,
                    "mem_mb": mem_mb,
                    "mem_pct": None,
                }
            )
        return records

    if tool_name in ("denglin", "teco"):
        if tool_name == "denglin":
            text = _run_tool(
                tool,
                "--query-compute-apps=pid,process_name,used_memory,gpu_index",
                "--format=csv,noheader",
            )
        else:
            text = _run_tool(tool, "pmon", "-C", "1")
        for line in text.splitlines():
            if line.lstrip().startswith("#") or not line.strip():
                continue
            if tool_name == "denglin":
                parts = [part.strip() for part in line.split(",")]
                if len(parts) < 4:
                    continue
                try:
                    pid = int(parts[0])
                    gpu = int(parts[3])
                except ValueError:
                    continue
                records.append(
                    {
                        "tool": tool_name,
                        "gpu": gpu,
                        "pid": pid,
                        "name": parts[1] or str(pid),
                        "util_pct": None,
                        "mem_mb": _number(parts[2]),
                        "mem_pct": None,
                    }
                )
            else:
                fields = line.split()
                if len(fields) < 4:
                    continue
                try:
                    gpu = int(fields[0])
                    pid = int(fields[1])
                except ValueError:
                    continue
                records.append(
                    {
                        "tool": tool_name,
                        "gpu": gpu,
                        "pid": pid,
                        "name": " ".join(fields[4:]) if len(fields) > 4 else str(pid),
                        "util_pct": _number(fields[2]),
                        "mem_mb": None,
                        "mem_pct": _number(fields[3]) if len(fields) > 3 else None,
                    }
                )
    return records


def _format_cards(cards: list) -> str:
    """``[0,1,2,3]`` -> ``0-3``; long sparse lists are clipped."""
    cards = sorted(set(cards))
    if not cards:
        return "-"
    if len(cards) > 1 and cards == list(range(cards[0], cards[-1] + 1)):
        return f"{cards[0]}-{cards[-1]}"
    text = ",".join(str(card) for card in cards)
    return text if len(text) <= 9 else text[:8] + "…"


def _smi_process_name(pid: int, fallback: str) -> str:
    """Full command name from /proc when the vendor truncated it."""
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as handle:
            comm = handle.read().strip()
    except OSError:
        return fallback
    return comm or fallback


def _drm_backends(sys: SysRoot) -> list:
    # Recent NVIDIA drivers also expose DRM nodes; the nvidia-smi backend is
    # the authoritative one, so skip those cards to avoid listing each GPU
    # twice.
    skip_nvidia = bool(nvidia_smi_path()) or bool(sys.glob(_NVIDIA_INFO_GLOB))
    backends = []
    for card in _cards(sys):
        driver = card["driver"] or "drm"
        if driver in _IGNORED_DRIVERS:
            continue
        if driver == "nvidia" and skip_nvidia:
            continue
        backends.append(
            GpuBackend(sys, driver, card["path"], _display_name(sys, card))
        )
    return backends


class GpuMonitor:
    """Every GPU on the machine, each one its own :class:`GpuBackend`.

    ``sample()`` keeps the single-GPU contract used by the main sampler and the
    process view (the busiest card); ``samples()`` returns one entry per card
    for the GPU view.  Intel cards share one ``fdinfo`` counter, so with more
    than one Intel GPU the per-card utilisation is reported as ``n/a`` instead
    of duplicating the aggregate on every row.
    """

    def __init__(self, sysroot: Optional[SysRoot], backends: list):
        self.sys = sysroot or SysRoot()
        self.backends = list(backends)

    @classmethod
    def detect(cls, sysroot: Optional[SysRoot] = None) -> "GpuMonitor":
        sys = sysroot or SysRoot()
        return cls(
            sys,
            _drm_backends(sys) + _nvidia_backends(sys) + _smi_backends(sys),
        )

    @property
    def available(self) -> bool:
        return bool(self.backends)

    @property
    def count(self) -> int:
        return len(self.backends)

    @property
    def driver(self) -> str:
        return self.backends[0].driver if self.backends else ""

    @property
    def name(self) -> str:
        return self.backends[0].name if self.backends else ""

    @property
    def per_process_supported(self) -> bool:
        return any(backend.per_process_supported for backend in self.backends)

    @property
    def has_vendor_processes(self) -> bool:
        """Whether any card is driven by a CLI that lists processes."""
        return any(
            isinstance(backend, VendorSmiBackend) for backend in self.backends
        ) or any(backend.driver == "nvidia" for backend in self.backends)

    def info(self) -> dict:
        """Identity of the vendor cards: name, driver, CUDA, card count."""
        for tool_name in _SMI_TOOLS:
            if not smi_tool(tool_name):
                continue
            info = smi_info(tool_name)
            if info:
                return info
        if any(backend.driver == "nvidia" for backend in self.backends):
            return _nvidia_info()
        return {}

    def processes(self) -> list:
        """Compute processes reported by every vendor tool present.

        One row per PID, with the set of cards it holds and the memory summed
        across them — a tensor-parallel worker pair and an EngineCore that
        spans eight cards should not show up as twenty table rows.
        """
        records = []
        seen = set()
        if any(backend.driver == "nvidia" for backend in self.backends):
            for record in _nvidia_processes():
                key = (record["gpu"], record["pid"])
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
        for tool_name in _SMI_TOOLS:
            if not smi_tool(tool_name):
                continue
            for record in _smi_processes(tool_name):
                key = (record["gpu"], record["pid"])
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)

        totals = {}
        for tool_name in _SMI_TOOLS:
            if not smi_tool(tool_name):
                continue
            for row in smi_rows(tool_name):
                totals[(tool_name, row["index"])] = row["mem_total"]

        grouped = {}
        for record in records:
            entry = grouped.setdefault(
                record["pid"],
                {
                    "pid": record["pid"],
                    "name": record["name"],
                    "gpus": set(),
                    "mem_mb": None,
                    "util_pct": None,
                },
            )
            if record["gpu"] is not None:
                entry["gpus"].add(record["gpu"])
            mem_mb = record["mem_mb"]
            if mem_mb is None and record.get("mem_pct") is not None:
                total = totals.get((record.get("tool"), record["gpu"]))
                if total:
                    mem_mb = record["mem_pct"] / 100.0 * total
            if mem_mb:
                config = _SMI_TOOLS.get(record.get("tool") or "", {})
                if config.get("sum_memory", True) or entry["mem_mb"] is None:
                    entry["mem_mb"] = (entry["mem_mb"] or 0) + mem_mb
                else:
                    entry["mem_mb"] = max(entry["mem_mb"], mem_mb)
            if record["util_pct"] is not None:
                entry["util_pct"] = max(entry["util_pct"] or 0.0, record["util_pct"])
            if len(record["name"] or "") > len(entry["name"] or ""):
                entry["name"] = record["name"]

        result = []
        for entry in grouped.values():
            cards = sorted(entry["gpus"])
            result.append(
                {
                    "pid": entry["pid"],
                    "name": entry["name"],
                    "gpus": cards,
                    "card": _format_cards(cards),
                    "mem_mb": entry["mem_mb"],
                    "util_pct": entry["util_pct"],
                    "model": "",
                }
            )
        result.sort(key=lambda item: (item["gpus"][0] if item["gpus"] else 99, item["pid"]))
        if result and os.path.isdir("/proc"):
            try:
                from .modelname import resolve as resolve_model

                for item in result:
                    item["model"] = resolve_model(item["pid"])
            except Exception:
                pass
        return result

    def capabilities(self) -> dict:
        aggregate = {"util": False, "freq": False, "power": False, "proc": False}
        for backend in self.backends:
            caps = backend.capabilities()
            for key in aggregate:
                aggregate[key] = aggregate[key] or caps[key]
        return aggregate

    def samples(self, elapsed: float) -> list:
        intel_count = sum(
            1 for backend in self.backends if backend.driver in ("i915", "xe")
        )
        samples = []
        for position, backend in enumerate(self.backends):
            sample = backend.sample(elapsed)
            sample.index = position
            if backend.driver in ("i915", "xe") and intel_count > 1:
                sample.busy_pct = None
            samples.append(sample)
        return samples

    @staticmethod
    def primary(samples: list) -> GpuSample:
        best = None
        best_score = -2.0
        for sample in samples:
            score = -2.0 if not sample.available else (
                -1.0 if sample.busy_pct is None else sample.busy_pct
            )
            if best is None or score > best_score:
                best, best_score = sample, score
        return best or GpuSample(available=False)

    def sample(self, elapsed: float) -> GpuSample:
        return self.primary(self.samples(elapsed))

    def per_process(self) -> dict:
        totals = {}
        if any(
            backend.driver in ("amdgpu", "i915", "xe") for backend in self.backends
        ):
            clients = scan_drm_clients(self.sys)
            for pid, engines in clients.items():
                if engines:
                    totals[pid] = totals.get(pid, 0) + max(engines.values())
        for backend in self.backends:
            if isinstance(backend, VendorSmiBackend):
                for pid, nanos in backend.per_process().items():
                    totals[pid] = totals.get(pid, 0) + nanos
        return totals


_shared: Optional[GpuMonitor] = None


def reset_shared() -> None:
    """Drop the process-wide monitor (tests, hot-plug debugging)."""
    global _shared
    _shared = None


def shared_backend(sysroot: Optional[SysRoot] = None) -> GpuMonitor:
    """Process-wide monitor so the sampler and the process view share scans."""
    global _shared
    if _shared is None:
        _shared = GpuMonitor.detect(sysroot)
    return _shared
