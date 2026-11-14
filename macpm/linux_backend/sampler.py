"""The Linux sampler: builds the same powermetrics-shaped dict as macOS.

``macpm.macpm`` consumes one dictionary per interval (clusters, GPU, power,
disk and network rates); this module produces it from sysfs/proc/psutil so the
UI does not need a Linux branch of its own.

Unavailable readings are reported as zero plus an availability flag, which the
UI renders as ``n/a`` instead of a misleading ``0``.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from typing import Optional

import psutil

from ..capabilities import Capabilities
from ..metrics import energy_available, sane_energy
from .cpu import CpuSampler
from .gpu import shared_backend
from .power import EnergySampler
from .soc import get_soc_info
from .sysfs import SysRoot, is_virtual_disk, is_virtual_net
from .thermal import ThermalPressure, ThermalReader

_PARTITION_RE = [
    re.compile(pattern)
    for pattern in (
        r"^(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)\d+$",
        r"^nvme\d+n\d+p\d+$",
        r"^mmcblk\d+p\d+$",
        r"^md\d+p\d+$",
    )
]


def _is_partition(name: str) -> bool:
    return any(pattern.match(name) for pattern in _PARTITION_RE)


def disk_counters() -> Optional[tuple]:
    """Aggregate physical disk counters, skipping partitions and loop/dm."""
    try:
        per_disk = psutil.disk_io_counters(perdisk=True)
    except Exception:
        return None
    if not per_disk:
        return None
    read_count = write_count = read_bytes = write_bytes = 0
    used = False
    for name, counters in per_disk.items():
        if is_virtual_disk(name) or _is_partition(name):
            continue
        used = True
        read_count += counters.read_count
        write_count += counters.write_count
        read_bytes += counters.read_bytes
        write_bytes += counters.write_bytes
    if not used:
        return None
    return read_count, write_count, read_bytes, write_bytes


def net_counters() -> dict:
    """Cumulative bytes per physical interface, skipping virtual links."""
    try:
        per_nic = psutil.net_io_counters(pernic=True)
    except Exception:
        return {}
    counters = {}
    for name, value in per_nic.items():
        if is_virtual_net(name):
            continue
        counters[name] = (value.bytes_sent, value.bytes_recv)
    return counters


class LinuxSampler:
    def __init__(self, interval: float):
        self.interval = max(0.1, float(interval))
        self.sys = SysRoot()
        self.cpu = CpuSampler(self.sys)
        self.soc = get_soc_info(self.sys, self.cpu)
        self.gpu = shared_backend(self.sys)
        self.energy = EnergySampler(self.sys)
        self.pressure = ThermalPressure(self.sys)
        self.thermal = ThermalReader(self.sys)

        # A multi-GPU host legitimately draws several hundred watts; scale the
        # plausibility limit with the number of cards.
        gpu_cards = max(1, self.gpu.count)
        self._power_limits = {
            "cpu": max(4.0 * self.soc["cpu_max_power"], 200.0),
            "gpu": max(4.0 * self.soc["gpu_max_power"] * gpu_cards, 200.0),
            "ane": 100.0,
        }
        self._seen = {"cpu": False, "gpu": False}
        self._sample_count = 0
        self.prev_time: Optional[float] = None
        self.prev_disk: Optional[tuple] = None
        self.prev_net: dict = {}
        self.thermal_pressure = "Nominal"
        self.capabilities = self._detect_capabilities()

    # -- capabilities ------------------------------------------------------

    def _detect_capabilities(self) -> Capabilities:
        gpu = self.gpu.capabilities()
        thermal = self.thermal.read()
        try:
            euid = os.geteuid()
        except AttributeError:  # pragma: no cover - Windows
            euid = -1
        e_cores = self.soc["e_core_count"]
        return Capabilities(
            platform="linux",
            root=euid == 0,
            cpu_freq=self.cpu.has_frequency(),
            e_cluster=e_cores > 0,
            gpu_util=gpu["util"],
            gpu_freq=gpu["freq"],
            gpu_power=gpu["power"],
            cpu_power=self.energy.rapl.available,
            ane=False,
            temps=thermal.cpu_c is not None or thermal.gpu_c is not None,
            fans=bool(thermal.fans),
            proc_gpu=gpu["proc"],
            proc_net=bool(shutil.which("ss")),
            thermal_pressure=True,
            gpu_util_self_only=(
                euid != 0 and self.gpu.driver in ("i915", "xe")
            ),
        )

    # -- lifecycle ---------------------------------------------------------

    def prime(self) -> None:
        self.cpu.prime()
        self.energy.prime()
        self.pressure.prime()
        self.prev_time = time.monotonic()
        self.prev_disk = disk_counters()
        self.prev_net = net_counters()
        self.thermal_pressure = self.pressure.read()
        # The first GPU sample establishes the fdinfo baseline.
        self.gpu.sample(self.interval)

    def close(self) -> None:
        self.cpu.close()

    # -- sampling ----------------------------------------------------------

    def sample(self) -> dict:
        now = time.monotonic()
        elapsed = now - self.prev_time if self.prev_time else self.interval
        if elapsed <= 0:
            elapsed = 1e-3
        self.prev_time = now

        clusters = self.cpu.sample()
        gpu_samples = self.gpu.samples(elapsed)
        gpu = self.gpu.primary(gpu_samples)
        energy = self.energy.sample(elapsed)
        self.thermal_pressure = self.pressure.read()

        # Sum per-card sensors; a single hwmon value would only show one card.
        card_watts = sum(
            sample.power_w for sample in gpu_samples if sample.power_w
        )
        gpu_watts = card_watts if card_watts > 0 else energy["gpu_w"]

        cpu_joules = _sane(energy["cpu_w"], elapsed, self._power_limits["cpu"])
        gpu_joules = _sane(gpu_watts, elapsed, self._power_limits["gpu"])
        self._sample_count += 1
        if cpu_joules > 0:
            self._seen["cpu"] = True
        if gpu_joules > 0:
            self._seen["gpu"] = True

        combined_joules = cpu_joules + gpu_joules
        if combined_joules <= 0 and energy["system_w"]:
            # Battery / board sensor: whole-system power is the only figure.
            combined_joules = max(0.0, energy["system_w"]) * elapsed
        # The UI divides the number by --interval, so it stores "joules per
        # interval" exactly like the macOS sampler does.
        scale = 1000.0 * self.interval / elapsed

        processor = {
            "clusters": clusters,
            "ane_energy": 0.0,
            "cpu_energy": cpu_joules * scale,
            "gpu_energy": gpu_joules * scale,
            "combined_power": combined_joules * scale,
            "cpu_energy_available": energy_available(
                self._seen["cpu"], self._sample_count
            ),
            "gpu_energy_available": energy_available(
                self._seen["gpu"], self._sample_count
            ),
            "ane_energy_available": False,
            "power_source": energy["cpu_source"] or energy["system_source"],
        }

        cards = [_gpu_payload(sample) for sample in gpu_samples]
        primary = _gpu_payload(gpu)
        primary["count"] = len(cards)
        return {
            "timestamp": time.time(),
            "thermal_pressure": self.thermal_pressure,
            "processor": processor,
            "gpu": primary,
            "gpus": cards,
            "disk": self._disk_rates(elapsed),
            "network": self._network_rates(elapsed),
        }

    # -- rates -------------------------------------------------------------

    def _disk_rates(self, elapsed: float) -> dict:
        current = disk_counters()
        previous = self.prev_disk
        self.prev_disk = current
        if current is None or previous is None:
            return {
                "rops_per_s": 0,
                "wops_per_s": 0,
                "rbytes_per_s": 0,
                "wbytes_per_s": 0,
            }
        reads = max(0, current[0] - previous[0]) / elapsed
        writes = max(0, current[1] - previous[1]) / elapsed
        read_bytes = max(0, current[2] - previous[2]) / elapsed
        write_bytes = max(0, current[3] - previous[3]) / elapsed
        return {
            "rops_per_s": int(reads),
            "wops_per_s": int(writes),
            "rbytes_per_s": int(read_bytes),
            "wbytes_per_s": int(write_bytes),
        }

    def _network_rates(self, elapsed: float) -> dict:
        current = net_counters()
        previous = self.prev_net
        self.prev_net = current
        sent = received = 0
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


def _gpu_payload(sample) -> dict:
    """One card in the shape the UI expects (see ``parse_gpu_metrics``).

    ``freq_hz`` has always held MHz in macpm's sample dictionary, and the UI
    multiplies it by 1e6 when it prints; keep that convention.
    """
    busy = sample.busy_pct
    return {
        "index": sample.index,
        "freq_hz": int(sample.freq_mhz or 0),
        "max_freq_mhz": sample.max_freq_mhz,
        "idle_ratio": (
            1.0 if busy is None else max(0.0, min(1.0, 1.0 - busy / 100.0))
        ),
        "busy_pct": busy,
        "name": sample.name,
        "driver": sample.driver,
        "power_w": sample.power_w,
        "mem_used_mb": sample.mem_used_mb,
        "mem_total_mb": sample.mem_total_mb,
        "temp_c": sample.temp_c,
    }


def _sane(watts: Optional[float], elapsed: float, limit: float) -> float:
    if watts is None:
        return 0.0
    return sane_energy(max(0.0, watts) * elapsed, elapsed, limit)
