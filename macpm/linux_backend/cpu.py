"""CPU topology, per-core utilisation and frequency for Linux.

Linux has no IOReport: utilisation comes from ``/proc/stat`` (through
``psutil``) and frequency from ``cpufreq``.  When ``cpufreq/stats/time_in_state``
exists (CONFIG_CPU_FREQ_STAT, most x86 laptops) the cluster frequency is the
residency-weighted average of the active P-states, which is the closest
analogue of what macpm reads on Apple Silicon.  Otherwise it falls back to the
mean of the instantaneous ``scaling_cur_freq`` values.

The E/P split is detected from the kernel's own topology description:
``topology/core_type`` (Intel hybrid), then ``cpu_capacity`` (arm64
big.LITTLE), then ``cpuinfo_max_freq``.  A uniform machine gets one cluster.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

import psutil

from .sysfs import SysRoot

CPU_SYS = "/sys/devices/system/cpu"

E_CLUSTER = "E-Cluster"
P_CLUSTER = "P-Cluster"

_MHZ_RE = re.compile(r"^cpu MHz\s*:\s*([0-9.]+)")


@dataclass(frozen=True)
class Core:
    """One logical CPU and the cluster it belongs to."""

    index: int
    cluster: str  # "E" or "P"


def _cpu_numbers(sys: SysRoot) -> list:
    numbers = []
    for path in sys.glob(CPU_SYS + "/cpu[0-9]*"):
        name = path.rsplit("/", 1)[-1]
        if name[3:].isdigit():
            numbers.append(int(name[3:]))
    if numbers:
        return sorted(numbers)
    return list(range(psutil.cpu_count(logical=True) or 1))


def _read_map(sys: SysRoot, numbers: list, pattern: str) -> dict:
    values = {}
    for number in numbers:
        value = sys.read_int(pattern.format(cpu=number))
        if value is not None:
            values[number] = value
    return values


def _split_by_gap(values: dict, minimum_ratio: float = 1.25) -> Optional[dict]:
    """Return the low-performance group when the values have a clear gap.

    ``None`` means the distribution looks uniform (or continuous), so the
    machine is not carved into fake clusters.  This keeps a laptop whose E
    cores differ by a few MHz from getting an arbitrary split.
    """
    unique = sorted(set(values.values()))
    if len(unique) < 2:
        return None
    largest_gap = 0.0
    split_at = None
    for lower, upper in zip(unique, unique[1:]):
        gap = (upper - lower) / max(float(upper), 1.0)
        if gap > largest_gap:
            largest_gap = gap
            split_at = lower
    if split_at is None or largest_gap < 0.10:
        return None
    if unique[-1] / max(float(unique[0]), 1.0) < minimum_ratio:
        return None
    return {cpu: value for cpu, value in values.items() if value <= split_at}


def classify_cores(sys: SysRoot) -> tuple:
    """Return ``(cores, method)`` describing the E/P layout of this machine.

    ``method`` is one of ``core_type``, ``capacity``, ``max_freq`` or
    ``uniform`` and is only used for the capability report.
    """
    numbers = _cpu_numbers(sys)

    core_types = _read_map(sys, numbers, CPU_SYS + "/cpu{cpu}/topology/core_type")
    if len(core_types) == len(numbers) and len(set(core_types.values())) == 2:
        # The kernel reports 0 for Atom/E-core and 1 for Core/P-core.
        return [
            Core(number, "E" if core_types[number] == 0 else "P")
            for number in numbers
        ], "core_type"

    capacities = _read_map(sys, numbers, CPU_SYS + "/cpu{cpu}/cpu_capacity")
    if len(capacities) == len(numbers):
        low = _split_by_gap(capacities)
        if low is not None:
            return [
                Core(number, "E" if number in low else "P") for number in numbers
            ], "capacity"

    max_freqs = _read_map(
        sys, numbers, CPU_SYS + "/cpu{cpu}/cpufreq/cpuinfo_max_freq"
    )
    if len(max_freqs) == len(numbers):
        low = _split_by_gap(max_freqs)
        if low is not None:
            return [
                Core(number, "E" if number in low else "P") for number in numbers
            ], "max_freq"

    return [Core(number, "P") for number in numbers], "uniform"


def _cpuinfo_mhz(sys: SysRoot) -> dict:
    """Fallback frequency table parsed from ``/proc/cpuinfo``."""
    values = {}
    current = None
    for line in (sys.read("/proc/cpuinfo") or "").splitlines():
        if line.startswith("processor"):
            try:
                current = int(line.split(":", 1)[1].strip())
            except (IndexError, ValueError):
                current = None
        elif current is not None:
            match = _MHZ_RE.match(line.strip())
            if match:
                try:
                    values[current] = float(match.group(1))
                except ValueError:
                    pass
    return values


def _idle(usage_percent: float) -> float:
    return max(0.0, min(1.0, 1.0 - float(usage_percent) / 100.0))


class CpuSampler:
    """Per-sample CPU clusters in the shape ``macpm.macpm.parse_cpu_metrics``
    expects (``name``/``freq_hz``/``idle_ratio``/``cpus``)."""

    def __init__(self, sysroot: Optional[SysRoot] = None, clock=time.monotonic):
        self.sys = sysroot or SysRoot()
        self.clock = clock
        self.cores, self.method = classify_cores(self.sys)
        self.cpu_count = len(self.cores)
        self.max_freqs = _read_map(
            self.sys,
            [core.index for core in self.cores],
            CPU_SYS + "/cpu{cpu}/cpufreq/cpuinfo_max_freq",
        )
        self._prev_states: dict = {}
        self._states: dict = {}

    # -- lifecycle ---------------------------------------------------------

    def prime(self) -> None:
        """Take the /proc/stat baseline and the first residency snapshot."""
        try:
            psutil.cpu_percent(percpu=True, interval=None)
        except Exception:
            pass
        self._prev_states = self._read_all_states()

    def close(self) -> None:
        self._prev_states = {}
        self._states = {}

    # -- frequencies -------------------------------------------------------

    def _read_all_states(self) -> dict:
        return {
            core.index: self._time_in_state(core.index) for core in self.cores
        }

    def _time_in_state(self, cpu: int) -> dict:
        states = {}
        for line in self.sys.lines(
            f"{CPU_SYS}/cpu{cpu}/cpufreq/stats/time_in_state"
        ):
            fields = line.split()
            if len(fields) < 2:
                continue
            try:
                states[int(fields[0])] = int(fields[1])
            except ValueError:
                continue
        return states

    def _current_freqs(self) -> list:
        """Current frequency per core in MHz, in ``self.cores`` order."""
        freqs = []
        cpuinfo = None
        for core in self.cores:
            value = self.sys.read_int(
                f"{CPU_SYS}/cpu{core.index}/cpufreq/scaling_cur_freq"
            )
            if value is None:
                value = self.sys.read_int(
                    f"{CPU_SYS}/cpu{core.index}/cpufreq/cpuinfo_cur_freq"
                )
            if value:
                freqs.append(value / 1000.0)
                continue
            if cpuinfo is None:
                cpuinfo = _cpuinfo_mhz(self.sys)
            if core.index in cpuinfo:
                freqs.append(cpuinfo[core.index])
                continue
            freqs.append(float(self.max_freqs.get(core.index, 0)) / 1000.0)
        return freqs

    def _residency_freq(self, members: list) -> Optional[float]:
        """Residency-weighted average frequency in MHz, or None."""
        weighted = 0.0
        total = 0
        for core in members:
            previous = self._prev_states.get(core.index) or {}
            for freq, ticks in (self._states.get(core.index) or {}).items():
                delta = ticks - previous.get(freq, 0)
                if delta > 0:
                    weighted += (freq / 1000.0) * delta
                    total += delta
        if total <= 0:
            return None
        return weighted / total

    # -- sampling ----------------------------------------------------------

    def sample(self) -> list:
        try:
            usage = psutil.cpu_percent(percpu=True, interval=None)
        except Exception:
            usage = []
        if not usage:
            usage = [0.0] * self.cpu_count

        current = self._current_freqs()
        self._states = self._read_all_states()

        clusters = []
        for letter, name in (("E", E_CLUSTER), ("P", P_CLUSTER)):
            members = [core for core in self.cores if core.cluster == letter]
            if not members:
                continue
            positions = [
                position
                for position, core in enumerate(self.cores)
                if core.cluster == letter
            ]
            per_core_usage = [
                usage[core.index] if core.index < len(usage) else 0.0
                for core in members
            ]
            average_freq = self._residency_freq(members)
            if average_freq is None:
                readings = [
                    current[position]
                    for position in positions
                    if position < len(current) and current[position] > 0
                ]
                average_freq = sum(readings) / len(readings) if readings else 0.0

            cpus = []
            for position, core in enumerate(members):
                core_freq = (
                    current[positions[position]]
                    if positions[position] < len(current)
                    else 0.0
                )
                if core_freq <= 0:
                    core_freq = float(self.max_freqs.get(core.index, 0)) / 1000.0
                cpus.append(
                    {
                        "cpu": position,
                        "freq_hz": int(core_freq * 1_000_000),
                        "idle_ratio": _idle(per_core_usage[position]),
                    }
                )
            clusters.append(
                {
                    "name": name,
                    "freq_hz": int(average_freq * 1_000_000),
                    "idle_ratio": _idle(
                        sum(per_core_usage) / len(per_core_usage)
                    ),
                    "cpus": cpus,
                }
            )

        self._prev_states = self._states
        return clusters

    # -- capabilities ------------------------------------------------------

    def has_frequency(self) -> bool:
        return any(
            self.sys.exists(f"{CPU_SYS}/cpu{core.index}/cpufreq/scaling_cur_freq")
            for core in self.cores
        )

    def cluster_max_freq_mhz(self, letter: str) -> int:
        values = [
            self.max_freqs[core.index]
            for core in self.cores
            if core.cluster == letter and core.index in self.max_freqs
        ]
        return int(max(values) / 1000) if values else 0

    def cluster_counts(self) -> tuple:
        e_cores = sum(1 for core in self.cores if core.cluster == "E")
        return e_cores, self.cpu_count - e_cores
