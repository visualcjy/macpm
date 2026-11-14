"""Recover the served model name from a process' parent chain.

vLLM and sglang workers are short-lived children of an EngineCore/scheduler,
which is a child of the launcher that carries the flags (``vllm serve <path>
--served-model-name ...``, ``sglang.launch_server --model-path ...``).  Walking
up a few ``/proc/<pid>`` links finds that launcher.

Only ``/proc/<pid>/cmdline`` is read: model directories can live on a stalled
network mount inside the container and opening ``config.json`` there blocks
indefinitely (observed on the field nodes), so this module never touches them.
"""

from __future__ import annotations

import os
import shlex
import time

# Processes that own the pod/host rather than a model server; stop before them.
_STOP = {
    "containerd-shim",
    "containerd-shim-runc-v2",
    "runc",
    "systemd",
    "init",
    "tini",
    "docker-init",
    "supervisord",
}

_MODEL_FLAGS = ("--model", "--model-path", "--model-id")
_CACHE_TTL = 10.0
_cache: dict = {}
_proc_root = "/proc"


def set_proc_root(path: str) -> None:
    """Point the resolver at a fixture tree (``/proc`` by default)."""
    global _proc_root
    _proc_root = path or "/proc"
    _cache.clear()


def reset_model_cache() -> None:
    _cache.clear()


def _read(pid: int, name: str) -> bytes:
    try:
        with open(os.path.join(_proc_root, str(pid), name), "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def _cmdline(pid: int) -> str:
    raw = _read(pid, "cmdline")
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _comm(pid: int) -> str:
    return _read(pid, "comm").decode("utf-8", "replace").strip()


def _ppid(pid: int) -> int:
    raw = _read(pid, "stat").decode("utf-8", "replace")
    # "pid (comm with spaces) state ppid ..." — cut after the last ")".
    tail = raw[raw.rfind(")") + 1:].split()
    try:
        return int(tail[1])
    except (IndexError, ValueError):
        return 0


def _basename(path: str) -> str:
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".gguf"):
        base = base[: -len(".gguf")]
    return base


def _scan(argv: list) -> tuple:
    served = path = ""
    for index, token in enumerate(argv):
        following = argv[index + 1] if index + 1 < len(argv) else ""
        if token == "--served-model-name" and following:
            served = following.split(",")[0]
        elif token in _MODEL_FLAGS and following:
            path = following
        elif token == "-m" and following.endswith(".gguf"):
            path = following
    return served, path


def extract(cmdline: str) -> str:
    """Model name from one command line, or ``""``."""
    attempts = []
    try:
        attempts.append(shlex.split(cmdline))
    except ValueError:
        attempts.append(cmdline.split())
    # k8s "bash -c '<full command>'" hides the flags inside a single argument.
    joined = " ".join(attempts[0]).replace("'", " ").replace('"', " ")
    attempts.append(joined.split())

    for argv in attempts:
        served, path = _scan(argv)
        if served:
            return served
        if path:
            return _basename(path)
        if "serve" in argv:
            index = argv.index("serve")
            if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                return _basename(argv[index + 1])
    return ""


def resolve(pid: int, limit: int = 10) -> str:
    """Walk up from ``pid`` until the launcher's model flags are found."""
    key = (str(_proc_root), pid)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    name = ""
    current = pid
    for _ in range(limit):
        if current <= 1:
            break
        cmdline = _cmdline(current)
        if cmdline:
            name = extract(cmdline)
            if name:
                break
        if _comm(current) in _STOP:
            break
        parent = _ppid(current)
        if parent <= 1 or parent == current:
            break
        current = parent

    _cache[key] = (now, name)
    return name
