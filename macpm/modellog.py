"""Read per-request statistics from a model server's own log.

Splash writes one line per request (``Started`` / ``TTFT`` / ``Done``) with the
same numbers its dashboard shows, so the terminal can show real history
instead of guessing from polls.  Only the bytes appended since the last read
are parsed; a truncated or rotated file restarts from the beginning.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

STARTED = re.compile(r"^(\d\d:\d\d:\d\d) Started · request (\d+) · input ([\d,]+) · cached ([\d,]+)")
TTFT = re.compile(r"^(\d\d:\d\d:\d\d) TTFT of request (\d+): ([\d.]+) ms")
DONE = re.compile(
    r"^(\d\d:\d\d:\d\d) Done · request (\d+) · input ([\d,]+) · cached ([\d,]+) "
    r"· output ([\d,]+).*?TTFT ([\d.]+)s · ([\d.]+) tok/s"
)

DEFAULT_SPLASH_LOG = "~/Library/Logs/Splash/server.log"


def _number(text: str) -> Optional[float]:
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def _clock(text: str) -> float:
    """``HH:MM:SS`` today → epoch seconds (logs are same-day)."""
    now = datetime.now()
    try:
        hour, minute, second = (int(part) for part in text.split(":"))
    except (ValueError, AttributeError):
        return time.time()
    return now.replace(hour=hour, minute=minute, second=second, microsecond=0).timestamp()


class LogTail:
    """Return only the lines appended to ``path`` since the previous call."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self.offset = 0

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def read(self) -> list:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:          # truncated or rotated
            self.offset = 0
        if size == self.offset:
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                lines = handle.read().splitlines()
                self.offset = handle.tell()
                return lines
        except OSError:
            return []


class SplashLogReader:
    """Turn Splash log lines into completed-request rows."""

    def __init__(self, path: str = DEFAULT_SPLASH_LOG):
        self.tail = LogTail(path)
        self.pending: dict = {}

    def poll(self) -> list:
        rows = []
        for line in self.tail.read():
            line = line.strip()
            match = STARTED.match(line)
            if match:
                clock, rid, prompt, cached = match.groups()
                self.pending[rid] = {
                    "started": _clock(clock),
                    "prompt": _number(prompt),
                    "cached": _number(cached),
                }
                continue
            match = TTFT.match(line)
            if match:
                _clock_text, rid, ms = match.groups()
                entry = self.pending.setdefault(rid, {})
                entry["first_ms"] = _number(ms)
                continue
            match = DONE.match(line)
            if match:
                (clock, rid, prompt, cached, output,
                 ttft_s, rate) = match.groups()
                entry = self.pending.pop(rid, {})
                started = entry.get("started") or _clock(clock)
                finished = _clock(clock)
                rows.append({
                    "finished": finished,
                    "started": started,
                    "duration": max(0.0, finished - started),
                    "prompt": _number(prompt),
                    "cached": _number(cached),
                    "output": _number(output),
                    "first_ms": entry.get("first_ms")
                                 or (_number(ttft_s) or 0.0) * 1000.0,
                    "tps": _number(rate),
                })
        return rows


class CommandTail:
    """Read new lines by re-running a bounded command (docker/kubectl logs)."""

    def __init__(self, command, timeout: float = 10.0):
        self.command = list(command)
        self.timeout = timeout
        self._seen: deque = deque(maxlen=500)

    def read(self) -> list:
        try:
            result = subprocess.run(
                self.command, capture_output=True, text=True,
                timeout=self.timeout, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        lines = (result.stdout or "").splitlines()
        fresh = [line for line in lines if line not in self._seen]
        self._seen.extend(fresh)
        return fresh


class HttpTail:
    """Tail a plain-text log endpoint (oMLX ``/admin/api/logs``)."""

    def __init__(self, url: str, timeout: float = 3.0):
        self.url = url
        self.timeout = timeout
        self._seen: deque = deque(maxlen=500)

    def read(self) -> list:
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as response:
                text = response.read(1 << 20).decode("utf-8", "replace")
        except (OSError, ValueError):
            return []
        fresh = [line for line in text.splitlines() if line not in self._seen]
        self._seen.extend(fresh)
        return fresh


VLLM_STATS = re.compile(
    r"Engine \d+: Avg prompt throughput: ([\d.]+) tokens/s, "
    r"Avg generation throughput: ([\d.]+) tokens/s, "
    r"Running: (\d+) reqs, Waiting: (\d+) reqs, "
    r"GPU KV cache usage: ([\d.]+)%, Prefix cache hit rate: ([\d.]+)%"
)


def parse_vllm_stats(line: str):
    """One ``Engine 000:`` stats line -> dict, or ``None``."""
    match = VLLM_STATS.search(line or "")
    if not match:
        return None
    prefill, decode, running, waiting, kv, hit = match.groups()
    return {
        "prefill_tps": float(prefill),
        "decode_tps": float(decode),
        "active": int(running),
        "waiting": int(waiting),
        "kv_pct": float(kv),
        "prefix_hit": float(hit),
    }


class JsonLogTail(LogTail):
    """Docker's json-file driver wraps each line as ``{"log": "..."}``."""

    def read(self) -> list:
        import json

        lines = []
        for raw in super().read():
            try:
                payload = json.loads(raw)
            except ValueError:
                lines.append(raw)
                continue
            text = payload.get("log")
            if text:
                lines.append(text.rstrip("\n"))
        return lines


def _run(command, timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout or ""


def docker_log_path(container: str) -> str:
    """Host path of a container's json-file log, or ``""``."""
    return _run(["docker", "inspect", "--format", "{{.LogPath}}",
                 container]).strip()


_PORTS = re.compile(r"(?:[\d.]+|\*)?:(\d+)->\d+/tcp")


def _backend_for(image: str) -> str:
    image = (image or "").lower()
    for needle, key in (("sglang", "sglang"), ("vllm", "vllm"),
                        ("ollama", "ollama"), ("llama", "llama.cpp"),
                        ("omlx", "omlx"), ("splash", "splash")):
        if needle in image:
            return key
    return "manual"


def docker_containers(runner=None) -> list:
    """Published container ports as ``[(backend, port, name, image)]``."""
    run = runner or _run
    out = run(["docker", "ps", "--format",
               "{{.Names}}|{{.Image}}|{{.Ports}}"])
    rows = []
    for line in (out or "").splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, image, ports = parts[0], parts[1], parts[2]
        for port in _PORTS.findall(ports):
            rows.append((_backend_for(image), int(port), name, image))
    return rows
