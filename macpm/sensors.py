"""CPU/GPU temperatures and fan speeds for the macpm TUI.

macOS reads the AppleSMC through the read-only IOKit user client (no root);
Linux reads hwmon.  The reader is deliberately small and fault tolerant: if
the sensors cannot be opened the TUI keeps running and the panel says so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .backend import is_linux, platform_name


def _format_temperature(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:.1f}\N{DEGREE SIGN}C"


def _format_rpm_value(value) -> str:
    if not isinstance(value, (int, float)):
        return "--"
    return str(int(round(value)))


def _format_rpm(value) -> str:
    return f"{_format_rpm_value(value)} rpm"


def _peak(peak: Optional[float], value: Optional[float]) -> Optional[float]:
    if value is None:
        return peak
    return value if peak is None else max(peak, value)


@dataclass
class SensorReading:
    cpu_c: Optional[float] = None
    gpu_c: Optional[float] = None
    fans: list = field(default_factory=list)
    error: Optional[str] = None


class _SmcSource:
    """macOS: AppleSMC through the IOKit user client."""

    def __init__(self, chip: str):
        self.chip = chip
        self._smc = None

    def read(self) -> SensorReading:
        from .smc import SMC

        if self._smc is None:
            try:
                self._smc = SMC()
            except Exception as error:  # AppleSMC missing or restricted
                return SensorReading(error=str(error))
        summary = self._smc.get_thermal_summary(self.chip)
        fans = [
            {
                "id": fan.get("id", index),
                "rpm": fan.get("rpm"),
                "max_rpm": fan.get("max_rpm"),
            }
            for index, fan in enumerate(self._smc.get_fans())
        ]
        return SensorReading(
            cpu_c=summary.get("cpu_c"),
            gpu_c=summary.get("gpu_c"),
            fans=fans,
        )

    def close(self) -> None:
        if self._smc is not None:
            self._smc.close()
            self._smc = None


class _HwmonSource:
    """Linux: hwmon temperature and fan inputs."""

    def __init__(self):
        self._reader = None

    def read(self) -> SensorReading:
        from .linux_backend.thermal import ThermalReader
        from .linux_backend.sysfs import SysRoot

        if self._reader is None:
            self._reader = ThermalReader(SysRoot())
        sample = self._reader.read()
        fans = [
            {"id": fan.id, "rpm": fan.rpm, "max_rpm": fan.max_rpm}
            for fan in sample.fans
        ]
        return SensorReading(
            cpu_c=sample.cpu_c, gpu_c=sample.gpu_c, fans=fans, error=sample.error
        )

    def close(self) -> None:
        self._reader = None


class SensorReader:
    """Cached sensor readings for the temperature/fan panel.

    ``read()`` is cheap to call every frame: it only talks to the sensors
    every ``refresh_interval`` seconds and keeps the previous values in
    between.
    """

    def __init__(self, chip: str, refresh_interval: float = 2.0, source=None):
        self.chip = chip
        self.refresh_interval = max(0.5, float(refresh_interval))
        self.cpu_c: Optional[float] = None
        self.gpu_c: Optional[float] = None
        self.cpu_peak: Optional[float] = None
        self.gpu_peak: Optional[float] = None
        self.fans: list = []
        self.error: Optional[str] = None
        self.ready = False
        self._next_read = 0.0
        if source is None:
            source = _HwmonSource() if is_linux() else _SmcSource(chip)
        self._source = source

    def close(self) -> None:
        if self._source is not None:
            self._source.close()

    def read(self) -> None:
        """Refresh the cached readings when the refresh interval has elapsed."""
        now = time.monotonic()
        if now < self._next_read:
            return
        self._next_read = now + self.refresh_interval

        try:
            reading = self._source.read()
        except Exception as error:  # sensor backend disappeared
            self.error = str(error)
            self.close()
            return

        if (
            reading.cpu_c is None
            and reading.gpu_c is None
            and not reading.fans
        ):
            self.error = reading.error or f"no sensors ({platform_name()})"
            return

        self.error = None
        self.cpu_c = reading.cpu_c
        self.gpu_c = reading.gpu_c
        self.cpu_peak = _peak(self.cpu_peak, self.cpu_c)
        self.gpu_peak = _peak(self.gpu_peak, self.gpu_c)
        self.fans = reading.fans
        self.ready = True

    def reset_peaks(self) -> None:
        self.cpu_peak = self.cpu_c
        self.gpu_peak = self.gpu_c

    def lines(self) -> list:
        """Temperature, peak and fan rows for the panel."""
        if self.error is not None:
            return ["sensors unavailable", self.error]
        if not self.ready:
            return ["reading..."]

        cpu = (
            f"CPU {_format_temperature(self.cpu_c)}   "
            f"peak {_format_temperature(self.cpu_peak)}"
        )
        gpu = (
            f"GPU {_format_temperature(self.gpu_c)}   "
            f"peak {_format_temperature(self.gpu_peak)}"
        )
        if self.fans:
            fan_text = "  ".join(
                f"Fan {fan['id'] + 1} {_format_rpm(fan['rpm'])}" for fan in self.fans
            )
        else:
            fan_text = "Fan: none"
        return [cpu, gpu, fan_text]

    def fan_levels(self) -> list:
        """Fan labels and their current speed as a percentage of maximum."""
        if self.error is not None or not self.ready:
            return []
        levels = []
        for fan in self.fans:
            rpm = fan.get("rpm")
            max_rpm = fan.get("max_rpm")
            level = None
            if (
                isinstance(rpm, (int, float))
                and isinstance(max_rpm, (int, float))
                and max_rpm > 0
            ):
                level = max(0.0, min(100.0, rpm / max_rpm * 100))
            levels.append((f"F{fan['id'] + 1}", level))
        return levels
