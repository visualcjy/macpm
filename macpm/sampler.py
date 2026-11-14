"""Rootless sampler that builds a powermetrics-shaped metrics dictionary.

macpm's display code consumes the plist that powermetrics prints.  This module
collects the same fields from IOReport / IOKit / psutil and returns a dict with
the same shape, so the UI code can stay a copy of the original.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

import psutil

from .metrics import (
    ENERGY_GRACE_SAMPLES,
    ENERGY_TO_J,
    calc_freq,
    energy_available,
    energy_to_joules,
    sane_energy,
)
from .soc import get_freq_tables, get_soc_info

# macOS-only bindings are imported lazily in ``Sampler.__init__``: importing
# this module on Linux must not dlopen CoreFoundation/IOReport, otherwise even
# ``macpm --version`` fails.
Channel = object


def _keep_energy_channel(name: str) -> bool:
    """Only the channels macpm's power panel needs."""
    return (
        name.endswith("CPU Energy")
        or name.endswith("GPU Energy")
        or name.startswith("ANE")
    )


# Backwards compatible aliases for the helpers that used to live here.
_ENERGY_TO_J = ENERGY_TO_J
_ENERGY_GRACE_SAMPLES = ENERGY_GRACE_SAMPLES
_energy_to_joules = energy_to_joules
_energy_available = energy_available
_sane_energy = sane_energy


class Sampler:
    def __init__(self, interval: float):
        import sys

        if sys.platform != "darwin":
            raise RuntimeError(
                "the macOS sampler needs IOReport; use macpm.backend.create_sampler()"
            )
        self.interval = max(0.1, float(interval))
        self.soc = get_soc_info()
        self.e_freqs, self.p_freqs, self.gpu_freqs = get_freq_tables(self.soc["name"])

        # Four independent subscriptions; each keeps its own baseline sample.
        # A missing group only costs that metric, not the whole UI.
        self._cpu_complex = self._subscription(
            "CPU Stats", "CPU Complex Performance States"
        )
        self._cpu_cores = self._subscription("CPU Stats", "CPU Core Performance States")
        self._gpu = self._subscription("GPU Stats", "GPU Performance States")
        self._energy = self._subscription("Energy Model", keep=_keep_energy_channel)
        self._subscriptions = [
            subscription
            for subscription in (
                self._cpu_complex,
                self._cpu_cores,
                self._gpu,
                self._energy,
            )
            if subscription is not None
        ]

        # A component cannot physically exceed these averages; anything above
        # is a coalesced counter update rather than real power.
        self._power_limits = {
            "cpu": max(4.0 * self.soc["cpu_max_power"], 200.0),
            "gpu": max(4.0 * self.soc["gpu_max_power"], 200.0),
            "ane": 100.0,
        }

        # Some macOS builds expose the Energy Model counters but never update
        # them without root.  Track whether a component has ever produced a
        # usable delta so the UI can say n/a instead of a misleading 0 W.
        self._energy_seen = {"cpu": False, "gpu": False}
        self._sample_count = 0

        self.prev_time: Optional[float] = None
        self.prev_disk = None
        self.prev_net: dict[str, tuple[int, int]] = {}
        self.thermal_pressure = "Nominal"
        self.thermal_time = 0.0

    # -- lifecycle ---------------------------------------------------------

    @staticmethod
    def _subscription(group: str, subgroup: Optional[str] = None, keep=None):
        from .ioreport import Subscription

        try:
            return Subscription(group, subgroup, keep=keep)
        except Exception:
            return None

    def prime(self) -> None:
        """Take the baseline samples for all IOReport subscriptions."""
        for subscription in self._subscriptions:
            subscription.delta()
        self.prev_time = time.monotonic()
        self.prev_disk = psutil.disk_io_counters()
        self.prev_net = self._net_counters()
        self._update_thermal(force=True)

    def close(self) -> None:
        for subscription in self._subscriptions:
            subscription.close()

    # -- sampling ----------------------------------------------------------

    def sample(self) -> dict:
        now = time.monotonic()
        elapsed = now - self.prev_time if self.prev_time else self.interval
        if elapsed <= 0:
            elapsed = 1e-3
        self.prev_time = now
        self._update_thermal()

        cpu_complex = self._cpu_complex.delta() if self._cpu_complex else {}
        cpu_cores = self._cpu_cores.delta() if self._cpu_cores else {}
        gpu = self._gpu.delta() if self._gpu else {}
        energy = self._energy.delta() if self._energy else {}
        cpu_complex = cpu_complex or {}
        cpu_cores = cpu_cores or {}
        gpu = gpu or {}
        energy = energy or {}

        clusters = self._clusters(cpu_complex, cpu_cores)

        cpu_joules = self._energy_sum(energy, lambda name: name.endswith("CPU Energy"))
        if cpu_joules == 0.0:
            cpu_joules = self._energy_sum(
                energy,
                lambda name: (
                    (name.startswith("EACC_") or name.startswith("PACC_"))
                    and "_CPU" in name
                    and not name.endswith("_SRAM")
                ),
            )
        gpu_joules = self._energy_sum(energy, lambda name: name.endswith("GPU Energy"))
        ane_joules = self._energy_sum(energy, lambda name: name.startswith("ANE"))

        self._sample_count += 1
        if cpu_joules > 0.0:
            self._energy_seen["cpu"] = True
        if gpu_joules > 0.0:
            self._energy_seen["gpu"] = True

        cpu_joules = _sane_energy(cpu_joules, elapsed, self._power_limits["cpu"])
        gpu_joules = _sane_energy(gpu_joules, elapsed, self._power_limits["gpu"])
        ane_joules = _sane_energy(ane_joules, elapsed, self._power_limits["ane"])

        # The display divides energy by --interval; normalize by the real
        # elapsed time so the watts stay correct if a frame was delayed.
        energy_scale = self.interval / max(elapsed, 1e-3)
        cpu_mj = cpu_joules * energy_scale * 1000.0
        gpu_mj = gpu_joules * energy_scale * 1000.0
        ane_mj = ane_joules * energy_scale * 1000.0
        total_mj = (cpu_joules + gpu_joules + ane_joules) * energy_scale * 1000.0

        gpu_freq, gpu_active = self._gpu_usage(gpu)

        return {
            "timestamp": time.time(),
            "thermal_pressure": self.thermal_pressure,
            "processor": {
                "clusters": clusters,
                "ane_energy": ane_mj,
                "cpu_energy": cpu_mj,
                "gpu_energy": gpu_mj,
                "combined_power": total_mj,
                "cpu_energy_available": _energy_available(
                    self._energy_seen["cpu"], self._sample_count
                ),
                "gpu_energy_available": _energy_available(
                    self._energy_seen["gpu"], self._sample_count
                ),
            },
            "gpu": {
                "freq_hz": int(gpu_freq),
                "idle_ratio": max(0.0, min(1.0, 1.0 - gpu_active)),
            },
            "disk": self._disk_rates(elapsed),
            "network": self._network_rates(elapsed),
        }

    # -- CPU / GPU ---------------------------------------------------------

    def _clusters(self, complex_raw: dict[str, Channel], cores_raw: dict[str, Channel]) -> list[dict]:
        e_cores = sorted(
            ((name, data) for name, data in cores_raw.items() if name.startswith("ECPU")),
            key=lambda item: item[0],
        )
        p_cores = sorted(
            ((name, data) for name, data in cores_raw.items() if name.startswith("PCPU")),
            key=lambda item: item[0],
        )

        specs = [
            ("E-Cluster", "ECPU", self.e_freqs, e_cores, self.soc["e_core_count"]),
            ("P-Cluster", "PCPU", self.p_freqs, p_cores, self.soc["p_core_count"]),
        ]

        clusters: list[dict] = []
        for label, channel_name, freqs, cores, expected in specs:
            complex_data = complex_raw.get(channel_name)
            states = complex_data.states if complex_data else []
            average, active = calc_freq(states, freqs)
            if average <= 0 and cores:
                core_metrics = [calc_freq(data.states, freqs) for _, data in cores]
                active = sum(value[1] for value in core_metrics) / len(core_metrics)
                average = sum(value[0] for value in core_metrics) / len(core_metrics)
            fallback_freq = freqs[0] if freqs else 0
            if average <= 0:
                average = float(fallback_freq)

            cpus = []
            for index, (_, data) in enumerate(cores):
                core_average, core_active = calc_freq(data.states, freqs)
                if core_average <= 0:
                    core_average = float(fallback_freq)
                cpus.append(
                    {
                        "cpu": index,
                        "freq_hz": int(core_average * 1_000_000),
                        "idle_ratio": max(0.0, min(1.0, 1.0 - core_active)),
                    }
                )

            # Some macOS builds expose the complex channel but no per-core
            # channels.  Keep the cluster gauge working with synthetic cores.
            if not cpus and expected:
                cpus = [
                    {
                        "cpu": index,
                        "freq_hz": int(fallback_freq * 1_000_000),
                        "idle_ratio": max(0.0, min(1.0, 1.0 - active)),
                    }
                    for index in range(int(expected))
                ]

            clusters.append(
                {
                    "name": label,
                    "freq_hz": int(average * 1_000_000),
                    "idle_ratio": max(0.0, min(1.0, 1.0 - active)),
                    "cpus": cpus,
                }
            )
        return clusters

    def _gpu_usage(self, raw: dict[str, Channel]) -> tuple[float, float]:
        data = raw.get("GPUPH")
        if not data:
            return 0.0, 0.0
        average, active = calc_freq(data.states, self.gpu_freqs)
        if average <= 0 and self.gpu_freqs:
            average = float(self.gpu_freqs[0])
        return average, active

    # -- energy ------------------------------------------------------------

    @staticmethod
    def _energy_sum(raw: dict[str, Channel], predicate) -> float:
        total = 0.0
        for name, channel in raw.items():
            if predicate(name):
                total += _energy_to_joules(channel.value, channel.unit)
        return total

    # -- disk / network ----------------------------------------------------

    def _disk_rates(self, elapsed: float) -> dict:
        current = psutil.disk_io_counters()
        previous = self.prev_disk
        self.prev_disk = current
        if current is None or previous is None:
            return {
                "read_iops": 0,
                "write_iops": 0,
                "read_Bps": 0,
                "write_Bps": 0,
                "rops_per_s": 0,
                "wops_per_s": 0,
                "rbytes_per_s": 0,
                "wbytes_per_s": 0,
            }
        return {
            "rops_per_s": int((current.read_count - previous.read_count) / elapsed),
            "wops_per_s": int((current.write_count - previous.write_count) / elapsed),
            "rbytes_per_s": int((current.read_bytes - previous.read_bytes) / elapsed),
            "wbytes_per_s": int((current.write_bytes - previous.write_bytes) / elapsed),
        }

    def _net_counters(self) -> dict[str, tuple[int, int]]:
        try:
            per_nic = psutil.net_io_counters(pernic=True)
        except Exception:
            per_nic = {}
        counters = {}
        for name, value in per_nic.items():
            if name.startswith("lo"):
                continue
            counters[name] = (value.bytes_sent, value.bytes_recv)
        return counters

    def _network_rates(self, elapsed: float) -> dict:
        current = self._net_counters()
        previous = self.prev_net
        self.prev_net = current
        sent = 0
        received = 0
        for name, (bytes_sent, bytes_recv) in current.items():
            old = previous.get(name)
            if old is None:
                continue
            sent += max(0, bytes_sent - old[0])
            received += max(0, bytes_recv - old[1])
        return {
            "obyte_rate": int(sent / elapsed),
            "ibyte_rate": int(received / elapsed),
        }

    # -- thermal -----------------------------------------------------------

    def _update_thermal(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.thermal_time < 5.0:
            return
        self.thermal_time = now
        try:
            output = subprocess.run(
                ["/usr/bin/pmset", "-g", "therm"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
            ).stdout.decode("utf-8", "ignore")
        except (OSError, subprocess.TimeoutExpired):
            self.thermal_pressure = "Nominal"
            return

        limits = [
            int(value)
            for value in re.findall(r"CPU_(?:Speed|Scheduler)_Limit\s*=\s*(\d+)", output)
        ]
        if limits and min(limits) < 100:
            self.thermal_pressure = "Moderate"
        elif "No thermal warning level" in output:
            self.thermal_pressure = "Nominal"
        elif "thermal" in output.lower():
            self.thermal_pressure = "Moderate"
        else:
            self.thermal_pressure = "Nominal"
