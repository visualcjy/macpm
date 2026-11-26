"""Read-only metrics for the selected model service.

The feed polls exactly one endpoint — the service the view has selected — with
a short deadline and a hard response cap, keeping the "no idle traffic" rule
from the design: nothing is fetched for rows the user has not looked at.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .modellog import (
    DEFAULT_SPLASH_LOG, CommandTail, HttpTail, JsonLogTail, LogTail,
    SplashLogReader, docker_log_path, find_pod_log, kube_log_url,
    parse_vllm_stats,
)
from .serviceregistry import Service

TIMEOUT = 1.5
MAX_BYTES = 1 << 20          # 1 MiB, the same cap netinfo uses
DEFAULT_TTL = 1.0
STALE_AFTER = 3.0

_KEYS = {
    "splash": "SPLASH_API_KEY",
    "omlx": "OMLX_API_KEY",
    "llama.cpp": "LLAMACPP_API_KEY",
}


@dataclass
class ModelStats:
    """What the detail panel shows for one polled service."""

    state: str = "unverified"          # live | stale | auth | error | no-telemetry
    model: str = ""
    decode_tps: Optional[float] = None
    prefill_tps: Optional[float] = None
    active: Optional[int] = None
    waiting: Optional[int] = None
    phases: dict = field(default_factory=dict)
    ttft_p50_ms: Optional[float] = None
    ttft_p95_ms: Optional[float] = None
    slots_used: Optional[int] = None
    slots_total: Optional[int] = None
    tokens_total: Optional[int] = None
    expires_s: Optional[float] = None
    ctx_used: Optional[int] = None
    ctx_total: Optional[int] = None
    kv_used: Optional[float] = None     # bytes
    kv_total: Optional[float] = None    # bytes
    kv_pct: Optional[float] = None      # 0-100 when the server reports a ratio
    decode_observed: Optional[float] = None   # tokens/s seen between polls
    prefix_hit: Optional[float] = None        # vLLM prefix cache hit rate
    gen_tokens: Optional[int] = None    # counters kept for client-side rates
    prompt_tokens: Optional[int] = None
    mem_used: Optional[float] = None    # bytes
    mem_peak: Optional[float] = None
    requests: Optional[int] = None
    failed: Optional[int] = None
    thermal: str = ""
    note: str = ""
    at: float = 0.0
    # Live request rows used to reconstruct completions; not rendered directly.
    active_rows: list = field(default_factory=list)


@dataclass
class RequestRow:
    """One completion the feed reconstructed from consecutive polls."""

    rid: str
    finished: float
    status: str = "done"
    prompt: Optional[int] = None
    output: Optional[int] = None
    tps: Optional[float] = None
    duration: float = 0.0
    first_ms: Optional[float] = None
    cached: Optional[int] = None
    started: Optional[float] = None


def _http_json(host: str, port: int, path: str, key: str = "") -> dict:
    url = f"http://{host}:{port}{path}"
    headers = {"Accept": "application/json", "User-Agent": "macpm"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if getattr(response, "status", 200) != 200:
            raise ValueError(f"HTTP {getattr(response, 'status', 0)}")
        payload = response.read(MAX_BYTES)
    return json.loads(payload.decode("utf-8", "replace"))


def _http_text(host: str, port: int, path: str, key: str = "") -> str:
    url = f"http://{host}:{port}{path}"
    headers = {"Accept": "text/plain", "User-Agent": "macpm"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if getattr(response, "status", 200) != 200:
            raise ValueError(f"HTTP {getattr(response, 'status', 0)}")
        return response.read(MAX_BYTES).decode("utf-8", "replace")


def parse_prometheus(text: str) -> dict:
    """Metric name → last value; labels and timestamps are ignored."""
    values = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        if not name:
            continue
        base = name.split("{", 1)[0]
        token = rest.split(" ")[0] if rest else ""
        try:
            values[base] = float(token)
        except ValueError:
            continue
    return values


def _basename(path: str) -> str:
    base = (path or "").rstrip("/").rsplit("/", 1)[-1]
    return base[: -len(".gguf")] if base.endswith(".gguf") else (base or "-")


def _seconds_until(iso: str) -> Optional[float]:
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def _key_for(backend: str) -> str:
    name = _KEYS.get(backend)
    return (os.environ.get(name or "", "") or "").strip()


def parse_splash(payload: dict, at: float = 0.0) -> ModelStats:
    """Splash ``GET /status`` → one card's worth of numbers."""
    stats = ModelStats(state="live", at=at)
    metrics = payload.get("metrics") or {}
    stats.decode_tps = _number(metrics.get("decode_tokens_per_second"))
    stats.prefill_tps = _number(metrics.get("prefill_tokens_per_second"))
    ttft = metrics.get("ttft_ms") or {}
    stats.ttft_p50_ms = _number(ttft.get("p50"))
    stats.ttft_p95_ms = _number(ttft.get("p95"))

    active = payload.get("active_requests") or []
    stats.active = len(active)
    for row in active:
        phase = str(row.get("phase") or "").strip()
        if phase:
            stats.phases[phase] = stats.phases.get(phase, 0) + 1
    stats.ctx_used = sum(
        int(row.get("prompt_tokens") or 0) + int(row.get("generated_tokens") or 0)
        for row in active
    ) or None
    stats.active_rows = [
        {
            "rid": str(row.get("id") or row.get("request_id") or ""),
            "prompt": _int(row.get("prompt_tokens")),
            "output": _int(row.get("generated_tokens")),
            "first": _number((row.get("request_latency") or {}).get("ttft_ms")
                             if isinstance(row.get("request_latency"), dict)
                             else row.get("ttft_ms")),
            "cached": _int(row.get("cached_tokens")),
        }
        for row in active
        if row.get("id") or row.get("request_id")
    ]
    stats.ctx_total = _int(payload.get("maximum_context_tokens")) or _int(
        (payload.get("capabilities") or {}).get("max_context_tokens")
    )

    kv = payload.get("kv") or {}
    budget = (payload.get("memory_plan") or {}).get("budget") or {}
    stats.kv_used = _number(kv.get("allocated_bytes"))
    stats.kv_total = (
        _number(budget.get("hard_budget_bytes"))
        or _number((payload.get("memory_plan") or {}).get("hard_budget_bytes"))
    )
    memory = payload.get("memory_actual") or {}
    stats.mem_used = _number(memory.get("current_bytes"))
    stats.mem_peak = _number(memory.get("peak_bytes"))

    requests = payload.get("requests") or {}
    stats.requests = _int(requests.get("completed"))
    stats.failed = _int(requests.get("failed"))
    stats.thermal = str(payload.get("thermal_state") or "")
    model = payload.get("model") or payload.get("model_name") or ""
    stats.model = str(model)
    if not payload.get("ready", True):
        stats.state = "stale"
        stats.note = "engine not ready"
    return stats


def _current_omlx_model(active: dict) -> dict:
    """The model the dashboard would call current: busy, else most recent."""
    models = [m for m in (active.get("models") or []) if m.get("loaded", True)]
    if not models:
        return {}
    for model in models:
        if (model.get("active_requests") or 0) + (model.get("waiting_requests") or 0) > 0:
            return model
    busy = [m for m in models if m.get("idle_seconds") is not None]
    if busy:
        return min(busy, key=lambda m: float(m.get("idle_seconds") or 0.0))
    return models[0]


def parse_omlx(payload: dict, at: float = 0.0) -> ModelStats:
    """oMLX ``GET /admin/api/activity`` → one card's worth of numbers."""
    active = payload.get("active_models") or payload
    model = _current_omlx_model(active)
    stats = ModelStats(state="live", at=at)
    if not model:
        stats.state = "no-telemetry"
        stats.note = "no loaded model"
        return stats

    stats.model = str(model.get("id") or model.get("model") or "")
    stats.active = _int(model.get("active_requests")) or 0
    stats.waiting = _int(model.get("waiting_requests")) or 0
    generating = model.get("generating") or []
    prefilling = model.get("prefilling") or []
    speeds = [_number(row.get("tokens_per_second")) for row in generating]
    speeds = [speed for speed in speeds if speed]
    if speeds:
        stats.decode_tps = sum(speeds)
    prefill = [_number(row.get("speed")) for row in prefilling]
    prefill = [speed for speed in prefill if speed]
    if prefill:
        stats.prefill_tps = sum(prefill)
    if generating:
        stats.phases["decode"] = len(generating)
    if prefilling:
        stats.phases["prefill"] = len(prefilling)
    stats.ctx_used = sum(
        int(row.get("prompt_tokens") or 0) + int(row.get("generated_tokens") or 0)
        for row in generating + prefilling
    ) or None
    stats.active_rows = [
        {
            "rid": str(row.get("request_id") or ""),
            "prompt": _int(row.get("prompt_tokens")),
            "output": _int(row.get("generated_tokens")),
        }
        for row in generating + prefilling
        if row.get("request_id")
    ]
    stats.mem_used = _number(active.get("model_memory_used"))
    stats.mem_peak = _number(active.get("model_memory_max"))
    stats.requests = _int(active.get("total_active_requests"))
    return stats


def parse_llamacpp(props: dict, metrics: dict) -> ModelStats:
    """llama.cpp ``/props`` plus the parsed ``/metrics`` counters."""
    stats = ModelStats(state="live")
    generation = props.get("default_generation_settings") or {}
    slots = _int(props.get("total_slots"))
    stats.slots_total = slots
    stats.ctx_total = _int(generation.get("n_ctx"))
    stats.model = _basename(props.get("model_path") or "")

    stats.decode_tps = _number(metrics.get("llamacpp:predicted_tokens_seconds"))
    stats.prefill_tps = _number(metrics.get("llamacpp:prompt_tokens_seconds"))
    stats.active = _int(metrics.get("llamacpp:requests_processing"))
    stats.waiting = _int(metrics.get("llamacpp:requests_deferred"))
    stats.slots_used = stats.active
    stats.tokens_total = _int(metrics.get("llamacpp:tokens_predicted_total"))
    stats.ctx_used = _int(metrics.get("llamacpp:n_tokens_max"))
    return stats


def parse_vllm(metrics: dict, model: str = "") -> ModelStats:
    """vLLM ``/metrics``: gauges for concurrency/KV, counters for tokens."""
    stats = ModelStats(state="live", model=model)
    stats.active = _int(metrics.get("vllm:num_requests_running"))
    stats.waiting = _int(metrics.get("vllm:num_requests_waiting"))
    kv = _number(metrics.get("vllm:kv_cache_usage_perc"))
    if kv is not None:
        stats.kv_pct = kv * 100.0 if kv <= 1.0 else kv
    stats.gen_tokens = _int(metrics.get("vllm:generation_tokens_total")
                            if "vllm:generation_tokens_total" in metrics
                            else metrics.get("vllm:generation_tokens"))
    stats.prompt_tokens = _int(metrics.get("vllm:prompt_tokens_total")
                               if "vllm:prompt_tokens_total" in metrics
                               else metrics.get("vllm:prompt_tokens"))
    stats.requests = _int(metrics.get("vllm:request_success_total"))
    total = metrics.get("vllm:time_to_first_token_seconds_sum")
    count = metrics.get("vllm:time_to_first_token_seconds_count")
    if total is not None and count:
        avg = total / count * 1000.0
        stats.ttft_p50_ms = avg
    return stats


def parse_sglang(metrics: dict, info: dict = None) -> ModelStats:
    """SGLang ``/metrics`` plus ``/get_server_info``."""
    stats = ModelStats(state="live")
    info = info or {}
    stats.model = _basename(info.get("model_path") or "")
    stats.active = _int(metrics.get("sglang:num_running_reqs"))
    stats.waiting = _int(metrics.get("sglang:num_queue_reqs"))
    stats.decode_tps = _number(metrics.get("sglang:gen_throughput"))
    usage = _number(metrics.get("sglang:token_usage"))
    if usage is not None:
        stats.kv_pct = usage * 100.0 if usage <= 1.0 else usage
    stats.gen_tokens = _int(metrics.get("sglang:generation_tokens_total"))
    stats.prompt_tokens = _int(metrics.get("sglang:prompt_tokens_total"))
    total = metrics.get("sglang:time_to_first_token_seconds_sum")
    count = metrics.get("sglang:time_to_first_token_seconds_count")
    if total is not None and count:
        stats.ttft_p50_ms = total / count * 1000.0
    return stats


def parse_ollama(payload: dict, at: float = 0.0) -> ModelStats:
    """ollama ``/api/ps``: load state only, no rates (a C-tier backend)."""
    models = payload.get("models") or []
    stats = ModelStats(state="live", at=at)
    if not models:
        stats.state = "live"
        stats.model = "-"
        stats.note = "no model loaded"
        return stats
    first = models[0]
    stats.model = str(first.get("name") or first.get("model") or "-")
    size = _number(first.get("size"))
    vram = _number(first.get("size_vram"))
    stats.mem_used = vram if vram else size
    stats.mem_peak = size
    stats.expires_s = _seconds_until(first.get("expires_at") or "")
    if len(models) > 1:
        stats.note = "{} models loaded · showing {}".format(len(models), stats.model)
    return stats


def _number(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ModelFeed:
    """One background poll for the currently selected service."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = DEFAULT_TTL,
        fetcher: Optional[Callable[[Service, str], ModelStats]] = None,
    ):
        self.clock = clock
        self.ttl = max(0.2, float(ttl))
        self._fetcher = fetcher or fetch_stats
        self._lock = threading.Lock()
        self._service: Optional[Service] = None
        self._stats: Optional[ModelStats] = None
        self._next = 0.0
        self._thread: Optional[threading.Thread] = None
        self._at = 0.0
        self._recent: list = []
        self._active: dict = {}
        self._counters: dict = {}
        self._decode_state: dict = {}
        self._logs: dict = {}

    # -- selection ---------------------------------------------------------

    def select(self, service: Optional[Service]) -> None:
        with self._lock:
            identity = service.identity if service is not None else None
            current = self._service.identity if self._service is not None else None
            if identity == current:
                return
            self._service = service
            self._stats = None
            self._next = 0.0
            self._recent = []
            self._active = {}
            self._counters = {}
            self._decode_state = {}
            self._logs = {}

    def snapshot(self) -> Optional[ModelStats]:
        with self._lock:
            return self._stats

    def recent(self) -> list:
        with self._lock:
            return list(self._recent)

    def _make_log_source(self, service: Service):
        """``MACPM_<BACKEND>_LOG`` (file) or ``_LOG_CMD`` / ``_LOG_URL``."""
        prefix = {"splash": "SPLASH", "vllm": "VLLM",
                  "sglang": "SGLANG", "ollama": "OLLAMA",
                  "llama.cpp": "LLAMACPP"}.get(service.backend)
        if prefix is None:
            return None
        if service.backend == "splash":
            return SplashLogReader(os.environ.get("SPLASH_LOG", DEFAULT_SPLASH_LOG))
        container = os.environ.get(prefix + "_DOCKER")
        if container:
            path = docker_log_path(container)
            return JsonLogTail(path) if path else None
        cmd = os.environ.get(prefix + "_LOG_CMD")
        if cmd:
            import shlex
            return CommandTail(shlex.split(cmd))
        url = os.environ.get(prefix + "_LOG_URL")
        if url:
            return HttpTail(url)
        pod = os.environ.get(prefix + "_POD")
        if pod:
            node_log = find_pod_log(pod)
            if node_log:
                return LogTail(node_log)
        kube_pod = os.environ.get(prefix + "_K8S_POD")
        if kube_pod:
            url, headers = kube_log_url(
                kube_pod,
                os.environ.get(prefix + "_K8S_NAMESPACE", "default"),
            )
            if url:
                return HttpTail(url, headers=headers)
        path = os.environ.get(prefix + "_LOG")
        if path:
            return LogTail(path)
        return None

    def _read_log(self, service: Service) -> bool:
        """Pull real per-request rows from the server's own log."""
        if service.backend != "splash":
            return False
        reader = self._logs.get(service.identity)
        if reader is None:
            reader = self._make_log_source(service)
            if reader is None:
                return False
            self._logs[service.identity] = reader
        if service.backend == "vllm":
            latest = None
            for line in reader.read():
                parsed = parse_vllm_stats(line)
                if parsed:
                    latest = parsed
            if latest is None:
                return False
            for key, value in latest.items():
                setattr(stats, key, value)
            return True
        if not getattr(reader, "tail", None) or not reader.tail.exists():
            return False
        got = False
        for entry in reader.poll():
            self._recent.insert(0, RequestRow(
                rid="log",
                finished=entry["finished"],
                started=entry.get("started"),
                prompt=entry.get("prompt"),
                cached=entry.get("cached"),
                output=entry.get("output"),
                first_ms=entry.get("first_ms"),
                tps=entry.get("tps"),
                duration=entry.get("duration") or 0.0,
            ))
            got = True
        if got:
            del self._recent[8:]
        return got

    def _observe_decode(self, service: Service, stats: ModelStats) -> None:
        """Tokens/s actually produced between two polls, like the dashboard."""
        outputs = sum(int(row.get("output") or 0) for row in stats.active_rows)
        now = self.clock()
        previous = self._decode_state.get(service.identity)
        self._decode_state[service.identity] = (outputs, now)
        if previous is None:
            return
        tokens0, at0 = previous
        elapsed = now - at0
        if elapsed <= 0.2:
            return
        stats.decode_observed = max(0.0, outputs - tokens0) / elapsed

    def _derive_rates(self, service: Service, stats: ModelStats) -> None:
        """vLLM/SGLang only expose token counters: diff them into tok/s."""
        if stats.gen_tokens is None and stats.prompt_tokens is None:
            return
        now = self.clock()
        previous = self._counters.get(service.identity)
        self._counters[service.identity] = (
            stats.gen_tokens or 0, stats.prompt_tokens or 0, now)
        if previous is None:
            stats.decode_tps = stats.decode_tps      # first sample: no rate yet
            stats.note = stats.note or "rate needs two samples"
            return
        gen0, prompt0, at0 = previous
        elapsed = max(0.2, now - at0)
        if stats.gen_tokens is not None:
            stats.decode_tps = max(0.0, stats.gen_tokens - gen0) / elapsed
        if stats.prompt_tokens is not None:
            stats.prefill_tps = max(0.0, stats.prompt_tokens - prompt0) / elapsed

    def _track(self, stats: ModelStats) -> None:
        """Turn disappeared request ids into completed rows (client-side)."""
        now = self.clock()
        seen = {}
        for row in stats.active_rows:
            rid = row.get("rid") or ""
            if not rid:
                continue
            seen[rid] = row
            start, prompt, _, first, cached = self._active.get(
                rid, (now, row.get("prompt"), 0, row.get("first"), row.get("cached")))
            self._active[rid] = (
                start,
                prompt if prompt is not None else row.get("prompt"),
                row.get("output") or 0,
                first if first is not None else row.get("first"),
                cached if cached is not None else row.get("cached"),
            )
        for rid, (start, prompt, output, first, cached) in list(self._active.items()):
            if rid in seen:
                continue
            duration = max(0.0, now - start)
            tps = (output / duration) if duration > 0 and output else None
            self._recent.insert(0, RequestRow(
                rid=rid,
                # Wall clock for display; ``now`` is monotonic and only used
                # for the duration maths above.
                finished=time.time(),
                prompt=prompt,
                output=output,
                tps=tps,
                duration=duration,
                first_ms=first,
                cached=cached,
            ))
            del self._active[rid]
        del self._recent[8:]

    # -- polling -----------------------------------------------------------

    def maybe_refresh(self) -> None:
        with self._lock:
            if self._service is None:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            if self.clock() < self._next:
                return
            service = self._service
            self._next = self.clock() + self.ttl
            thread = threading.Thread(
                target=self._load, args=(service,), daemon=True
            )
            self._thread = thread
        thread.start()

    def _load(self, service: Service) -> None:
        try:
            stats = self._fetcher(service, _key_for(service.backend))
        except urllib.error.HTTPError as error:
            stats = ModelStats(
                state="auth" if error.code in (401, 403) else "error",
                note=f"HTTP {error.code}",
            )
        except Exception as error:  # timeouts, bad JSON, refused connections
            stats = ModelStats(state="error", note=str(error)[:80])
        stats.at = self.clock()
        with self._lock:
            # Drop the result if the user moved on to another service.
            if self._service is not None and self._service.identity == service.identity:
                self._observe_decode(service, stats)
                self._derive_rates(service, stats)
                self._stats = stats
                if not self._read_log(service):
                    self._track(stats)


def _fetch_splash(service: Service, key: str) -> ModelStats:
    return parse_splash(_http_json(service.host, service.port, "/status", key))


def _fetch_omlx(service: Service, key: str) -> ModelStats:
    return parse_omlx(
        _http_json(service.host, service.port, "/admin/api/activity", key)
    )


def _fetch_llamacpp(service: Service, key: str) -> ModelStats:
    props = _http_json(service.host, service.port, "/props", key)
    try:
        metrics = parse_prometheus(
            _http_text(service.host, service.port, "/metrics", key)
        )
        stats = parse_llamacpp(props, metrics)
    except (urllib.error.HTTPError, ValueError):
        # ``--metrics`` is off by default: the model and slot count still work.
        stats = parse_llamacpp(props, {})
        stats.note = "metrics disabled · start llama-server with --metrics"
    return stats


def _fetch_ollama(service: Service, key: str) -> ModelStats:
    return parse_ollama(_http_json(service.host, service.port, "/api/ps", key))


def _model_info(host: str, port: int, key: str) -> dict:
    try:
        payload = _http_json(host, port, "/v1/models", key)
        data = payload.get("data") or []
        if data:
            return data[0]
    except Exception:
        pass
    return {}


def _fetch_vllm(service: Service, key: str) -> ModelStats:
    info = _model_info(service.host, service.port, key)
    try:
        metrics = parse_prometheus(
            _http_text(service.host, service.port, "/metrics", key))
    except urllib.error.HTTPError as error:
        if error.code not in (404, 501):
            raise
        # Some deployments serve the API without the metrics endpoint.
        stats = ModelStats(state="live", model=str(info.get("id") or ""))
        stats.ctx_total = _int(info.get("max_model_len"))
        stats.note = "metrics endpoint unavailable · showing the model only"
        return stats
    stats = parse_vllm(metrics, str(info.get("id") or ""))
    stats.ctx_total = _int(info.get("max_model_len"))
    return stats


def _fetch_sglang(service: Service, key: str) -> ModelStats:
    try:
        metrics = parse_prometheus(
            _http_text(service.host, service.port, "/metrics", key))
    except urllib.error.HTTPError as error:
        if error.code not in (404, 501):
            raise
        metrics = {}
    try:
        info = _http_json(service.host, service.port, "/get_server_info", key)
    except Exception:
        info = {}
    stats = parse_sglang(metrics, info)
    stats.ctx_total = _int(info.get("context_length"))
    return stats


_FETCHERS = {
    "vllm": _fetch_vllm,
    "sglang": _fetch_sglang,
    "splash": _fetch_splash,
    "omlx": _fetch_omlx,
    "llama.cpp": _fetch_llamacpp,
    "ollama": _fetch_ollama,
}


def fetch_stats(service: Service, key: str = "") -> ModelStats:
    """Fetch and parse one service; raises for transport errors."""
    fetcher = _FETCHERS.get(service.backend)
    if fetcher is None:
        return ModelStats(state="no-telemetry", note="adapter not implemented")
    stats = fetcher(service, key)
    if not stats.model:
        stats.model = service.model
    return stats
