"""Local model-service view (key 6).

Discovery (:mod:`macpm.serviceregistry`) is HTTP-free; the metrics for the
selected row come from :mod:`macpm.modelfeed`, which polls only that endpoint.
A row that has not been looked at stays ``unverified``.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from blessed import Terminal

from .modelfeed import ModelFeed, ModelStats
from .serviceregistry import Service, ServiceRegistry
from .theme import (
    ADDRESS,
    BOX,
    CRIT,
    KEY,
    OK,
    WARN,
    metric_spans,
    HINT,
    LABEL,
    MUTED,
    TITLE,
    VALUE,
    Palette,
    footer_spans,
    paint_spans,
)
from .tui import RESET_FG, _border, _bottom, _center, _fit, _pad, footer

_FOOTER = footer("q quit  1-6 view  \u2191/\u2193 select  s sort  Space pause  r refresh")
_FOOTER_PAUSED = footer("q quit  1-6 view  \u2191/\u2193 select  s sort  Space resume  r refresh")

_SORTS = ("port", "backend", "model")
_VALUE_TOKEN = re.compile(r"\d+(?:\.\d+)?[A-Za-z%]*")
_STATE_WIDTH = 14


def _service_sort_key(sort: str):
    if sort == "model":
        return lambda service: (service.model.lower(), service.port)
    if sort == "backend":
        return lambda service: (service.backend, service.model.lower(), service.port)
    return lambda service: (service.port, service.backend, service.model.lower())


def _value_spans(text: str, base: str = LABEL) -> list:
    spans, position = [], 0
    for match in _VALUE_TOKEN.finditer(text):
        if match.start() > position:
            spans.append((text[position:match.start()], base))
        spans.append((match.group(0), VALUE))
        position = match.end()
    if position < len(text):
        spans.append((text[position:], base))
    return spans or [(text, base)]


def _pids_text(service: Service) -> str:
    pids = sorted(service.pids)
    if not pids:
        return "-"
    head = ", ".join(str(pid) for pid in pids[:3])
    if len(pids) > 3:
        head += " +{}".format(len(pids) - 3)
    return head


def _rate(value: Optional[float]) -> str:
    return "-" if value is None else "{:,.1f} tok/s".format(value)


def _tokens(value: Optional[int]) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return "{:.1f}M".format(value / 1_000_000)
    if value >= 1_000:
        return "{:.1f}K".format(value / 1_000)
    return str(value)


def _gib(value: Optional[float]) -> str:
    return "-" if value is None else "{:.1f} GiB".format(value / (1024 ** 3))


def _ttft(ms: Optional[float]) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return "{:.0f}ms".format(ms)
    return "{:.2f}s".format(ms / 1000.0)


def _seconds(ms: Optional[float]) -> str:
    return "-" if ms is None else "{:.2f}s".format(ms / 1000.0)


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600:
        return "{}h {:02d}m".format(seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "{}m {:02d}s".format(seconds // 60, seconds % 60)
    return "{}s".format(seconds)


def _format_rate(bps) -> str:
    if bps is None:
        return "-"
    bps = float(bps)
    if bps >= 1024 ** 3:
        return "{:.1f} GB/s".format(bps / 1024 ** 3)
    if bps >= 1024 ** 2:
        return "{:.1f} MB/s".format(bps / 1024 ** 2)
    if bps >= 1024:
        return "{:.1f} kB/s".format(bps / 1024)
    return "{:.0f} B/s".format(bps)


_BAR_WIDTH = [20]


def _bar(percent, width: int = 20) -> str:
    if percent is None:
        return " " * width
    filled = int(round(max(0.0, min(100.0, float(percent))) / 100.0 * width))
    return "\u2589" * filled + "\u2591" * (width - filled)


def _cards_text(cards: list) -> str:
    if not cards:
        return "-"
    ordered = sorted(set(cards))
    if ordered == list(range(ordered[0], ordered[-1] + 1)):
        return str(ordered[0]) if len(ordered) == 1 else "{}-{}".format(ordered[0], ordered[-1])
    return ",".join(str(card) for card in ordered)


def _percent(used: Optional[float], total: Optional[float]) -> Optional[int]:
    if not used or not total:
        return None
    return int(round(100.0 * used / total))


class ModelView:
    """Model-service list plus a detail panel for the selected row."""

    def __init__(
        self,
        color: int = 2,
        registry: Optional[ServiceRegistry] = None,
        feed: Optional[ModelFeed] = None,
        no_color: bool = False,
        native: bool = False,
        interval: float = 2.0,
        gpu_source=None,
    ):
        self.color = color
        self.no_color = no_color
        self.native = native
        self.interval = max(0.5, float(interval))
        self.registry = registry or ServiceRegistry()
        self.feed = feed or ModelFeed()
        self.services: list = []
        self.sort = "port"
        self.index = 0
        self.offset = 0
        self.paused = False
        self.ready = False
        self._last_scan = 0.0
        self._term: Optional[Terminal] = None
        self.gpu_source = gpu_source
        self._gpu: dict = {}
        self._gpu_at = -1e9
        self._speed_history: list = []
        self._history: dict = {}

    # -- lifecycle / keys --------------------------------------------------

    def prime(self) -> None:
        """Scan immediately so the view is never empty on first paint."""
        self.services = self.registry.discover(force=True)
        self.sort = "port"
        self.index = 0
        self.offset = 0
        self.paused = False
        self.ready = True
        self._last_scan = time.monotonic()
        self._clamp()
        self._sync_feed()

    def sample(self) -> None:
        if self.paused:
            return
        now = time.monotonic()
        if self.ready and now - self._last_scan < self.interval:
            self.feed.maybe_refresh()
            return
        self._last_scan = now
        self._replace(self.registry.discover())
        self.feed.maybe_refresh()
        self.feed.probe_states(self.services)

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if not self.paused:
            self._last_scan = 0.0

    def refresh(self) -> None:
        """Force a rescan (``r``)."""
        self._replace(self.registry.discover(force=True))
        self._last_scan = time.monotonic()
        self.feed.select(self.selected())

    def cycle_sort(self) -> None:
        index = (_SORTS.index(self.sort) + 1) % len(_SORTS) if self.sort in _SORTS else 0
        self.sort = _SORTS[index]
        self.index = 0
        self.offset = 0
        self._clamp()
        self._sync_feed()

    def select(self, delta: int) -> None:
        if not self.services:
            return
        self.index = max(0, min(len(self.services) - 1, self.index + delta))
        self._clamp()
        self._sync_feed()

    # -- selection ---------------------------------------------------------

    def ordered(self) -> list:
        return sorted(self.services, key=_service_sort_key(self.sort))

    def selected(self) -> Optional[Service]:
        ordered = self.ordered()
        if not ordered:
            return None
        return ordered[min(self.index, len(ordered) - 1)]

    def _replace(self, services: list) -> None:
        """Keep the selection on the same service across polls."""
        current = self.selected()
        identity = current.identity if current is not None else None
        self.services = services
        ordered = self.ordered()
        if identity is not None:
            for position, service in enumerate(ordered):
                if service.identity == identity:
                    self.index = position
                    break
        self._clamp()

    def _clamp(self) -> None:
        count = len(self.services)
        self.index = max(0, min(self.index, count - 1)) if count else 0

    def _sync_feed(self) -> None:
        self.feed.select(self.selected())

    # -- rendering ---------------------------------------------------------

    def display(self, parse=None, args=None, sensors=None) -> None:
        term = self._term
        if term is None:
            term = self._term = Terminal()
        width, height = term.width, term.height
        if width < 40 or height < 10:
            return

        palette = Palette(
            term,
            self.color,
            getattr(self, "native", False),
            getattr(self, "no_color", False),
        )
        ordered = self.ordered()
        stats = self.feed.snapshot() if ordered else None
        inner = max(0, width - 4)
        out: list = [term.clear]

        if not ordered:
            out += self._box(term, 0, width, 6, " MODEL SERVICES ",
                             self._empty_lines(inner), palette)
        elif len(ordered) == 1:
            lines = self._detail_lines(ordered[0], inner, stats)
            detail_height = min(height - 2, len(lines) + 2)
            grid = self._grid(parse, args, sensors, stats, width, height - 2 - detail_height, ordered[0])
            out += self._box(term, 0, width, detail_height,
                             " MODEL SERVICE · {} ".format(ordered[0].label), lines, palette)
            top = detail_height
            for index, spans in enumerate(grid):
                painted = paint_spans(spans, palette, width)
                out.append(term.move(top + index, 0) + painted)
        else:
            list_height = self._list_height(height, len(ordered))
            out += self._box(term, 0, width, list_height, self._list_title(len(ordered)),
                             self._list_lines(ordered, inner, list_height - 3, stats), palette)
            detail = ordered[self.index]
            lines = self._detail_lines(detail, inner, stats)
            remaining = height - 2 - list_height
            if remaining >= 4:
                detail_height = max(4, min(remaining, len(lines) + 2))
                out += self._box(term, list_height, width, detail_height,
                                 " {} · {} ".format(detail.label, detail.endpoint),
                                 lines, palette)
                below = height - 2 - list_height - detail_height
                if below >= 10:
                    grid = self._grid(parse, args, sensors, stats, width, below, detail)
                    for index, spans in enumerate(grid):
                        out.append(term.move(list_height + detail_height + index, 0)
                                   + paint_spans(spans, palette, width))

        text = _FOOTER_PAUSED if self.paused else _FOOTER
        fitted = _fit(text, max(0, width - 2))
        if fitted:
            out.append(
                term.move(height - 1, 1) + paint_spans(footer_spans(fitted), palette)
            )
        out.append(term.move(height - 2, 0))
        print("".join(out), end="", flush=True)

    # -- content -----------------------------------------------------------

    def _empty_lines(self, inner: int) -> list:
        lines = [
            (_center("no local model service found", inner), TITLE),
            ("", LABEL),
            (_center("vllm · sglang · llama.cpp · ollama · omlx · splash", inner), HINT),
            (_center("start one, or pass --model-url host:port", inner), HINT),
        ]
        if self.registry.error:
            lines[3] = (_center(self.registry.error, inner), HINT)
        return lines

    def _list_title(self, count: int) -> str:
        title = " MODEL SERVICES · {} detected · {} \u2191 ".format(count, self.sort)
        if self.paused:
            title = title[:-2] + " · paused "
        return title

    def _list_height(self, height: int, count: int) -> int:
        return max(4, min(count + 3, max(5, (height - 2) // 2)))

    def _list_lines(self, ordered: list, inner: int, body: int,
                    stats: Optional[ModelStats]) -> list:
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + body:
            self.offset = self.index - body + 1
        self.offset = max(0, min(self.offset, max(0, len(ordered) - body)))

        lines = [self._header_line(inner)]
        for position in range(self.offset, min(len(ordered), self.offset + body)):
            service = ordered[position]
            selected = position == self.index
            lines.append(self._row_line(service, selected, inner, stats))
        while len(lines) < body + 1:
            lines.append("")
        return lines

    def _columns(self, inner: int) -> tuple:
        """Model, backend, endpoint, cards and vram widths for one row."""
        backend, endpoint, cards, vram, state = 9, 15, 6, 9, _STATE_WIDTH
        fixed = (2 + 1 + backend + 1 + endpoint + 1 + cards + 1 + vram
                 + 1 + state)
        model = inner - fixed
        if model < 16:                       # narrow: drop cards + vram
            cards = vram = 0
            fixed = 2 + 1 + backend + 1 + endpoint + 1 + state
            model = inner - fixed
        if model < 16:                       # narrower: drop the endpoint
            endpoint = 0
            cards = vram = 0
            fixed = 2 + 1 + backend + 1 + state
            model = inner - fixed
        if model < 10:                       # very narrow: model + state
            backend = 0
            fixed = 2 + 1 + state
            model = inner - fixed
        if not self._gpu_index():            # no vendor table: nothing to show
            cards = vram = 0
        return max(6, min(model, 40)), backend, endpoint, cards, vram

    def _header_line(self, inner: int) -> str:
        model, backend, endpoint, cards, vram = self._columns(inner)
        text = "  " + _pad("MODEL", model)
        if backend:
            text += " " + _pad("BACKEND", backend)
        if endpoint:
            text += " " + _pad("ENDPOINT", endpoint)
        if cards:
            text += " " + _pad("CARDS", cards)
        if vram:
            text += " " + _pad("VRAM", vram, right=True)
        text += " " + _pad("STATE", _STATE_WIDTH, right=True)
        return _fit(text, inner)

    def _row_line(self, service: Service, selected: bool, inner: int,
                  stats: Optional[ModelStats]) -> tuple:
        model, backend, endpoint, cards, vram = self._columns(inner)
        text = ("\u25b6 " if selected else "  ") + _pad(_fit(service.model, model), model)
        if backend:
            text += " " + _pad(_fit(service.label, backend), backend)
        if endpoint:
            text += " " + _pad(service.endpoint, endpoint)
        if cards:
            usage = self._service_gpu(service)
            text += " " + _pad(usage[0] if usage else "-", cards)
        if vram:
            usage = self._service_gpu(service)
            text += " " + _pad(_gib(usage[1]) if usage else "-", vram, right=True)
        state = (self._state_text(stats) if selected
                 else (self.feed.state(service) or "unverified"))
        text += " " + _pad(state, _STATE_WIDTH, right=True)
        return (_fit(text, inner), TITLE if selected else LABEL)

    def _gpu_index(self) -> dict:
        """``pid -> (cards, bytes)`` from the vendor table, cached briefly."""
        now = time.monotonic()
        if now - self._gpu_at < 5.0:
            return self._gpu
        rows = {}
        try:
            source = self.gpu_source
            if source is None:
                from .backend import gpu_process_source
                source = gpu_process_source()
            for row in (list(source()) if source else []):
                rows[int(row.get("pid", 0))] = (
                    list(row.get("gpus") or []),
                    float(row.get("mem_mb") or 0.0) * 1024 * 1024,
                )
        except Exception:
            rows = {}
        self._gpu, self._gpu_at = rows, now
        return rows

    def _service_cards(self, service) -> set:
        index = self._gpu_index()
        cards = set()
        for pid in getattr(service, "pids", ()):
            entry = index.get(pid)
            if entry:
                cards.update(entry[0])
        return cards

    def _service_gpu(self, service: Service):
        index = self._gpu_index()
        cards, memory = set(), 0.0
        for pid in service.pids:
            entry = index.get(pid)
            if entry:
                cards.update(entry[0])
                memory += entry[1]
        if not cards and memory <= 0:
            return None
        return (_cards_text(cards), memory)

    def _state_text(self, stats: Optional[ModelStats]) -> str:
        if stats is None:
            return "unverified"
        if stats.state == "live":
            if stats.decode_tps:
                return "live · {:,.0f} t/s".format(stats.decode_tps)
            return "live"
        if stats.state == "auth":
            return "auth"
        if stats.state == "no-telemetry":
            return "no metrics"
        if stats.state == "stale":
            return "stale"
        return "error"

    def _detail_lines(self, service: Service, inner: int,
                      stats: Optional[ModelStats]) -> list:
        def field(name: str, value: str, role: str = VALUE) -> list:
            if role == VALUE:
                spans = _value_spans(_fit(value, inner - 10))
            else:
                spans = [(_fit(value, inner - 10), role)]
            return [(_pad(name, 10), KEY)] + spans

        model = (stats.model if stats and stats.model else service.model) or "-"
        lines = [
            field("model", model, LABEL),
            field("endpoint", service.endpoint, ADDRESS),
            field("state", self._state_detail(stats), MUTED),
        ]
        recent = []
        if stats is not None and stats.state == "live":
            extra, recent = self._metric_lines(stats, field, service)
            lines += extra
        lines = self._pair(lines)
        for row in recent:
            lines.append([(_fit(row, inner), MUTED)])
        if stats is not None and stats.note:
            lines.append(field("note", stats.note, HINT))
        return lines

    def _pair(self, lines: list) -> list:
        """Lay short fields out two per row to keep the card compact."""
        out = []
        for index in range(0, len(lines), 2):
            left = list(lines[index])
            width = sum(len(text) for text, _role in left)
            row = left
            if index + 1 < len(lines):
                gap = max(2, 40 - width)
                row = left + [(" " * gap, LABEL)] + list(lines[index + 1])
            out.append(row)
        return out

    def _state_detail(self, stats: Optional[ModelStats]) -> str:
        if stats is None:
            return "unverified · polling starts with the selection"
        if stats.state == "live":
            parts = ["live"]
            if stats.active is not None:
                parts.append("{} active".format(stats.active))
            if stats.waiting:
                parts.append("{} waiting".format(stats.waiting))
            for phase, count in sorted(stats.phases.items()):
                parts.append("{} {}".format(count, phase))
            return " · ".join(parts)
        if stats.state == "auth":
            return "auth required · set the backend's API key"
        if stats.state == "no-telemetry":
            return "no metrics endpoint"
        if stats.state == "stale":
            return "stale"
        return "error"

    def _metric_lines(self, stats: ModelStats, field, service: Service) -> tuple:
        lines = []
        lines.append(field("decode", _rate(stats.decode_tps)))
        lines.append(field("prefill", _rate(stats.prefill_tps)))
        if stats.slots_total:
            used = stats.slots_used if stats.slots_used is not None else stats.active
            text = "{} / {}".format("-" if used is None else used, stats.slots_total)
            if stats.waiting:
                text += "   queued {}".format(stats.waiting)
            lines.append(field("slots", text))
        if stats.ttft_p50_ms is not None:
            ttft = "{} p50 · {} p95".format(
                _ttft(stats.ttft_p50_ms), _ttft(stats.ttft_p95_ms)
            )
            lines.append(field("ttft", ttft))
        if stats.ctx_total:
            if stats.ctx_used:
                text = "{} / {}".format(_tokens(stats.ctx_used), _tokens(stats.ctx_total))
                pct = _percent(stats.ctx_used, stats.ctx_total)
                if pct is not None:
                    text += "  {}%".format(pct)
            else:
                text = "idle · capacity {}".format(_tokens(stats.ctx_total))
            lines.append(field("context", text))
        if stats.kv_pct is not None:
            lines.append(field("kv", "{:.0f}%  of the KV pool".format(stats.kv_pct)))
        if stats.kv_used is not None:
            text = "{} / {}".format(_gib(stats.kv_used), _gib(stats.kv_total))
            pct = _percent(stats.kv_used, stats.kv_total)
            lines.append(field("kv", text + ("  {}%".format(pct) if pct is not None else "")))
        usage = self._service_gpu(service)
        if usage is not None:
            lines.append(field("gpu", "cards {} · {}".format(usage[0], _gib(usage[1]))))
        if stats.mem_used is not None:
            text = _gib(stats.mem_used)
            if stats.mem_peak:
                text += " · peak {}".format(_gib(stats.mem_peak))
            lines.append(field("memory", text))
        if stats.prefix_hit is not None:
            lines.append(field("cache hit", "{:.1f}%  prefix".format(stats.prefix_hit)))
        if stats.requests is not None:
            text = "{} done".format(stats.requests)
            if stats.failed:
                text += " · {} failed".format(stats.failed)
            if stats.thermal:
                text += " · thermal {}".format(stats.thermal)
            pressure = (stats.thermal or "").lower()
            role = (CRIT if pressure in ("serious", "critical")
                    else WARN if pressure == "fair" else None)
            lines.append(field("requests", text, role) if role
                         else field("requests", text))
        if stats.tokens_total is not None:
            lines.append(field("tokens", "{:,} generated".format(stats.tokens_total)))
        if stats.expires_s is not None:
            lines.append(field("unload", "in {}".format(_duration(stats.expires_s))))
        recent_rows = []
        recent = self.feed.recent()[:3]
        if not recent:
            recent_rows.append("recent    -- waiting for the next completed request")
        for index, row in enumerate(recent):
            text = "{}  {}  out {}  {}  {}".format(
                time.strftime("%H:%M:%S", time.localtime(row.started or row.finished)),
                row.status,
                _tokens(row.output),
                _rate(row.tps) if row.tps else "-",
                _duration(row.duration),
            )
            text += "  first {}".format(
                _ttft(row.first_ms) if row.first_ms is not None else "--")
            text += "  cached {}".format(
                _tokens(row.cached) if row.cached else "--")
            recent_rows.append(
                ("recent    " if index == 0 else " " * 10) + text)
        return lines, recent_rows

    # -- wide eight-card grid (form C) -------------------------------------

    def _grid_cards(self, parse, args, sensors, stats, service=None):
        interval = max(0.05, float(getattr(args, "interval", 1) or 1))
        processor = (parse or {}).get("processor") or {}
        gpu = (parse or {}).get("gpu") or {}
        disk = (parse or {}).get("disk") or {}

        def pct(idle):
            return None if idle is None else max(0.0, min(100.0, (1.0 - idle) * 100.0))

        clusters = processor.get("clusters") or []
        gpu_pct = pct(gpu.get("idle_ratio"))
        cpu_pct = None
        if clusters:
            cpu_pct = sum(pct(c.get("idle_ratio")) or 0.0 for c in clusters) / len(clusters)
        cpu_w = (processor.get("cpu_energy") or 0.0) / 1000.0 / interval
        gpu_w = (processor.get("gpu_energy") or 0.0) / 1000.0 / interval
        total_w = (processor.get("combined_power") or 0.0) / 1000.0 / interval
        cards = (parse or {}).get("gpus") or gpu.get("cards") or []
        mine = self._service_cards(service) if service is not None else set()
        if mine and cards:
            cards = [c for c in cards if c.get("index") in mine] or cards
        temp = getattr(sensors, "gpu_c", None) if sensors is not None else None
        if cards:
            temps = [c.get("temp_c") for c in cards if c.get("temp_c")]
            if temps:
                temp = max(temps)
            used = sum(c.get("mem_used_mb") or 0.0 for c in cards)
            total = sum(c.get("mem_total_mb") or 0.0 for c in cards)
            if used and total:
                gpu["mem_used_mb"], gpu["mem_total_mb"] = used, total
            watts = [c.get("power_w") for c in cards if c.get("power_w")]
            if watts:
                gpu_w = sum(watts)
                total_w = gpu_w
            busy = [c.get("busy_pct") for c in cards if c.get("busy_pct") is not None]
            if busy:
                gpu_pct = sum(busy) / len(busy)
            names = {(c.get("name") or "").strip() for c in cards if c.get("name")}
            if len(cards) > 1 and names:
                gpu["name"] = "{}x {}".format(len(cards), sorted(names)[0])
        speed = None if stats is None else (stats.decode_observed
                                         if stats.decode_observed is not None else stats.decode_tps)
        read = disk.get("rbytes_per_s")
        ctx_pct = _percent(stats.ctx_used, stats.ctx_total) if stats else None

        def size(mb):
            return "-" if not mb else "{:.1f} GB".format(mb / 1024.0)

        return [
            ("speed", "Decode", "{:,.1f} tok/s".format(speed) if speed else "--",
             "now" if speed else "no decoding", "chart", speed or 0.0, None),
            ("gpu", "GPU load", "-" if gpu_pct is None else "{:.0f} %".format(gpu_pct),
             (gpu.get("name") or "-")[:24], "chart", gpu_pct, 100.0),
            ("vram", "VRAM", "{} / {}".format(size(gpu.get("mem_used_mb")),
                                              size(gpu.get("mem_total_mb"))),
             ("model " + size((stats.mem_used or 0) / 1024 ** 2)) if stats and stats.mem_used else "-",
             "chart", 100.0 * (gpu.get("mem_used_mb") or 0) / (gpu.get("mem_total_mb") or 1), 100.0),
            ("temp", "GPU temp", "-" if temp is None else "{:.0f} C".format(temp), "", "chart", temp, 100.0),
            ("power", "Power", "{:.2f} W".format(
                total_w if processor.get("cpu_energy_available", True) else gpu_w),
             ("cpu {:.1f} · gpu {:.1f}".format(cpu_w, gpu_w)
              if processor.get("cpu_energy_available", True)
              else "gpu {:.2f} W · cpu n/a".format(gpu_w)), "chart",
             total_w if processor.get("cpu_energy_available", True) else gpu_w, None),
            ("ctx", "Context", ("{} / {}".format(_tokens(stats.ctx_used), _tokens(stats.ctx_total))
                                if stats and stats.ctx_total else "--"),
             "context fill", "chart", ctx_pct, 100.0),
            ("cpu", "CPU", "-" if cpu_pct is None else "{:.0f} %".format(cpu_pct),
             "E/P clusters", "chart", cpu_pct, 100.0),
            ("disk", "Disk read", "read {}".format(_format_rate(read)),
             "write {}".format(_format_rate(disk.get("wbytes_per_s"))), "chart", read or 0.0, None),
        ]

    def _grid(self, parse, args, sensors, stats, width: int, height: int,
              service=None) -> list:
        cards = self._grid_cards(parse, args, sensors, stats, service)
        if width >= 100:
            columns, count = 4, 8
        elif width >= 56:
            columns, count = 2, 8
        else:
            return []
        # Never draw past the space under the detail card: drop cards until the
        # remaining ones fit at four rows each.
        while count and ((count + columns - 1) // columns) * 4 > height:
            count -= 2
        if count <= 0:
            return []
        chosen = cards[:count]
        card_rows = (len(chosen) + columns - 1) // columns
        card_height = max(4, min(6, height // card_rows))
        card_width = (width - 2 * (columns - 1)) // columns
        # Append one history sample per frame for the scrolling charts.
        for key, _title, _big, _sub, _kind, value, _scale in chosen:
            self._history.setdefault(key, [])
            self._history[key] = (self._history[key] + [float(value or 0.0)])[-64:]

        lines = []
        for start in range(0, len(chosen), columns):
            group = chosen[start:start + columns]
            rendered = [self._grid_card(card, card_width, card_height) for card in group]
            for row in range(card_height):
                spans = []
                for position, item in enumerate(rendered):
                    if position:
                        spans.append(("  ", LABEL))
                    spans.extend(item[row])
                lines.append(spans)
        return lines

    def _grid_card(self, card, width: int, height: int) -> list:
        key, title, big, sub, kind, value, scale = card
        inner = width - 4
        dashes = "\u2500" * max(0, width - 5 - len(title))
        top = [("\u250c\u2500 ", None), (title, BOX), (" ", None), (dashes, None), ("\u2510", None)]
        bottom = [("\u2514", None), ("\u2500" * (width - 2), None), ("\u2518", None)]
        if height <= 3:
            return [top, [("\u2502 ", None)] + _value_spans(_pad(big, inner))
                    + [(" \u2502", None)], bottom]
        out = [top,
               [("\u2502 ", None)] + _value_spans(_pad(big, inner)) + [(" \u2502", None)],
               [("\u2502 ", None)]
               + ([(_pad(sub, inner), LABEL)] if key == "disk"
                  else _value_spans(_pad(sub, inner))) + [(" \u2502", None)]]
        chart_rows = max(0, height - 4)
        history = self._history.get(key, [])
        for index in range(chart_rows):
            if kind == "chart":
                text = self._chart_line(history, inner, chart_rows, index, scale)
                out.append([("\u2502 ", None), (text, OK), (" \u2502", None)])
            else:
                level = 0.0 if not value else max(0.0, min(1.0, float(value) / 100.0))
                if index == chart_rows - 1:      # the bar sits on the last row
                    filled = int(round(level * inner))
                    text = "\u2589" * filled + " " * (inner - filled)
                else:
                    text = " " * inner
                role = CRIT if (value or 0) >= 85 else WARN if (value or 0) >= 60 else OK
                out.append([("\u2502 ", None), (text, role), (" \u2502", None)])
        out.append(bottom)
        return out

    def _chart_line(self, values: list, width: int, height: int, line: int,
                    scale=None) -> str:
        window = values[-width:]
        peak = scale if scale else (max(window) if window else 0.0) * 1.25
        peak = peak or 1.0
        text = []
        for offset in range(width):
            index = offset - (width - len(window))
            value = window[index] if 0 <= index < len(window) else 0.0
            level = max(0.0, min(1.0, value / peak)) * height * 8.0
            eighths = level - (height - 1 - line) * 8.0
            step = int(round(max(0.0, min(8.0, eighths))))
            text.append(" \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"[step])
        return "".join(text)

    def _spark(self, values: list) -> str:
        if not values:
            return ""
        levels = "▁▂▃▄▅▆▇█"
        peak = max(values) or 1.0
        return "".join(levels[min(7, int(value / peak * 7))] for value in values)

    # -- painting ----------------------------------------------------------

    def _box(self, term, top: int, width: int, height: int, title: str,
             lines: list, palette: Palette) -> list:
        inner = max(0, width - 4)
        out = [term.move(top, 0) + palette.accent() + _border(width, "") + RESET_FG,
               term.move(top, 2) + palette.style(BOX) + _fit(" " + title, inner) + RESET_FG]
        body = max(0, height - 2)
        for index in range(body):
            line = lines[index] if index < len(lines) else ""
            if isinstance(line, tuple):
                text, role = line
                painted = paint_spans([(text, role or LABEL)], palette, inner)
            elif isinstance(line, list):
                painted = paint_spans([(text, role or LABEL) for text, role in line],
                                      palette, inner)
            else:
                painted = paint_spans([(line, LABEL)], palette, inner)
            out.append(
                term.move(top + 1 + index, 0)
                + palette.accent() + "\u2502 " + painted + palette.accent()
                + " \u2502" + RESET_FG
            )
        out.append(
            term.move(top + 1 + body, 0) + palette.accent() + _bottom(width, "")
            + RESET_FG
        )
        return out
