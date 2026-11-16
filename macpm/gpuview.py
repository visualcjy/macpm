"""Dedicated GPU view (key 5), multi-GPU aware.

The top box lists every detected card — index, name, utilisation, clock, power,
VRAM and temperature — one row per GPU, so an 8-GPU server is readable at a
glance.  The table below ranks processes by GPU time.

Linux fills ``sample["gpus"]`` with one entry per card (amdgpu sysfs, Intel
``gt_*``/``fdinfo``, ``nvidia-smi`` rows); macOS reports a single GPU.  The
process column comes from the same reader the process view uses (DRM ``fdinfo``
on Linux, IOAccelerator on macOS).  Per-process memory is a GPU number, never
RSS: AMD/Intel DRM cards get it from ``fdinfo`` through the private-client gate
(``linux_backend.proc``) and are grouped by the card they hold, NVIDIA/Iluvatar
/… rows get it from the vendor tool, and an unknown figure stays ``n/a``.
"""

from __future__ import annotations

import math
import os
import pwd
import re
import time
from dataclasses import dataclass
from typing import Optional

from blessed import Terminal

from .backend import (
    gpu_process_detail_source,
    gpu_process_memory_source,
    gpu_process_source,
)
from .capabilities import MACOS_CAPABILITIES, Capabilities
from .processes import ProcessRow, ProcessSampler
from .tui import RESET_FG, _border, _bottom, _center, _fit, _format_mem, _pad, _text_width, footer

_SORTS = ["gpu", "pid", "name", "user"]
_HEADER = {
    "pid": "PID",
    "name": "NAME",
    "user": "USER",
    "card": "CARD",
    "gpu": "GPU%",
    "mem": "GPU MEM",
    "shared": "SHARED",
    "model": "MODEL",
}
_FOOTER = footer("q quit  1-5 view  s sort  ↑/↓ page  Space pause  ←/→ colour")
_FOOTER_PAUSED = footer("q quit  1-5 view  s sort  ↑/↓ page  Space resume  ←/→ colour")

# Per-card table columns: (key, header, width).  NAME takes the remaining
# space; the trailing columns are dropped as the terminal narrows.
_CARD_COLUMNS = (
    ("idx", "#", 3),
    ("busy", "BUSY", 18),
    ("clock", "CLOCK", 9),
    ("power", "POWER", 7),
    ("vram", "VRAM", 22),
    ("temp", "TEMP", 5),
    ("mem", "MEM", 4),
    ("enc", "ENC", 4),
    ("dec", "DEC", 4),
    ("pstate", "P", 3),
)
_CARD_HEADERS = {key: header for key, header, _ in _CARD_COLUMNS}
_CARD_HEADERS["name"] = "NAME"
_CARD_MIN_NAME = 18


@dataclass
class GpuDetails:
    index: int = 0
    name: str = ""
    driver: str = ""
    busy_pct: Optional[float] = None
    freq_mhz: Optional[float] = None
    max_freq_mhz: Optional[float] = None
    power_w: Optional[float] = None
    mem_used_mb: Optional[float] = None
    mem_total_mb: Optional[float] = None
    temp_c: Optional[float] = None
    busy_recent: Optional[float] = None
    mem_util_pct: Optional[float] = None
    enc_pct: Optional[float] = None
    dec_pct: Optional[float] = None
    pstate: str = ""
    note: str = ""
    # macOS only: the IOAccelerator reports its engine split separately from
    # IOReport's utilisation, and how many GPU cores the chip has.
    renderer_pct: Optional[float] = None
    tiler_pct: Optional[float] = None
    cores: Optional[int] = None

    @property
    def mem_pct(self) -> Optional[float]:
        if self.mem_used_mb is None or not self.mem_total_mb:
            return None
        return max(0.0, min(100.0, self.mem_used_mb / self.mem_total_mb * 100.0))


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _gpu_memory_mb(row) -> Optional[float]:
    """The GPU memory to show for a process, or ``None`` when unknown.

    Dedicated memory wins; an integrated GPU has none, so its GTT bytes are
    the process's GPU memory rather than spill.  RSS is deliberately not a
    fallback: system memory in a GPU-memory column is a lie.
    """
    if row.gpu_mem_mb is not None:
        return row.gpu_mem_mb
    return row.gpu_shared_mb


def _memory_text(mb: Optional[float]) -> str:
    if mb is None:
        return "n/a"
    return _format_mem(int(mb * 1024 * 1024))


def _card_label(cards: list) -> str:
    """``[0]`` / ``[0-1]`` / ``[0,2]`` for the cards a process holds."""
    if not cards:
        return ""
    ordered = sorted(set(cards))
    consecutive = ordered == list(range(ordered[0], ordered[0] + len(ordered)))
    if len(ordered) == 1:
        return str(ordered[0])
    if consecutive:
        return f"{ordered[0]}-{ordered[-1]}"
    return ",".join(str(card) for card in ordered)


def _details(
    gpu: dict,
    processor: dict,
    capabilities: Capabilities,
    interval: float,
    sensors,
    index: int,
) -> GpuDetails:
    busy = gpu.get("busy_pct")
    if busy is None:
        idle = gpu.get("idle_ratio")
        busy = None if idle is None else (1.0 - float(idle)) * 100.0

    power = gpu.get("power_w")
    if power is None and processor.get("gpu_energy_available", True):
        energy_mj = processor.get("gpu_energy")
        if energy_mj is not None:
            power = float(energy_mj) / 1000.0 / max(interval, 1e-3)

    temperature = gpu.get("temp_c")
    if temperature is None and sensors is not None:
        temperature = getattr(sensors, "gpu_c", None)

    name = gpu.get("name") or (
        "Apple GPU" if capabilities.platform == "macos" else f"GPU {index}"
    )
    driver = gpu.get("driver") or ("IOReport" if capabilities.platform == "macos" else "")

    mem_used = _as_float(gpu.get("mem_used_mb"))
    note = ""
    if capabilities.gpu_util_self_only:
        note = "own processes only"
    elif capabilities.platform == "macos" and mem_used is not None:
        # The IOAccelerator only reports a total: every process's Metal
        # allocations, not the sum of the rows below.
        note = "GPU alloc: all processes"

    return GpuDetails(
        index=index,
        name=name,
        driver=driver,
        busy_pct=busy,
        freq_mhz=_as_float(gpu.get("freq_hz")),
        max_freq_mhz=_as_float(gpu.get("max_freq_mhz")),
        power_w=power,
        mem_used_mb=mem_used,
        mem_total_mb=_as_float(gpu.get("mem_total_mb")),
        temp_c=temperature,
        busy_recent=_as_float(gpu.get("busy_recent")),
        mem_util_pct=_as_float(gpu.get("mem_util_pct")),
        enc_pct=_as_float(gpu.get("enc_pct")),
        dec_pct=_as_float(gpu.get("dec_pct")),
        pstate=str(gpu.get("pstate") or ""),
        note=note,
        renderer_pct=_as_float(gpu.get("renderer_pct")),
        tiler_pct=_as_float(gpu.get("tiler_pct")),
        cores=_as_int(gpu.get("gpu_cores")),
    )


def build_details(
    parse: Optional[dict],
    capabilities: Capabilities = MACOS_CAPABILITIES,
    interval: float = 1.0,
    sensors=None,
) -> GpuDetails:
    """The primary card (busiest one on Linux) as a single object."""
    parse = parse or {}
    return _details(
        parse.get("gpu") or {},
        parse.get("processor") or {},
        capabilities,
        interval,
        sensors,
        0,
    )


def build_cards(
    parse: Optional[dict],
    capabilities: Capabilities = MACOS_CAPABILITIES,
    interval: float = 1.0,
    sensors=None,
) -> list:
    """Every card in the sample, in the order the backend reported them."""
    parse = parse or {}
    processor = parse.get("processor") or {}
    entries = parse.get("gpus")
    if not entries:
        entries = [parse.get("gpu") or {}]
    cards = []
    for index, entry in enumerate(entries):
        cards.append(
            _details(
                entry or {},
                processor,
                capabilities,
                interval,
                sensors if index == 0 else None,
                index,
            )
        )
    return cards


def _bar(percent: float, width: int) -> str:
    width = max(0, width)
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100.0))
    if width == 0:
        return ""
    if filled >= width:
        return "▉" * width
    if filled <= 0:
        return "▏" * width
    return "▉" * filled + "▏" * (width - filled)


def _format_mb(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value >= 1024:
        return f"{value / 1024:.1f}G"
    return f"{value:.0f}M"


def _card_start(text) -> int:
    match = re.match(r"(\d+)", text or "")
    return int(match.group(1)) if match else -1


def _username(pid: int) -> str:
    try:
        return pwd.getpwuid(os.stat(f"/proc/{pid}").st_uid).pw_name
    except (OSError, KeyError):
        return "-"


class _VendorProcessSampler:
    """Rows supplied by a vendor ``*smi`` tool instead of psutil/fdinfo.

    These tools list the compute processes (pid, name, owning card and memory)
    but usually not an SM percentage; ``util_known`` is false then and the view
    prints a dash instead of a fake 0.0.
    """

    gpu_available = True

    def __init__(self, reader):
        self.reader = reader
        self.rows = []

    def reset(self) -> None:
        self.rows = []

    def sample(self) -> list:
        rows = []
        users = {}
        for record in self.reader():
            pid = record["pid"]
            user = users.get(pid)
            if user is None:
                user = users[pid] = _username(pid)
            util = record.get("util_pct")
            mem_mb = record.get("mem_mb")
            rows.append(
                ProcessRow(
                    pid=pid,
                    name=record.get("name") or str(pid),
                    user=user,
                    cpu_pct=0.0,
                    mem_bytes=int((mem_mb or 0) * 1024 * 1024),
                    gpu_pct=util or 0.0,
                    gpu_card=record.get("card"),
                    gpu_mem_mb=mem_mb,
                    util_known=util is not None,
                    model=record.get("model") or None,
                    gpu_list=record.get("gpus") or None,
                )
            )
        return rows


class GpuView:
    """GPU overview shown on key 5: one row per card plus the process ranking."""

    def __init__(
        self,
        color: int = 2,
        capabilities: Optional[Capabilities] = None,
        refresh_interval: float = 2.0,
    ):
        self.color = color
        self.caps = capabilities or MACOS_CAPABILITIES
        self.refresh_interval = max(1.0, float(refresh_interval))
        source = None
        try:
            source = gpu_process_source()
        except Exception:
            source = None
        self.sampler = (
            _VendorProcessSampler(source) if source is not None else ProcessSampler()
        )
        details = None
        try:
            details = gpu_process_detail_source()
        except Exception:
            details = None
        self._details_source = details
        memory = None
        try:
            memory = gpu_process_memory_source()
        except Exception:
            memory = None
        self._memory_source = memory
        self.rows: list = []
        self.cards: list = []
        self._sorted: Optional[list] = None
        self.sort = "gpu"
        self.desc = True
        # Multi-GPU mode sorts the processes inside each card with ``s``.
        self.proc_sort = "pid"
        self.offset = 0
        self.page = 0
        self._pages = 1
        self._info = {}
        self._info_source = None
        self.ready = False
        self.paused = False
        self._last_sample = 0.0
        self._term = None
        self._body_height = 0
        self._content_height = 0
        self._lines_total = 0

    # -- lifecycle / keys --------------------------------------------------

    def prime(self) -> None:
        """Reset the GPU-time baselines and show "collecting" until the next sample."""
        self.sampler.reset()
        self.rows = self._collect()
        self._sorted = None
        self._last_sample = time.monotonic()
        self.ready = False
        self.paused = False
        self.offset = 0

    def sample(self) -> None:
        if self.paused:
            return
        now = time.monotonic()
        if self.ready and now - self._last_sample < self.refresh_interval:
            return
        self._last_sample = now
        self.rows = self._collect()
        self._sorted = None
        self.ready = True

    def _collect(self) -> list:
        rows = self.sampler.sample()
        rows = self._attach_details(rows)
        return self._attach_footprint(rows)

    def _attach_details(self, rows: list) -> list:
        """Fill in per-process GPU memory and card for DRM cards.

        Memory comes from the card's own counters (``fdinfo``), not from the
        process table: a row keeps ``n/a`` when the kernel cannot prove the
        bytes are private, instead of showing RSS as if it were VRAM.
        """
        source = getattr(self, "_details_source", None)
        if source is None or not rows:
            return rows
        try:
            details = source() or {}
        except Exception:
            return rows
        if not details:
            return rows
        for row in rows:
            detail = details.get(row.pid)
            if not detail:
                continue
            vram_mb = detail.get("vram_bytes")
            gtt_mb = detail.get("gtt_bytes")
            vram_mb = None if vram_mb is None else vram_mb / (1024.0 * 1024.0)
            gtt_mb = None if gtt_mb is None else gtt_mb / (1024.0 * 1024.0)
            # Idle processes that hold a client but nothing worth showing stay
            # out of the ranking; a few idle megabytes is not "using the GPU".
            if (
                row.gpu_pct <= 0.05
                and (vram_mb or 0) < 16.0
                and (gtt_mb or 0) < 16.0
            ):
                continue
            cards = detail.get("cards") or []
            if row.gpu_card is None and cards:
                row.gpu_card = _card_label(cards)
                if len(cards) > 1:
                    row.gpu_list = list(cards)
            if row.gpu_mem_mb is None:
                row.gpu_mem_mb = vram_mb
            if row.gpu_shared_mb is None:
                row.gpu_shared_mb = gtt_mb
            if detail.get("reason"):
                row.gpu_note = detail["reason"]
        return rows

    def _attach_footprint(self, rows: list) -> list:
        """Per-process GPU memory on macOS, measured in the background.

        ``footprint`` costs ~15 ms per process, so the poller is handed the
        rows as targets and the view only reads what is already known; a
        process that has not been measured yet stays ``n/a``.
        """
        source = getattr(self, "_memory_source", None)
        if source is None or not rows:
            return rows
        gpu_pids = getattr(getattr(self, "sampler", None), "gpu_pids", None)
        targets = [
            (row.pid, row.gpu_pct > 0.5)
            for row in rows
            if row.gpu_mem_mb is None
            and row.gpu_card is None
            and (gpu_pids is None or row.pid in gpu_pids)
        ]
        try:
            source.set_targets(targets)
        except Exception:
            return rows
        for row in rows:
            if row.gpu_mem_mb is not None:
                continue
            try:
                value = source.get(row.pid)
            except Exception:
                value = None
            if value is None:
                row.gpu_note = row.gpu_note or "footprint"
                continue
            row.gpu_mem_mb = value / (1024.0 * 1024.0)
            if row.gpu_note == "footprint":
                row.gpu_note = None
        return rows

    def close(self) -> None:
        """Stop the macOS measurement thread (the rest of the view is stateless)."""
        source = getattr(self, "_memory_source", None)
        self._memory_source = None
        if source is not None:
            try:
                source.close()
            except Exception:
                pass

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if not self.paused:
            self.sampler.reset()
            self._last_sample = 0.0

    def cycle_sort(self) -> None:
        if len(self.cards) > 1:
            order = ["pid", "name", "mem"]
            self.proc_sort = order[(order.index(self.proc_sort) + 1) % len(order)]
            self._sorted = None
            self.offset = 0
            return
        index = (_SORTS.index(self.sort) + 1) % len(_SORTS) if self.sort in _SORTS else 0
        self.sort = _SORTS[index]
        self.desc = self.sort == "gpu"
        self._sorted = None
        self.offset = 0

    def scroll(self, delta: int) -> None:
        """``delta`` is in pages: ↑/↓ turn a whole page."""
        pages = max(1, getattr(self, "_pages", 1))
        self.page = max(0, min(pages - 1, self.page + delta))

    # -- rows --------------------------------------------------------------

    def _visible_rows(self) -> list:
        if self._sorted is None:
            rows = [
                row
                for row in self.rows
                if row.gpu_card is not None
                or not row.util_known
                or row.gpu_pct > 0.05
                # A process can hold GPU memory while it is momentarily idle
                # (a browser between frames).  Keep it listed, like the
                # activity-based rows, instead of making the table flicker.
                or (row.gpu_mem_mb or 0.0) >= 16.0
                or (row.gpu_shared_mb or 0.0) >= 16.0
            ]
            key = {
                "gpu": lambda row: (
                    -_card_start(row.gpu_card),
                    row.gpu_pct,
                    row.gpu_mem_mb or 0.0,
                    -row.pid,
                ),
                "pid": lambda row: row.pid,
                "name": lambda row: (row.name.lower(), row.pid),
                "user": lambda row: (row.user.lower(), row.pid),
            }[self.sort]
            rows.sort(key=key, reverse=self.desc)
            self._sorted = rows
        return self._sorted

    # -- card table --------------------------------------------------------

    def _card_layout(self, inner: int, multi: bool = False) -> tuple:
        widths = {key: width for key, _, width in _CARD_COLUMNS}
        columns = ["idx", "name", "busy", "clock", "power", "vram", "temp",
                   "mem", "enc", "dec", "pstate"]
        if multi:
            # Encode/decode engines only make sense when a single card is the
            # whole story; on an 8-GPU host they are just noise.
            columns.remove("enc")
            columns.remove("dec")
        for key in ("pstate", "dec", "enc", "mem", "temp", "vram", "clock",
                    "power", "busy"):
            fixed = sum(widths[column] for column in columns if column != "name")
            fixed += len(columns) - 1
            if inner - fixed >= _CARD_MIN_NAME:
                break
            columns.remove(key)
        fixed = sum(widths[column] for column in columns if column != "name")
        fixed += len(columns) - 1
        widths["name"] = max(6, inner - fixed)
        return columns, widths

    def _card_header(self, columns: list, widths: dict) -> str:
        return " ".join(
            _pad(_CARD_HEADERS[key], widths[key], right=key != "name")
            for key in columns
        )

    def _card_cells(self, card: GpuDetails) -> dict:
        if card.busy_pct is None:
            busy = _pad("n/a", 18, right=True)
        else:
            busy = _bar(card.busy_pct, 6) + f" {card.busy_pct:>3.0f}%"
            if card.busy_recent is not None and card.busy_recent > card.busy_pct + 1:
                busy += f" \N{UPWARDS ARROW}{card.busy_recent:.0f}%"
        if card.freq_mhz is None:
            clock = "n/a"
        elif card.max_freq_mhz:
            clock = f"{card.freq_mhz:.0f}/{card.max_freq_mhz:.0f}"
        else:
            clock = f"{card.freq_mhz:.0f} MHz"
        if card.mem_total_mb:
            vram = (
                f"{_bar(card.mem_pct or 0.0, 10)} "
                f"{_format_mb(card.mem_used_mb):>5}/{_format_mb(card.mem_total_mb)}"
            )
        elif self.caps.platform == "macos":
            vram = "unified"
        else:
            vram = "n/a"
        def pct(value):
            return "n/a" if value is None else f"{value:.0f}%"

        return {
            "idx": str(card.index),
            "name": card.name[len("NVIDIA "):] if card.name.startswith("NVIDIA ") else card.name,
            "busy": busy,
            "clock": clock,
            "power": "n/a" if card.power_w is None else f"{card.power_w:.1f}W",
            "vram": vram,
            "temp": "n/a" if card.temp_c is None else f"{card.temp_c:.0f}\N{DEGREE SIGN}C",
            "mem": pct(card.mem_util_pct),
            "enc": pct(card.enc_pct),
            "dec": pct(card.dec_pct),
            "pstate": card.pstate or "n/a",
        }

    def _card_row(self, card: GpuDetails, columns: list, widths: dict) -> str:
        cells = self._card_cells(card)
        return " ".join(
            _pad(cells[key], widths[key], right=key not in ("name",))
            for key in columns
        )

    def _cards_box(self, cards: list, width: int, rows: int) -> list:
        inner = max(0, width - 4)
        columns, widths = self._card_layout(inner, multi=len(cards) > 1)
        total_power = sum(card.power_w for card in cards if card.power_w)
        parts = []
        if total_power >= 10:
            parts.append(f"{total_power:.0f}W")
        elif total_power > 0:
            parts.append(f"{total_power:.1f}W")
        note = cards[0].note if cards else ""
        if note:
            parts.append(note)
        right = f" {' · '.join(parts)} " if parts else ""
        title = f" GPUs ({len(cards)}) "
        if rows < len(cards):
            title = f" GPUs ({len(cards)}, showing {rows}) "
        lines = [_border(width, title, right)]
        lines.append("│ " + _pad(self._card_header(columns, widths), inner) + " │")
        if not cards:
            for index in range(rows):
                text = _center("no GPU detected", inner) if index == 0 else ""
                lines.append("│ " + _pad(text, inner) + " │")
        else:
            for card in cards[:rows]:
                lines.append(
                    "│ " + _pad(self._card_row(card, columns, widths), inner) + " │"
                )
            for _ in range(rows - min(len(cards), rows)):
                lines.append("│ " + " " * inner + " │")
        lines.append(_bottom(width, ""))
        return lines


    # -- multi-GPU layout: two lines per card (metrics, then its processes) --

    def _per_card_rows(self, rows: list) -> dict:
        """Rows grouped by card; a multi-card process repeats on each card."""
        order = getattr(self, "proc_sort", "pid")
        key = {
            "pid": lambda row: row.pid,
            "name": lambda row: (row.name.lower(), row.pid),
            "mem": lambda row: -(_gpu_memory_mb(row) or 0.0),
        }[order]
        per_card = {}
        for row in rows:
            gpus = row.gpu_list or []
            if gpus:
                for gpu in gpus:
                    per_card.setdefault(max(0, gpu), []).append(row)
            else:
                index = max(0, _card_start(row.gpu_card))
                per_card.setdefault(index, []).append(row)
        for items in per_card.values():
            items.sort(key=key)
        return per_card

    def _process_text(self, row: ProcessRow) -> str:
        memory = _memory_text(_gpu_memory_mb(row))
        span = ""
        cards = row.gpu_card or ""
        if any(char in cards for char in "-,"):
            span = f" [{cards}]"
        util = f"{row.gpu_pct:.0f}% " if row.util_known else ""
        return f"{row.pid} {row.name}{span} {util}{memory}"

    def _process_line(self, items: list, inner: int) -> str:
        """Second line for one card: as many processes as fit, then ``+N``."""
        model = next(
            (row.model for row in items if getattr(row, "model", None)), ""
        )
        label = _fit(model, max(6, inner // 3)) if model else ""
        reserve = _text_width(label) + 2 if label else 0
        budget = max(0, inner - 4 - reserve)
        shown = []
        used = 0
        hidden = 0
        for row in items:
            text = self._process_text(row)
            cost = _text_width(text) + (3 if shown else 0)
            if shown and used + cost > budget:
                hidden += 1
                continue
            shown.append(text)
            used += cost
        def compose() -> str:
            text = "      └ " + " · ".join(shown)
            return text + (f" +{hidden}" if hidden else "")

        text = compose()
        # Keep the "+N" marker and the right-aligned model inside the panel.
        while len(shown) > 1 and _text_width(text) > inner - reserve:
            shown.pop()
            hidden += 1
            text = compose()

        if label:
            return _pad(_fit(text, inner - reserve), inner - _text_width(label)) + label
        return _fit(text, inner)

    # -- info box ----------------------------------------------------------

    def _fetch_info(self) -> dict:
        if getattr(self, "_info_source", None) is None:
            try:
                from .backend import gpu_info_source

                self._info_source = gpu_info_source() or False
            except Exception:
                self._info_source = False
        if not self._info_source:
            return {}
        try:
            return self._info_source() or {}
        except Exception:
            return {}

    def _info_lines(self, cards: list) -> list:
        info = self._info or {}
        name = info.get("name") or (cards[0].name if cards else "GPU")
        first = [name]
        count = info.get("count")
        if count:
            first.append(f"{int(count)} devices")
        elif cards:
            first.append(f"{len(cards)} device" + ("s" if len(cards) != 1 else ""))
        cards_n = info.get("cards")
        if cards_n and count and int(cards_n) != int(count):
            first.append(f"{int(cards_n)} cards")
        if info.get("dies"):
            first.append(f"{int(info['dies'])} dies/card")

        second = []
        if info.get("driver"):
            second.append(f"driver {info['driver']}")
        if info.get("cuda"):
            second.append(f"CUDA {info['cuda']}")
        if info.get("pcie_gen"):
            width = info.get("pcie_width")
            link = f"x{int(width)}" if width else ""
            second.append(f"PCIe {int(info['pcie_gen'])} {link}".strip())
        vbios = info.get("vbios") or ""
        if vbios and vbios not in ("N/A", "[N/A]"):
            second.append(f"VBIOS {vbios}")
        if info.get("vendor"):
            second.append(info["vendor"])
        if self.caps.platform == "macos":
            card = cards[0] if cards else None
            if card is not None and card.cores:
                second.append(f"{card.cores} GPU cores")
            second.append("unified memory")
            if card is not None and card.renderer_pct is not None:
                second.append(f"renderer {card.renderer_pct:.0f}%")
            if card is not None and card.tiler_pct is not None:
                second.append(f"tiler {card.tiler_pct:.0f}%")
            second.append("IOReport")

        lines = ["  ·  ".join(str(part) for part in first)]
        if second:
            lines.append("  ·  ".join(str(part) for part in second))
        return lines

    def _info_box(self, term, width: int, inner: int, lines: list, accent: str) -> list:
        reset = RESET_FG
        out = [term.move(0, 0) + accent + _border(width, " GPU INFO ") + reset]
        for index, line in enumerate(lines):
            out.append(
                term.move(1 + index, 0)
                + accent
                + "│ " + _pad(line, inner) + " │"
                + reset
            )
        out.append(
            term.move(1 + len(lines), 0) + accent + _bottom(width, "") + reset
        )
        return out

    # -- single-GPU layout: card summary + process table -------------------

    def _process_layout(
        self, inner: int, has_model: bool = False, has_shared: bool = False
    ) -> tuple:
        widths = {
            "pid": 7,
            "card": 5,
            "user": 9,
            "gpu": 6,
            "mem": 7,
            "shared": 7,
            "model": 18,
        }
        columns = ["pid", "name", "card", "user", "gpu", "mem"]
        if getattr(self.caps, "platform", "") == "macos":
            # A Mac has one integrated GPU; a CARD column of dashes is noise.
            columns.remove("card")
            widths.pop("card")
        if has_shared:
            columns.append("shared")
        if has_model:
            columns.append("model")
        for key in ("model", "shared", "mem", "user", "card"):
            if key not in columns:
                continue
            fixed = sum(widths[k] for k in columns if k != "name") + len(columns) - 1
            if inner - fixed >= 12:
                break
            columns.remove(key)
            widths.pop(key)
        fixed = sum(widths[k] for k in columns if k != "name") + len(columns) - 1
        widths["name"] = max(6, inner - fixed)
        return columns, widths

    def _process_header(self, columns: list, widths: dict) -> str:
        cells = []
        for key in columns:
            label = _HEADER[key]
            if key == self.sort:
                label += " ↓" if self.desc else " ↑"
            cells.append(_pad(label, widths[key], right=key != "name"))
        return " ".join(cells)

    def _process_row(self, row: ProcessRow, columns: list, widths: dict) -> str:
        memory = _memory_text(_gpu_memory_mb(row))
        shared = (
            _memory_text(row.gpu_shared_mb)
            if row.gpu_mem_mb is not None and row.gpu_shared_mb is not None
            else "-"
        )
        values = {
            "pid": str(row.pid),
            "name": row.name,
            "user": row.user,
            "card": row.gpu_card or "-",
            "gpu": f"{row.gpu_pct:.1f}" if row.util_known else "—",
            "mem": memory,
            "shared": shared,
            "model": row.model or "-",
        }
        return " ".join(
            _pad(values[key], widths[key], right=key != "name")
            for key in columns
        )

    def _single_body(self, cards, rows, width, height, top, inner, accent) -> list:
        available = height - top
        max_card_rows = max(1, available - 9)
        card_rows = max(1, min(len(cards) or 1, max_card_rows))
        cards_height = card_rows + 3
        body_height = max(0, available - cards_height - 3)
        self._body_height = body_height
        self._pages = max(1, math.ceil(len(rows) / body_height)) if body_height else 1
        self.page = min(getattr(self, "page", 0), self._pages - 1)
        start = self.page * body_height

        out = []
        for index, line in enumerate(self._cards_box(cards, width, card_rows)):
            out.append(
                self._move(top + index, 0) + accent + line + RESET_FG
            )

        proc_top = top + cards_height
        title = f" processes by GPU {len(rows)} "
        has_model = any(getattr(row, "model", None) for row in rows)
        has_shared = any(
            row.gpu_mem_mb is not None and row.gpu_shared_mb is not None
            for row in rows
        )
        columns, widths = self._process_layout(inner, has_model, has_shared)
        out += [
            self._move(proc_top, 0),
            accent,
            _border(width, title, " s sort "),
            RESET_FG,
            self._move(proc_top + 1, 0),
            accent,
            "│ " + _pad(self._process_header(columns, widths), inner) + " │",
            RESET_FG,
        ]

        placeholder = (
            "no GPU detected on this machine"
            if (not self.caps.any_gpu and not self.sampler.gpu_available)
            else "collecting…"
        )
        for index in range(body_height):
            entry = start + index
            out.append(self._move(proc_top + 2 + index, 0))
            if not self.ready:
                content = _center(placeholder, inner)
            elif entry < len(rows):
                content = _pad(self._process_row(rows[entry], columns, widths), inner)
            else:
                content = " " * inner
            out.append(accent + "│ " + content + " │" + RESET_FG)

        out += [
            self._move(height - 1, 0),
            accent,
            _bottom(width, self._footer_text()),
            RESET_FG,
        ]
        return out

    # -- multi-GPU layout: one table, two lines per card -------------------

    def _multi_body(self, cards, rows, width, height, top, inner, accent) -> list:
        content_height = max(1, height - top - 3)
        columns, widths = self._card_layout(inner, multi=len(cards) > 1)
        per_card = self._per_card_rows(rows)

        body = []
        for card in cards:
            body.append(_pad(self._card_row(card, columns, widths), inner))
            items = per_card.get(card.index, [])
            if items:
                body.append(_pad(self._process_line(items, inner), inner))

        self._lines_total = len(body)
        self._content_height = content_height
        self._pages = max(1, math.ceil(len(body) / content_height))
        self.page = min(getattr(self, "page", 0), self._pages - 1)
        start = self.page * content_height

        header = _pad(self._card_header(columns, widths), inner)
        title = f" GPUs ({len(cards)}) "
        if self._pages > 1:
            end = min(start + content_height, len(body))
            title = f" GPUs ({len(cards)})  {start + 1}-{end}/{len(body)} "
        total_power = sum(card.power_w for card in cards if card.power_w)
        right = ""
        if total_power >= 10:
            right = f" {total_power:.0f}W "
        elif total_power > 0:
            right = f" {total_power:.1f}W "

        out = [
            self._move(top, 0), accent, _border(width, title, right), RESET_FG,
            self._move(top + 1, 0), accent, "│ " + header + " │", RESET_FG,
        ]
        for index in range(content_height):
            position = start + index
            text = body[position] if position < len(body) else " " * inner
            out += [
                self._move(top + 2 + index, 0),
                accent,
                "│ " + text + " │",
                RESET_FG,
            ]
        out += [
            self._move(height - 1, 0),
            accent,
            _bottom(width, self._footer_text()),
            RESET_FG,
        ]
        return out

    # -- rendering ---------------------------------------------------------

    def _footer_text(self) -> str:
        base = _FOOTER_PAUSED if self.paused else _FOOTER
        if getattr(self, "_pages", 1) > 1:
            base += f" Page {getattr(self, 'page', 0) + 1}/{self._pages} "
        return base

    def _move(self, row: int, column: int) -> str:
        return self._term.move(row, column)

    def display(self, parse=None, args=None, sensors=None) -> None:
        term = self._term
        if term is None:
            term = self._term = Terminal()
        width, height = term.width, term.height
        if width < 40 or height < 12:
            return

        interval = getattr(args, "interval", 1.0)
        no_gpu = not self.caps.any_gpu and not self.sampler.gpu_available
        cards = [] if no_gpu else build_cards(parse or {}, self.caps, interval, sensors)
        self.cards = cards
        rows = self._visible_rows()
        inner = max(0, width - 4)
        accent = term.color(self.color)

        out = []
        top = 0
        if height >= 15:
            self._info = self._fetch_info()
            lines = self._info_lines(cards)
            if height < 17:
                lines = lines[:1]
            lines = lines[:2]
            if lines:
                out += self._info_box(term, width, inner, lines, accent)
                top = len(lines) + 2

        if len(cards) > 1:
            out += self._multi_body(cards, rows, width, height, top, inner, accent)
        else:
            out += self._single_body(cards, rows, width, height, top, inner, accent)
        out.append(term.move(height - 2, 0))
        print("".join(out), end="", flush=True)
