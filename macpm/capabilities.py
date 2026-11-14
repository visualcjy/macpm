"""What this machine and this permission level can actually measure.

The UI hides or greys out panels from this report instead of printing a
meaningless ``0``.  macOS supports everything; Linux is detected at startup
(and refined after the first sample).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Capabilities:
    platform: str = "macos"
    root: bool = False
    cpu_freq: bool = True
    e_cluster: bool = True
    cpu_power: bool = True
    gpu_util: bool = True
    gpu_freq: bool = True
    gpu_power: bool = True
    ane: bool = True
    temps: bool = True
    fans: bool = True
    proc_gpu: bool = True
    proc_net: bool = True
    thermal_pressure: bool = True
    # True when the GPU figure is derived from this user's own DRM clients
    # (Intel i915/xe on Linux without root): other processes are invisible.
    gpu_util_self_only: bool = False

    @property
    def power_panel(self) -> bool:
        """Whether a power figure exists at all (any source)."""
        return self.cpu_power or self.gpu_power or self.platform == "macos"

    @property
    def any_gpu(self) -> bool:
        return self.gpu_util or self.gpu_freq or self.gpu_power

    def as_dict(self) -> dict:
        return asdict(self)

    def missing(self) -> list:
        names = []
        for field in (
            "cpu_power",
            "gpu_util",
            "gpu_freq",
            "gpu_power",
            "ane",
            "temps",
            "fans",
            "proc_gpu",
        ):
            if not getattr(self, field):
                names.append(field)
        return names


MACOS_CAPABILITIES = Capabilities()
