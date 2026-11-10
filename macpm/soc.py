"""SoC identity, core counts and DVFS frequency tables without root."""

from __future__ import annotations

import re
import subprocess

import psutil

SYSCTL = "/usr/sbin/sysctl"
IOREG = "/usr/sbin/ioreg"
GREP = "/usr/bin/grep"
SHELL = "/bin/sh"


def _run(command: list[str], timeout: float = 10.0) -> str:
    try:
        proc = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.decode("utf-8", "ignore")


def _run_shell(command: str, timeout: float = 10.0) -> str:
    """Run a fixed shell pipeline and return its (already filtered) stdout.

    ioreg prints several megabytes of properties for IOAccelerator/pmgr.
    Capturing all of it and filtering in Python leaves the allocator holding
    tens of megabytes; grep in the pipeline keeps the capture small.
    """
    try:
        proc = subprocess.run(
            [SHELL, "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.decode("utf-8", "ignore")


def sysctl(name: str) -> str:
    return _run([SYSCTL, "-n", name]).strip()


def sysctl_int(name: str, default: int = 0) -> int:
    try:
        return int(sysctl(name))
    except ValueError:
        return default


def get_gpu_cores():
    """GPU core count from the accelerator node (fast), system_profiler fallback."""
    out = _run_shell(
        IOREG + ' -l -w0 -c IOAccelerator | ' + GREP + ' -m1 \'"gpu-core-count"\''
    )
    match = re.search(r'"gpu-core-count"\s*=\s*(\d+)', out)
    if match:
        return int(match.group(1))

    out = _run(
        [
            "/usr/sbin/system_profiler",
            "-detailLevel",
            "basic",
            "SPDisplaysDataType",
        ],
        timeout=20.0,
    )
    match = re.search(r"Total Number of Cores:\s*(\d+)", out)
    if match:
        return int(match.group(1))
    return "?"


# Max CPU/GPU power and memory bandwidth are guesses in macpm as well.  Keep the
# original table so the charts scale exactly like macpm does on the same chip.
def _tdp(name: str) -> tuple[int, int]:
    if name == "Apple M1 Max":
        return 30, 60
    if name == "Apple M1 Pro":
        return 30, 30
    if name == "Apple M1":
        return 20, 20
    if name == "Apple M1 Ultra":
        return 60, 120
    if name == "Apple M2":
        return 25, 15
    return 20, 20


def _bandwidth(name: str) -> tuple[int, int]:
    if name == "Apple M1 Max":
        return 250, 400
    if name == "Apple M1 Pro":
        return 200, 200
    if name == "Apple M1":
        return 70, 70
    if name == "Apple M1 Ultra":
        return 500, 800
    if name == "Apple M2":
        return 100, 100
    return 70, 70


def get_soc_info() -> dict:
    name = sysctl("machdep.cpu.brand_string") or "Apple Silicon"
    core_count = sysctl_int("machdep.cpu.core_count")
    p_cores = sysctl_int("hw.perflevel0.logicalcpu")
    e_cores = sysctl_int("hw.perflevel1.logicalcpu")
    if p_cores + e_cores == 0:
        p_cores = psutil.cpu_count(logical=True) or 1
        e_cores = 0
    if core_count <= 0:
        core_count = p_cores + e_cores

    cpu_max_power, gpu_max_power = _tdp(name)
    cpu_max_bw, gpu_max_bw = _bandwidth(name)
    return {
        "name": name,
        "core_count": core_count,
        "cpu_max_power": cpu_max_power,
        "gpu_max_power": gpu_max_power,
        "cpu_max_bw": cpu_max_bw,
        "gpu_max_bw": gpu_max_bw,
        "e_core_count": e_cores,
        "p_core_count": p_cores,
        "gpu_core_count": get_gpu_cores(),
    }


_TABLE_RE = re.compile(
    r'"(voltage-states[0-9]+(?:-sram)?)"\s*=\s*<([0-9a-fA-F]+)>'
)


def _ioreg_tables() -> dict[str, bytes]:
    out = _run_shell(
        IOREG + ' -l -w0 -n pmgr | ' + GREP + ' -E \'"voltage-states[0-9]+(-sram)?"\''
    )
    tables: dict[str, bytes] = {}
    for match in _TABLE_RE.finditer(out):
        try:
            tables[match.group(1)] = bytes.fromhex(match.group(2))
        except ValueError:
            continue
    return tables


def _scale_for(raw_freqs: list[int], chip: str) -> int:
    if any(token in chip for token in ("M1", "M2", "M3")) or "A1" in chip:
        return 1_000_000
    if any(token in chip for token in ("M4", "M5", "M6")):
        return 1_000
    # Unknown chip: infer from the magnitude.
    return 1_000_000 if max(raw_freqs, default=0) > 20_000_000 else 1_000


def _freqs_from_raw(raw: list[int], scale: int, drop_first_zero: bool = False) -> list[int]:
    if drop_first_zero and raw and raw[0] == 0:
        raw = raw[1:]
    # The first word of each 8-byte entry is the frequency; the second is voltage.
    return [value // scale for value in raw]


def get_freq_tables(chip: str) -> tuple[list[int], list[int], list[int]]:
    """Return (E-core MHz, P-core MHz, GPU MHz) tables.

    M1-M3 store Hz in these properties, M4+ store kHz; the tables live on the
    pmgr node.  Missing tables are tolerated and simply produce 0 MHz readings.
    """
    tables = _ioreg_tables()

    def table(key: str, drop_first_zero: bool = False) -> list[int]:
        data = tables.get(key)
        if not data:
            return []
        raw = [
            int.from_bytes(data[offset : offset + 4], "little")
            for offset in range(0, len(data) - 7, 8)
        ]
        return _freqs_from_raw(raw, _scale_for(raw, chip), drop_first_zero)

    e_freqs = table("voltage-states1-sram")
    p_freqs = table("voltage-states5-sram")
    if not p_freqs:
        # M5 Pro/Max store the performance tier in a different slot.
        p_freqs = table("voltage-states22-sram")

    gpu_freqs = table("voltage-states9", drop_first_zero=True)
    if not gpu_freqs:
        gpu_freqs = table("voltage-states9-sram", drop_first_zero=True)

    if not e_freqs and not p_freqs:
        # Last resort: the package frequency is not per cluster, but it keeps
        # the gauge alive on an unknown chip instead of showing 0 MHz.
        try:
            current = psutil.cpu_freq()
        except Exception:
            current = None
        if current and current.current:
            value = int(current.current)
            e_freqs = [value]
            p_freqs = [value]

    return e_freqs, p_freqs, gpu_freqs
