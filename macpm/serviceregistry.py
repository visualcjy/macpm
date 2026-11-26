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
import subprocess
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
        return f"{self.host}:{self.port}" if self.port else "?"

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
    lower = joined.lower()
    if "vllm" in joined and ("serve" in argv or "vllm.entrypoints" in joined):
        return _BY_KEY["vllm"]
    if "sglang" in joined and ("launch_server" in joined or "serve" in argv):
        return _BY_KEY["sglang"]
    if name in ("llama-server", "llama_server") or "llama-server" in argv:
        return _BY_KEY["llama.cpp"]
    if name == "ollama" and "serve" in argv:
        return _BY_KEY["ollama"]
    # The desktop apps launch their own Python runtime, so the binary name says
    # nothing; the bundle path does.
    if name == "splash" or "splash.app" in lower or "/splash/" in lower:
        return _BY_KEY["splash"]
    if name == "omlx" or "omlx.app" in lower or "/.omlx/" in lower:
        return _BY_KEY["omlx"]
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
        container_source: Optional[Callable[[], list]] = None,
        gpu_source: Optional[Callable[[], list]] = None,
        validate_api: bool = False,
        api_probe: Optional[Callable[[str, int], bool]] = None,
    ):
        self.clock = clock
        self.ttl = max(0.0, float(ttl))
        self._process_source = process_source or _psutil_processes
        self._prober = prober or _port_open
        self._manual = [item for item in (manual or ()) if item]
        self._container_source = container_source
        self._gpu_source = gpu_source
        self.validate_api = validate_api
        self._api_probe = api_probe or _api_probe
        self._endpoints: dict = {}
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

    def _fill_endpoint(self, service) -> None:
        """Bind a port only when the API there reports the same model."""
        cache_key = (service.backend, service.model, service.pids)
        cached = self._endpoints.get(cache_key)
        if cached:
            service.host, service.port = cached
            return
        for pid in service.pids:
            for ip, port in _candidate_ports(pid):
                host = ip if ip != "0.0.0.0" else (netns_address(pid) or LOCAL_HOST)
                alive, identity, owner = _api_identity(host, port)
                if not alive:
                    continue
                # The API is the only authority for model and backend; the
                # GPU table only supplies pids, cards and memory.
                service.host, service.port = host, port
                if identity:
                    service.model = identity
                backend = _OWNER_BACKEND.get((owner or "").lower())
                if backend:
                    service.backend = backend
                    service.family = _BY_KEY[backend].family
                self._endpoints[cache_key] = (host, port)
                return

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
            key = ("pod", _pod_key(pid), backend.key)
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

        try:
            containers = (self._container_source() if self._container_source
                          else [])
        except Exception:
            containers = []
        for backend, port, _name, _image in containers or []:
            key = (backend, int(port))
            groups.setdefault(
                key,
                Service(
                    backend=backend,
                    family=_BY_KEY.get(backend, Backend(backend, backend, "openai", 0)).family,
                    host=LOCAL_HOST,
                    port=int(port),
                    model="-",
                    source="container",
                ),
            )

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

        for service in gpu_services(self._gpu_source):
            if not service.port:
                self._fill_endpoint(service)
            key = (("pod", _pod_key(service.pids[0]), service.backend)
                   if service.pids else (service.backend, service.model))
            groups.setdefault(key, service)

        if self.validate_api:
            groups = {
                key: service
                for key, service in groups.items()
                if service.source == "gpu" or not service.port
                or self._api_probe(service.host, service.port)
            }

        # One pod can contribute several PIDs (a launcher and its helper); once
        # the endpoint is known they are the same service.
        merged = {}
        for service in groups.values():
            key = ((service.backend, service.host, service.port) if service.port
                   else (service.backend, service.model, service.pids))
            existing = merged.get(key)
            if existing is None:
                merged[key] = service
            else:
                existing.pids = tuple(sorted(set(existing.pids) | set(service.pids)))
        groups = merged

        services = [
            service
            for service in groups.values()
            # A published container port we cannot attribute to a model or a
            # card is noise (databases, proxies): keep only known services and
            # the endpoints the user added by hand.
            if not (service.source == "container" and service.backend == "manual")
            and (not service.port or self._prober(service.host, service.port))
        ]
        services.sort(key=lambda service: (service.backend, service.port))
        return services


def _pod_key(pid: int) -> str:
    """Group processes that share a network namespace (one container = one service)."""
    try:
        return os.readlink("/proc/%d/ns/net" % pid)
    except OSError:
        return "pid:%d" % pid


def _candidate_ports(pid: int):
    """Service ports first, then the lowest non-ephemeral ones."""
    preferred = {8000, 8001, 8002, 8010, 8080, 9931, 11434, 30000, 30001}
    listeners = netns_listeners(pid)
    ordered = [entry for entry in listeners if entry[1] in preferred]
    ordered += sorted((entry for entry in listeners if entry[1] < 32768
                       and entry[1] not in preferred),
                      key=lambda entry: entry[1])
    return ordered


_OWNER_BACKEND = {
    "vllm": "vllm", "sglang": "sglang", "llamacpp": "llama.cpp",
    "llama.cpp": "llama.cpp", "ollama": "ollama", "omlx": "omlx",
    "splash": "splash",
}


def _api_identity(host: str, port: int, timeout: float = 1.2):
    """``(alive, model_id, owned_by)`` for an OpenAI-style endpoint."""
    import urllib.error
    import urllib.request

    url = "http://{}:{}/v1/models".format(host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            import json

            payload = json.loads(response.read(1 << 20).decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return error.code in (401, 403), "", ""
    except (OSError, ValueError):
        return False, "", ""
    data = payload.get("data") or []
    if not data:
        return True, "", ""
    first = data[0]
    return True, str(first.get("id") or ""), str(first.get("owned_by") or "")


def _same_model(left: str, right: str) -> bool:
    left, right = (left or "").lower(), (right or "").lower()
    return bool(left) and bool(right) and (left == right or left.startswith(right)
                                           or right.startswith(left))
def gpu_services(gpu_source=None) -> list:
    """Services the GPU table knows, one per container (network namespace)."""
    if gpu_source is None:
        try:
            from .backend import gpu_process_source

            gpu_source = gpu_process_source()
        except Exception:
            gpu_source = None
    try:
        rows = list(gpu_source()) if gpu_source else []
    except Exception:
        rows = []
    found = {}
    for row in rows:
        model = (row.get("model") or "").strip()
        if not model:
            continue
        name = (row.get("name") or "").lower()
        backend = "sglang" if "sglang" in name else "vllm"
        pid = int(row.get("pid") or 0)
        key = (_pod_key(pid), backend, model)
        service = found.get(key)
        if service is None:
            found[key] = Service(
                backend=backend,
                family=_BY_KEY[backend].family,
                host=LOCAL_HOST,
                port=0,
                model=model,
                pids=(pid,) if pid else (),
                source="gpu",
            )
        elif pid:
            service.pids = tuple(sorted(set(service.pids) | {pid}))
    return list(found.values())


PROC = "/proc"


def netns_listeners(pid: int, proc_root: str = PROC) -> list:
    """Listening ``(ip, port)`` pairs inside one process' network namespace.

    ``/proc/<pid>/net/tcp`` is that namespace's table, so a containerised
    server is visible here even though the host's ``ss`` never lists it.
    """
    pairs = []
    try:
        with open(os.path.join(proc_root, str(pid), "net", "tcp"),
                  "r", encoding="ascii", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":     # 0A = LISTEN
            continue
        try:
            address, port = fields[1].split(":")
            ip = socket.inet_ntoa(bytes.fromhex(address)[::-1])
            pairs.append((ip, int(port, 16)))
        except (ValueError, OSError):
            continue
    return pairs


def netns_address(pid: int) -> str:
    """First non-loopback IPv4 address of a process' namespace (root only)."""
    try:
        result = subprocess.run(
            ["nsenter", "-t", str(pid), "-n", "ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=3,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if "inet" in parts:
            address = parts[parts.index("inet") + 1].split("/")[0]
            if not address.startswith("127."):
                return address
    return ""


def _api_probe(host: str, port: int, timeout: float = 1.2) -> bool:
    """Does this endpoint answer an OpenAI-style API (or ask for a key)?"""
    import urllib.error
    import urllib.request

    url = "http://{}:{}/v1/models".format(host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return getattr(response, "status", 200) == 200
    except urllib.error.HTTPError as error:
        return error.code in (401, 403)
    except (OSError, ValueError):
        return False
