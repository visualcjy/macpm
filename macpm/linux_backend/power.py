"""Power readings for Linux: RAPL, hwmon power sensors and the battery.

RAPL is the only generic CPU power counter on Linux and since kernel 5.10 its
``energy_uj`` file is root-only (CVE-2020-8694).  macpm therefore reads it when
it can and reports ``n/a`` otherwise; running as root is the expected mode on
Linux.  GPU power comes from the vendor hwmon node (amdgpu exposes
``power1_average``).  On laptops and arm64 boards where neither exists the
battery's ``power_now`` gives a whole-system figure.
"""

from __future__ import annotations

from typing import Optional

from .sysfs import SysRoot

_RAPL_GLOB = "/sys/class/powercap/intel-rapl:*"
_GPU_CHIPS = {"amdgpu", "nouveau", "radeon", "i915", "xe", "nvidia", "apple-gpu"}


class RaplReader:
    """Cumulative package energy, with the core sub-zone preferred."""

    def __init__(self, sysroot: SysRoot):
        self.sys = sysroot
        self.energy_path: Optional[str] = None
        self.max_range_path: Optional[str] = None
        self.name = ""
        self._pick_zone()

    def _zones(self) -> list:
        return [
            path
            for path in self.sys.glob(_RAPL_GLOB)
            if self.sys.exists(path + "/energy_uj")
        ]

    def _pick_zone(self) -> None:
        zones = self._zones()
        if not zones:
            return
        # A "core" sub-zone excludes the iGPU and uncore, so it does not
        # double count a GPU power sensor.  Fall back to the package.
        for path in zones:
            if (self.sys.read(path + "/name") or "").strip() in ("core", "cores"):
                self._use(path)
                return
        for path in zones:
            name = (self.sys.read(path + "/name") or "").strip()
            if name.startswith("package"):
                self._use(path)
                return
        self._use(zones[0])

    def _use(self, path: str) -> None:
        self.energy_path = path + "/energy_uj"
        self.max_range_path = path + "/max_energy_range_uj"
        self.name = (self.sys.read(path + "/name") or "").strip()

    @property
    def available(self) -> bool:
        if not self.energy_path:
            return False
        value = self.sys.read_int(self.energy_path)
        return value is not None and value >= 0

    def read_uj(self) -> Optional[int]:
        if not self.energy_path:
            return None
        return self.sys.read_int(self.energy_path)

    def max_range_uj(self) -> Optional[int]:
        if not self.max_range_path:
            return None
        return self.sys.read_int(self.max_range_path)


def rapl_delta(previous: Optional[int], current: Optional[int], maximum: Optional[int]) -> int:
    """Micro-joules consumed between two cumulative RAPL readings."""
    if previous is None or current is None:
        return 0
    if current >= previous:
        return current - previous
    if maximum and maximum > previous:
        return (maximum - previous) + current
    return 0


class HwmonPowerReader:
    """``power1_average``/``power1_input`` values from every hwmon chip."""

    _GLOB = "/sys/class/hwmon/hwmon*"

    def __init__(self, sysroot: SysRoot):
        self.sys = sysroot
        self._chips = None

    def _discover(self) -> list:
        chips = []
        for path in self.sys.glob(self._GLOB):
            name = (self.sys.read(path + "/name") or "").strip()
            if not name:
                continue
            chips.append((path, name))
        return chips

    def read(self) -> dict:
        """Return ``{chip_name: watts}``."""
        if self._chips is None:
            self._chips = self._discover()
        readings = {}
        for path, name in self._chips:
            micro_watts = self.sys.read_int(path + "/power1_average")
            if micro_watts is None:
                micro_watts = self.sys.read_int(path + "/power1_input")
            if micro_watts is None or micro_watts < 0:
                continue
            readings[name] = micro_watts / 1_000_000.0
        return readings

    def gpu_watts(self) -> Optional[float]:
        readings = self.read()
        values = [value for name, value in readings.items() if name in _GPU_CHIPS]
        return max(values) if values else None

    def system_watts(self) -> Optional[float]:
        readings = self.read()
        values = [value for name, value in readings.items() if name not in _GPU_CHIPS]
        return max(values) if values else None


class BatteryReader:
    """Whole-system power from ``/sys/class/power_supply/BAT*``."""

    _GLOB = "/sys/class/power_supply/BAT*"

    def __init__(self, sysroot: SysRoot):
        self.sys = sysroot

    def watts(self) -> Optional[float]:
        total = 0.0
        found = False
        for path in self.sys.glob(self._GLOB):
            micro_watts = self.sys.read_int(path + "/power_now")
            if micro_watts is not None:
                # power_now is reported as positive while discharging, and
                # some drivers expose a signed value.
                total += abs(float(micro_watts)) / 1_000_000.0
                found = True
                continue
            current = self.sys.read_int(path + "/current_now")
            voltage = self.sys.read_int(path + "/voltage_now")
            if current is not None and voltage is not None:
                total += abs(current * voltage) / 1e12
                found = True
        return total if found else None


class EnergySampler:
    """CPU/GPU/system watts per sample, plus an availability report."""

    def __init__(self, sysroot: SysRoot):
        self.rapl = RaplReader(sysroot)
        self.hwmon = HwmonPowerReader(sysroot)
        self.battery = BatteryReader(sysroot)
        self._previous_uj: Optional[int] = None
        self._previous_battery: Optional[float] = None

    def prime(self) -> None:
        self._previous_uj = self.rapl.read_uj()
        self._previous_battery = self.battery.watts()

    def sample(self, elapsed: float) -> dict:
        elapsed = max(elapsed, 1e-3)
        current_uj = self.rapl.read_uj()
        delta = rapl_delta(self._previous_uj, current_uj, self.rapl.max_range_uj())
        self._previous_uj = current_uj
        cpu_watts = delta / 1_000_000.0 / elapsed if delta > 0 else None

        gpu_watts = self.hwmon.gpu_watts()

        battery = self.battery.watts()
        if battery is not None and battery <= 0:
            battery = None
        system_watts = battery or self.hwmon.system_watts()
        self._previous_battery = battery

        return {
            "cpu_w": cpu_watts,
            "gpu_w": gpu_watts,
            "system_w": system_watts,
            "cpu_source": f"rapl:{self.rapl.name}" if cpu_watts is not None else "",
            "gpu_source": "hwmon" if gpu_watts is not None else "",
            "system_source": "battery" if battery is not None else ("hwmon" if system_watts else ""),
        }
