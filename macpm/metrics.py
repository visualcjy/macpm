"""Platform-neutral sampling maths shared by the macOS and Linux samplers.

The state-residency / energy helpers used to live in ``macpm.sampler``; they
are re-exported from there for backwards compatibility.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

ENERGY_TO_J = {
    "mj": 1e-3,
    "uj": 1e-6,
    "µj": 1e-6,
    "μj": 1e-6,
    "nj": 1e-9,
    "pj": 1e-12,
    "j": 1.0,
}

ENERGY_GRACE_SAMPLES = 3
# Availability looks at this many recent samples so a counter that stops
# updating (macOS 27 freezes the CPU/ANE Energy Model channels without root)
# falls back to ``n/a`` instead of reporting a permanent 0 W.
ENERGY_WINDOW_SAMPLES = 10


def calc_freq(states: list, freqs: list) -> tuple[float, float]:
    """Return (average active frequency in MHz, active residency ratio).

    Mirrors the calculation used by other residency monitors: skip
    DOWN/IDLE/OFF, weight the remaining P-states by residency, and divide
    their residency by the total.  ``freqs`` is the DVFS table for those
    active states.  If the table is shorter than the state list the last
    known frequency is used for the extra states.
    """
    if not states:
        return 0.0, 0.0

    total = sum(residency for _, residency in states)
    if total <= 0:
        return 0.0, 0.0

    offset = None
    for index, (name, _) in enumerate(states):
        if name not in ("IDLE", "DOWN", "OFF"):
            offset = index
            break
    if offset is None:
        return 0.0, 0.0

    active_states = states[offset:]
    usage = sum(residency for _, residency in active_states)
    if usage <= 0:
        return 0.0, 0.0

    if not freqs:
        return 0.0, usage / total

    last_freq = float(freqs[-1])
    average = sum(
        residency / usage * (freqs[i] if i < len(freqs) else last_freq)
        for i, (_, residency) in enumerate(active_states)
    )
    return float(average), usage / total


def ane_usage(raw: dict) -> Optional[float]:
    """Share of the interval the ANE spent Running, or ``None``.

    ``ANE/status`` is a residency channel in 24 MHz ticks; the sum of every
    state is the interval, so Running/total is a measured utilisation rather
    than a power estimate.  ``None`` means the channel or its states were not
    available, never "0 % but unknown".
    """
    channel = (raw or {}).get("status")
    states = getattr(channel, "states", None)
    if not states:
        return None
    total = 0
    running = 0
    for name, residency in states:
        value = max(0, int(residency or 0))
        total += value
        if name.strip().lower() == "running":
            running += value
    if total <= 0:
        return None
    return min(100.0, running / total * 100.0)


def energy_to_joules(value: int, unit: str) -> float:
    return max(0.0, float(value)) * ENERGY_TO_J.get((unit or "").strip().lower(), 1e-3)


def energy_available(seen: bool) -> bool:
    """Whether a component's energy counter reported a non-zero delta.

    A counter that has never produced a usable reading — or stopped doing so,
    which is what macOS 27 does to the CPU channel without root — must be
    shown as unavailable rather than as a plausible 0 W.
    """
    return bool(seen)


def energy_warming(samples: int) -> bool:
    """The first frames of a session, before a counter has had a chance."""
    return samples <= ENERGY_GRACE_SAMPLES


class EnergyTracker:
    """Recent-sample availability for one energy counter.

    ``available`` is true while the last ``ENERGY_WINDOW_SAMPLES`` deltas
    contained a non-zero reading, so a counter that reports once and then
    freezes stops being trusted; ``warming`` covers the first frames, before
    any reading has had a chance to arrive.
    """

    def __init__(self, window: int = ENERGY_WINDOW_SAMPLES) -> None:
        self._recent: deque = deque(maxlen=max(1, int(window)))
        self.samples = 0

    def add(self, joules: float) -> None:
        self.samples += 1
        self._recent.append(joules > 0.0)

    def available(self) -> bool:
        return any(self._recent)

    def warming(self) -> bool:
        return self.samples <= ENERGY_GRACE_SAMPLES


def sane_energy(joules: float, elapsed: float, max_power: float) -> float:
    """Drop an energy delta that implies an impossible average power."""
    if joules <= 0:
        return 0.0
    if joules / max(elapsed, 1e-3) > max_power:
        return 0.0
    return joules


def delta_rates(
    previous: Optional[tuple[int, int]],
    current: Optional[tuple[int, int]],
    elapsed: float,
) -> tuple[float, float]:
    """Per-second rates for two cumulative counters.

    A counter that went backwards (interface reset, counter wrap) yields 0
    instead of a huge bogus rate.
    """
    if previous is None or current is None or elapsed <= 0:
        return 0.0, 0.0
    read = current[0] - previous[0]
    write = current[1] - previous[1]
    if read < 0 or write < 0:
        return 0.0, 0.0
    return read / elapsed, write / elapsed
