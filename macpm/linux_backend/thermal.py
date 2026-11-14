"""Temperatures, fans and thermal pressure for Linux.

Sensors come from hwmon (the same place ``sensors``/``psutil`` read): the
chip name identifies CPU dies (``coretemp``, ``k10temp``, ``macsmc``) and GPUs
(``amdgpu``, ``nouveau``), labels disambiguate the rest.  Fans are
``fanN_input`` with the optional ``fanN_max`` macpm uses for its load bars.

Thermal state is approximated with the kernel's own signals: PSI
(``/proc/pressure/cpu``) and the x86 throttle counters in
``/sys/devices/system/cpu/cpu*/thermal_throttle``.  There is no equivalent of
``pmset -g therm``'s speed limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .sysfs import SysRoot

_HWMON_GLOB = "/sys/class/hwmon/hwmon*"

_CPU_CHIPS = {
    "coretemp",
    "k10temp",
    "zenpower",
    "k8temp",
    "cpu_thermal",
    "cpu-thermal",
    "soc_thermal",
    "soc-thermal",
    "macsmc",
    "applesmc",
    "acpitz",
    "x86_pkg_temp",
}
_GPU_CHIPS = {"amdgpu", "nouveau", "radeon", "i915", "xe", "nvidia", "apple-gpu"}

_CPU_LABEL_RE = re.compile(r"cpu|core|package|tctl|tdie|soc", re.IGNORECASE)
_GPU_LABEL_RE = re.compile(r"gpu|graphics", re.IGNORECASE)
_TEMP_FILE_RE = re.compile(r"^temp(\d+)_input$")
_FAN_FILE_RE = re.compile(r"^fan(\d+)_input$")


@dataclass
class FanReading:
    id: int
    name: str
    rpm: Optional[float]
    max_rpm: Optional[float]


@dataclass
class ThermalSample:
    cpu_c: Optional[float] = None
    gpu_c: Optional[float] = None
    fans: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    error: Optional[str] = None


def _valid_temperature(value: Optional[float]) -> bool:
    return value is not None and 0.0 < value < 130.0


class ThermalReader:
    def __init__(self, sysroot: SysRoot):
        self.sys = sysroot

    def _chips(self) -> list:
        chips = []
        for path in self.sys.glob(_HWMON_GLOB):
            name = (self.sys.read(path + "/name") or "").strip()
            chips.append((path, name))
        return chips

    def _temperatures(self, path: str) -> list:
        readings = []
        for entry in self.sys.listdir(path):
            match = _TEMP_FILE_RE.match(entry)
            if not match:
                continue
            number = match.group(1)
            raw = self.sys.read_int(path + "/" + entry)
            if raw is None:
                continue
            label = (self.sys.read(f"{path}/temp{number}_label") or "").strip()
            readings.append((label, raw / 1000.0))
        return readings

    def _fans(self, path: str) -> list:
        fans = []
        for entry in self.sys.listdir(path):
            match = _FAN_FILE_RE.match(entry)
            if not match:
                continue
            number = match.group(1)
            rpm = self.sys.read_int(path + "/" + entry)
            if rpm is None:
                continue
            maximum = self.sys.read_int(f"{path}/fan{number}_max")
            label = (self.sys.read(f"{path}/fan{number}_label") or "").strip()
            fans.append(
                FanReading(
                    id=len(fans),
                    name=label or f"Fan {len(fans) + 1}",
                    rpm=float(rpm),
                    max_rpm=float(maximum) if maximum else None,
                )
            )
        return fans

    def read(self) -> ThermalSample:
        sample = ThermalSample()
        cpu_values = []
        gpu_values = []
        fallback_values = []
        for path, chip in self._chips():
            readings = self._temperatures(path)
            if readings:
                sample.sources.append(chip or path)
            fans = self._fans(path)
            if fans:
                for fan in fans:
                    fan.id = len(sample.fans)
                    sample.fans.append(fan)
            for label, value in readings:
                if not _valid_temperature(value):
                    continue
                if chip in _GPU_CHIPS or _GPU_LABEL_RE.search(label):
                    gpu_values.append(value)
                elif chip in _CPU_CHIPS or _CPU_LABEL_RE.search(label) or _CPU_LABEL_RE.search(chip):
                    cpu_values.append(value)
                else:
                    fallback_values.append(value)

        if cpu_values:
            sample.cpu_c = round(max(cpu_values), 1)
        elif fallback_values:
            sample.cpu_c = round(max(fallback_values), 1)
        if gpu_values:
            sample.gpu_c = round(max(gpu_values), 1)

        sample.fans = sample.fans[:10]
        if not sample.sources and not sample.fans:
            sample.error = "no hwmon sensors"
        return sample


class ThermalPressure:
    """Nominal / Moderate / Critical approximation of the thermal state."""

    _THROTTLE_GLOB = (
        "/sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/"
        "core_throttle_count"
    )
    _PACKAGE_GLOB = (
        "/sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/"
        "package_throttle_count"
    )

    def __init__(self, sysroot: SysRoot):
        self.sys = sysroot
        self._previous_counts: Optional[int] = None
        self._previous_psi: Optional[float] = None

    def _throttle_total(self) -> Optional[int]:
        values = [
            value
            for value in (
                self.sys.read_int(path)
                for path in self.sys.glob(self._THROTTLE_GLOB)
                + self.sys.glob(self._PACKAGE_GLOB)
            )
            if value is not None
        ]
        return sum(values) if values else None

    def psi_cpu(self) -> Optional[float]:
        text = self.sys.read("/proc/pressure/cpu") or ""
        match = re.search(r"some\s+avg10=([0-9.]+)", text)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    def prime(self) -> None:
        self._previous_counts = self._throttle_total()
        self._previous_psi = self.psi_cpu()

    def read(self) -> str:
        counts = self._throttle_total()
        throttled = False
        if counts is not None and self._previous_counts is not None:
            throttled = counts > self._previous_counts
        self._previous_counts = counts

        psi = self.psi_cpu()
        self._previous_psi = psi

        if throttled:
            return "Moderate"
        if psi is None:
            return "Nominal"
        if psi >= 50.0:
            return "Critical"
        if psi >= 20.0:
            return "Moderate"
        return "Nominal"
