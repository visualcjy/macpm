"""Discover local model servers from the process table.

Nothing here talks HTTP.  A service is listed when

* a live process names a known backend (``vllm``, ``sglang``, ``llama-server``,
  ``ollama``, ``omlx``, ``splash``),
* its command line yields a port (or the backend's default one), and
* that port accepts a local TCP connection.

That keeps the model view's idle cost at "read a few command lines" instead of
polling every endpoint, and it is what the small ``unverified`` marker in the
list means: the process is real, its metrics have not been fetched yet.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import psutil

DEFAULT_SCAN_TTL = 5.0
TCP_TIMEOUT = 0.15
LOCAL_HOST = "127.0.0.1"


@dataclass(frozen=True)
class Backend:
    key: str
    label: str
    family: str
    default_port: int


# The order matters: the more specific command lines are matched first.
BACKENDS = (
    Backend("vllm", "vllm", "prometheus", 8000),
    Backend("sglang", "sglang", "prometheus", 30000),
    Backend("llama.cpp", "llama.cpp", "llamacpp", 9931),
    Backend("ollama", "ollama", "openai", 11434),
    Backend("omlx", "omlx", "native", 8000),
    Backend("splash", "splash", "native", 8000),
    Backend("manual", "manual", "openai", 8000),
)

_BY_KEY = {backend.key: backend for backend in BACKENDS}


@dataclass
class Service:
    """One local model server, as discovered without HTTP."""

    backend: str
    family: str
    host: str
    port: int
    model: str = "-"
    pids: tuple = ()
    source: str = "process"

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return _BY_KEY.get(self.backend, Backend(self.backend, self.backend, "", 0)).label

    @property
    def identity(self) -> tuple:
        """Stable across polls and process restarts, unlike ``pid``."""
        return (self.backend, self.port)


def _argv0(argv: Iterable[str]) -> str:
    for token in argv:
        return os.path.basename(token or "")
    return ""


def _flag(argv: list, *names: str) -> str:
    """Value of ``--name value`` or ``--name=value`` (first match)."""
    for index, token in enumerate(argv):
        for name in names:
            if token == name and index + 1 < len(argv):
                return argv[index + 1]
            prefix = name + "="
            if token.startswith(prefix):
                return token[len(prefix):]
    return ""


def _after(argv: list, word: str) -> str:
    if word not in argv:
        return ""
    index = argv.index(word)
    if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
        return argv[index + 1]
    return ""


def _basename_model(path: str) -> str:
    base = (path or "").rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".gguf"):
        base = base[: -len(".gguf")]
    return base or "-"


def identify(argv: list) -> Optional[Backend]:
    """Backend named by one command line, or ``None``."""
    if not argv:
        return None
    name = _argv0(argv)
    joined = " ".join(argv)
    if "vllm" in joined and ("serve" in argv or "vllm.entrypoints" in joined):
        return _BY_KEY["vllm"]
    if "sglang" in joined and ("launch_server" in joined or "serve" in argv):
        return _BY_KEY["sglang"]
    if name in ("llama-server", "llama_server") or "llama-server" in argv:
        return _BY_KEY["llama.cpp"]
    if name == "ollama" and "serve" in argv:
        return _BY_KEY["ollama"]
    if name == "omlx" and ("serve" in argv or "start" in argv):
        return _BY_KEY["omlx"]
    if name == "splash" and "serve" in argv:
        return _BY_KEY["splash"]
    return None


def parse_port(argv: list, backend: Backend) -> int:
    raw = _flag(argv, "--port")
    if raw.isdigit():
        return int(raw)
    return backend.default_port


def parse_model(argv: list, backend: Backend) -> str:
    served = _flag(argv, "--served-model-name")
    if served:
        return served.split(",")[0]
    if backend.key == "vllm":
        return _basename_model(_after(argv, "serve") or _flag(argv, "--model"))
    if backend.key == "sglang":
        return _basename_model(_flag(argv, "--model-path") or _flag(argv, "--model"))
    if backend.key == "llama.cpp":
        return _basename_model(_flag(argv, "-m", "--model"))
    if backend.key == "splash":
        return _flag(argv, "--model") or "-"
    return "-"


def _port_open(host: str, port: int, timeout: float = TCP_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _psutil_processes() -> list:
    """``(pid, argv)`` for every process whose command line is readable."""
    rows = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            argv = proc.info.get("cmdline") or []
        except (psutil.Error, OSError):  # pragma: no cover - defensive
            continue
        if argv:
            rows.append((proc.info["pid"], list(argv)))
    return rows


def parse_endpoint(text: str) -> Optional[tuple]:
    """``http://host:port/`` / ``host:port`` / bare port → ``(host, port)``."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.isdigit():
        return LOCAL_HOST, int(text)
    if ":" not in text:
        return None
    host, _, port = text.rpartition(":")
    if not port.isdigit():
        return None
    return host or LOCAL_HOST, int(port)


class ServiceRegistry:
    """Cached, HTTP-free service discovery.

    ``process_source`` returns ``[(pid, argv), ...]``; ``prober`` decides
    whether ``host:port`` is listening.  Both are injectable for tests.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = DEFAULT_SCAN_TTL,
        process_source: Optional[Callable[[], list]] = None,
        prober: Optional[Callable[[str, int], bool]] = None,
        manual: Iterable[str] = (),
    ):
        self.clock = clock
        self.ttl = max(0.0, float(ttl))
        self._process_source = process_source or _psutil_processes
        self._prober = prober or _port_open
        self._manual = [item for item in (manual or ()) if item]
        self._services: list = []
        self._cached_at = -1e9
        self.error = ""

    # -- public ------------------------------------------------------------

    def discover(self, force: bool = False) -> list:
        now = self.clock()
        if not force and self._services and now - self._cached_at < self.ttl:
            return list(self._services)
        self._services = self._scan()
        self._cached_at = now
        return list(self._services)

    # -- internals ---------------------------------------------------------

    def _scan(self) -> list:
        groups: dict = {}
        try:
            rows = self._process_source() or []
        except Exception as error:  # psutil can fail on a locked-down host
            self.error = str(error)
            rows = []
        else:
            self.error = ""

        for pid, argv in rows:
            backend = identify(argv)
            if backend is None:
                continue
            port = parse_port(argv, backend)
            host = _flag(argv, "--host") or LOCAL_HOST
            if host in ("0.0.0.0", "::", ""):
                host = LOCAL_HOST
            key = (backend.key, port)
            service = groups.get(key)
            model = parse_model(argv, backend)
            if service is None:
                groups[key] = Service(
                    backend=backend.key,
                    family=backend.family,
                    host=host,
                    port=port,
                    model=model,
                    pids=(pid,),
                )
            else:
                service.pids = tuple(sorted(set(service.pids) | {pid}))
                if service.model == "-" and model != "-":
                    service.model = model

        for raw in self._manual:
            parsed = parse_endpoint(raw)
            if parsed is None:
                continue
            host, port = parsed
            key = ("manual", port)
            groups.setdefault(
                key,
                Service(
                    backend="manual",
                    family="openai",
                    host=host,
                    port=port,
                    model="-",
                    source="manual",
                ),
            )

        services = [
            service
            for service in groups.values()
            if self._prober(service.host, service.port)
        ]
        services.sort(key=lambda service: (service.backend, service.port))
        return services
