"""Local model-service view (key 6).

Discovery (:mod:`macpm.serviceregistry`) is HTTP-free; the metrics for the
selected row come from :mod:`macpm.modelfeed`, which polls only that endpoint.
A row that has not been looked at stays ``unverified``.
"""

from __future__ import annotations

import time
from typing import Optional

from blessed import Terminal

from .modelfeed import ModelFeed, ModelStats
from .serviceregistry import Service, ServiceRegistry
from .theme import (
    ADDRESS,
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
_STATE_WIDTH = 14


def _service_sort_key(sort: str):
    if sort == "model":
        return lambda service: (service.model.lower(), service.port)
    if sort == "backend":
        return lambda service: (service.backend, service.port)
    return lambda service: (service.port, service.backend)


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


def _seconds(ms: Optional[float]) -> str:
    return "-" if ms is None else "{:.2f}s".format(ms / 1000.0)


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600:
        return "{}h {:02d}m".format(seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "{}m {:02d}s".format(seconds // 60, seconds % 60)
    return "{}s".format(seconds)


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

    def display(self, parse=None, args=None) -> None:
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
        out: list = []

        if not ordered:
            out += self._box(term, 0, width, 6, " MODEL SERVICES ",
                             self._empty_lines(inner), palette)
        elif len(ordered) == 1:
            lines = self._detail_lines(ordered[0], inner, stats)
            out += self._box(term, 0, width, min(height - 2, len(lines) + 2),
                             " MODEL · {} ".format(ordered[0].label), lines, palette)
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
        state = self._state_text(stats) if selected else "unverified"
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
        def field(name: str, value: str, role: str = VALUE) -> tuple:
            return (_pad(name, 10) + _fit(value, inner - 10), role)

        model = (stats.model if stats and stats.model else service.model) or "-"
        lines = [
            field("model", model),
            field("backend", "{} · {} family".format(service.label, service.family)),
            field("endpoint", service.endpoint, ADDRESS),
            field("pids", _pids_text(service)),
            field("state", self._state_detail(stats), MUTED),
        ]
        if stats is not None and stats.state == "live":
            lines += self._metric_lines(stats, field, service)
        if stats is not None and stats.note:
            lines.append(field("note", stats.note, HINT))
        return lines

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

    def _metric_lines(self, stats: ModelStats, field, service: Service) -> list:
        lines = []
        rates = "decode {}   prefill {}".format(_rate(stats.decode_tps), _rate(stats.prefill_tps))
        lines.append(field("rates", rates))
        if stats.slots_total:
            used = stats.slots_used if stats.slots_used is not None else stats.active
            text = "{} / {}".format("-" if used is None else used, stats.slots_total)
            if stats.waiting:
                text += "   queued {}".format(stats.waiting)
            lines.append(field("slots", text))
        if stats.ttft_p50_ms is not None:
            ttft = "{} p50 · {} p95".format(
                _seconds(stats.ttft_p50_ms), _seconds(stats.ttft_p95_ms)
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
        if stats.requests is not None:
            text = "{} done".format(stats.requests)
            if stats.failed:
                text += " · {} failed".format(stats.failed)
            if stats.thermal:
                text += " · thermal {}".format(stats.thermal)
            lines.append(field("requests", text))
        if stats.tokens_total is not None:
            lines.append(field("tokens", "{:,} generated".format(stats.tokens_total)))
        if stats.expires_s is not None:
            lines.append(field("unload", "in {}".format(_duration(stats.expires_s))))
        for index, row in enumerate(self.feed.recent()[:3]):
            text = "{}  {}  out {}  {}  {}".format(
                time.strftime("%H:%M:%S", time.localtime(row.finished)),
                row.status,
                _tokens(row.output),
                _rate(row.tps) if row.tps else "-",
                _duration(row.duration),
            )
            lines.append(field("recent" if index == 0 else "", text, MUTED))
        return lines

    # -- painting ----------------------------------------------------------

    def _box(self, term, top: int, width: int, height: int, title: str,
             lines: list, palette: Palette) -> list:
        inner = max(0, width - 4)
        out = [term.move(top, 0) + palette.style(TITLE) + _border(width, title) + RESET_FG]
        body = max(0, height - 2)
        for index in range(body):
            line = lines[index] if index < len(lines) else ""
            if isinstance(line, tuple):
                text, role = line
                painted = paint_spans([(text, role or LABEL)], palette, inner)
            else:
                painted = paint_spans([(line, LABEL)], palette, inner)
            out.append(
                term.move(top + 1 + index, 0)
                + palette.style(LABEL) + "\u2502 " + painted + palette.style(LABEL)
                + " \u2502" + RESET_FG
            )
        out.append(
            term.move(top + 1 + body, 0) + palette.style(LABEL) + _bottom(width, "")
            + RESET_FG
        )
        return out
