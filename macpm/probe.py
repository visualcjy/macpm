"""Inventory a Linux box for macpm compatibility — standard library only.

This answers "will macpm work here, and what will it show?" without installing
anything:

    python3 macpm/probe.py            # human readable
    python3 macpm/probe.py --json     # machine readable

It walks the same sources the Linux backend uses (``/sys/class/drm``,
``/sys/class/hwmon``, ``/proc``), lists which counters each GPU exposes, checks
for vendor CLI tools (``nvidia-smi``, ``ixsmi``, ``rocm-smi``…) and, when the
macpm package is importable, prints the capability report it would show in the
UI.  Run it with and without ``sudo`` to see the difference root makes.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
import sys

def _root() -> str:
    """``MACPM_SYSROOT`` replays a captured tree, same knob as the backend."""
    return os.environ.get("MACPM_SYSROOT", "").rstrip("/")


def _p(path: str) -> str:
    root = _root()
    return root + path if root else path


def _glob(pattern: str) -> list:
    root = _root()
    matches = sorted(glob.glob(_p(pattern)))
    if root:
        return [
            match[len(root):] if match.startswith(root) else match
            for match in matches
        ]
    return matches


def _listdir(path: str) -> list:
    try:
        return sorted(os.listdir(_p(path)))
    except OSError:
        return []

VENDOR_TOOLS = (
    "nvidia-smi",
    "rocm-smi",
    "xpu-smi",
    "intel_gpu_top",
    "ixsmi",
    "ix-smi",
    "dlsmi",
    "teco-smi",
    "brsmi",
    "mx-smi",
    "cnmon",
    "npu-smi",
    "hy-smi",
)

# Files that tell macpm (or a future backend) what a card can report.
_CARD_FILES = (
    "gpu_busy_percent",
    "gpu_busy",
    "busy_percent",
    "utilization",
    "load",
    "mem_info_vram_used",
    "mem_info_vram_total",
    "mem_info_vis_vram_used",
    "mem_info_vis_vram_total",
    "lmem_total_bytes",
    "pp_dpm_sclk",
    "pp_dpm_mclk",
    "gt_cur_freq_mhz",
    "gt_max_freq_mhz",
    "tile0/gt0/freq0/cur_freq",
    "tile0/gt0/freq0/max_freq",
    "product_name",
    "vendor",
    "device",
)

_DRM_GLOB = "/sys/class/drm/card[0-9]*"
_HWMON_GLOB = "/sys/class/hwmon/hwmon*"

_MACPM_BACKEND_FILES = (
    "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
    "/sys/devices/system/cpu/cpu0/cpufreq/stats/time_in_state",
    "/sys/devices/system/cpu/cpu0/topology/core_type",
    "/sys/devices/system/cpu/cpu0/cpu_capacity",
    "/sys/class/powercap/intel-rapl:0/energy_uj",
    "/proc/pressure/cpu",
    "/proc/diskstats",
    "/proc/net/dev",
    "/proc/net/tcp",
)


def _read(path: str, limit: int = 4096) -> str:
    try:
        with open(_p(path), "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit).strip()
    except OSError:
        return ""


def _read_int(path: str):
    text = _read(path)
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _exists(path: str) -> bool:
    return os.path.exists(_p(path))


def _uname() -> dict:
    return {
        "kernel": platform.release(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }


def _os_release() -> dict:
    info = {}
    for line in _read("/etc/os-release", 8192).splitlines():
        key, _, value = line.partition("=")
        if key and value:
            info[key] = value.strip().strip('"')
    return info


def _run(command: list) -> str:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.decode("utf-8", "replace").strip()


def _hwmon_chips() -> list:
    chips = []
    for path in _glob(_HWMON_GLOB):
        name = _read(path + "/name")
        if not name:
            continue
        temps = []
        fans = []
        power = []
        for entry in _listdir(path):
            if entry.startswith("temp") and entry.endswith("_input"):
                value = _read_int(path + "/" + entry)
                if value is not None:
                    temps.append(round(value / 1000.0, 1))
            elif entry.startswith("fan") and entry.endswith("_input"):
                value = _read_int(path + "/" + entry)
                if value is not None:
                    fans.append(value)
            elif entry.startswith("power") and entry.endswith(("_input", "_average")):
                value = _read_int(path + "/" + entry)
                if value is not None:
                    power.append(round(value / 1_000_000.0, 2))
        chips.append(
            {
                "path": path,
                "name": name,
                "temps_c": temps,
                "fans_rpm": fans,
                "power_w": power,
            }
        )
    return chips


def _cards() -> list:
    import re

    cards = []
    for path in _glob(_DRM_GLOB):
        if not re.match(r"^card\d+$", os.path.basename(path)):
            continue
        device = path + "/device"
        if not _exists(device):
            continue
        driver = ""
        try:
            driver = os.path.basename(os.readlink(_p(device + "/driver")).rstrip("/"))
        except OSError:
            pass
        files = {name: _exists(device + "/" + name) for name in _CARD_FILES}
        present = [name for name, found in files.items() if found]
        hwmon = [
            {
                "name": _read(entry + "/name"),
                "power": _exists(entry + "/power1_average")
                or _exists(entry + "/power1_input"),
                "temp": _exists(entry + "/temp1_input"),
            }
            for entry in _glob(device + "/hwmon/hwmon*")
        ]
        cards.append(
            {
                "card": os.path.basename(path),
                "driver": driver,
                "vendor": _read(device + "/vendor"),
                "device_id": _read(device + "/device"),
                "busy": next(
                    (
                        _read_int(device + "/" + name)
                        for name in ("gpu_busy_percent", "gpu_busy", "busy_percent",
                                     "utilization", "load")
                        if _read_int(device + "/" + name) is not None
                    ),
                    None,
                ),
                "files": present,
                "hwmon": hwmon,
            }
        )
    return cards


def _nvidia() -> dict:
    gpus = []
    for path in _glob("/proc/driver/nvidia/gpus/*/information"):
        model = ""
        for line in _read(path, 8192).splitlines():
            if line.lower().startswith("model:"):
                model = line.split(":", 1)[1].strip()
        gpus.append({"path": path, "model": model})
    smi = shutil.which("nvidia-smi")
    listing = ""
    if smi:
        listing = _run([smi, "-L"])
    return {"proc_entries": gpus, "nvidia_smi": smi or "", "listing": listing}


def _sockets() -> int:
    return len(_read("/proc/net/tcp", 1 << 20).splitlines())


def collect() -> dict:
    report = {
        "uname": _uname(),
        "os_release": _os_release(),
        "euid": os.geteuid() if hasattr(os, "geteuid") else -1,
        "root": (os.geteuid() == 0) if hasattr(os, "geteuid") else False,
        "cpu": {
            "model": next(
                (
                    line.split(":", 1)[1].strip()
                    for line in _read("/proc/cpuinfo", 1 << 16).splitlines()
                    if line.startswith("model name") or line.startswith("Model")
                ),
                "",
            ),
            "logical": _read_int("/sys/devices/system/cpu/online") or None,
            "cpus_online": len(
                [
                    line
                    for line in _read("/sys/devices/system/cpu/online").split(",")
                    if line
                ]
            ),
        },
        "macpm_sources": {
            path: _exists(path) for path in _MACPM_BACKEND_FILES
        },
        "drm_cards": _cards(),
        "hwmon": _hwmon_chips(),
        "nvidia": _nvidia(),
        "accelerators": {
            "dev_dri": _listdir("/dev/dri"),
            "sys_class_accel": _listdir("/sys/class/accel"),
            "dev_nodes": [
                entry
                for entry in _listdir("/dev")
                if entry.startswith(("ix", "accel", "npu", "davinci", "mx", "hy"))
            ],
        },
        "tools": {name: shutil.which(name) or "" for name in VENDOR_TOOLS},
        "containers": {
            "docker": shutil.which("docker") or "",
            "podman": shutil.which("podman") or "",
        },
        "tcp_sockets": _sockets(),
    }

    if __package__ in (None, ""):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from macpm.backend import default_capabilities

        caps = default_capabilities()
        report["macpm_capabilities"] = caps.as_dict()
    except Exception as error:  # package not installed / import error
        report["macpm_capabilities"] = {"error": str(error)}

    return report


def _human(report: dict) -> str:
    lines = []
    uname = report["uname"]
    distro = report["os_release"].get("PRETTY_NAME", "unknown distro")
    lines.append(f"host      : {distro}  kernel {uname['kernel']}  {uname['machine']}")
    lines.append(
        f"python    : {uname['python']}   root: {report['root']} (euid {report['euid']})"
    )
    lines.append(f"cpu       : {report['cpu']['model'] or '?'}")
    lines.append("")

    cards = report["drm_cards"]
    lines.append(f"DRM cards : {len(cards)}")
    for card in cards:
        busy = "n/a" if card["busy"] is None else str(card["busy"])
        hwmon = ", ".join(
            entry["name"] + ("(p/t)" if entry["power"] and entry["temp"] else "")
            for entry in card["hwmon"]
        )
        lines.append(
            f"  {card['card']:>6}  driver={card['driver'] or '?':<10}"
            f" vendor={card['vendor'] or '?':<7} busy={busy:<4} "
            f"hwmon=[{hwmon}]"
        )
        if card["files"]:
            lines.append(f"          files: {' '.join(card['files'])}")
    if not cards:
        lines.append("  (none)")

    nvidia = report["nvidia"]
    lines.append("")
    lines.append(f"nvidia    : {len(nvidia['proc_entries'])} proc entries, "
                 f"smi={nvidia['nvidia_smi'] or 'no'}")
    if nvidia["listing"]:
        for line in nvidia["listing"].splitlines():
            lines.append(f"  {line}")

    lines.append("")
    lines.append(f"hwmon     : {len(report['hwmon'])} chips")
    for chip in report["hwmon"]:
        lines.append(
            f"  {chip['name']:<16} temps={chip['temps_c']} "
            f"fans={chip['fans_rpm']} power={chip['power_w']}"
        )

    accel = report["accelerators"]
    lines.append("")
    lines.append(f"/dev/dri  : {accel['dev_dri']}")
    lines.append(f"accel     : {accel['sys_class_accel']}  nodes={accel['dev_nodes']}")

    lines.append("")
    tools = {name: path for name, path in report["tools"].items() if path}
    lines.append(f"tools     : {tools or 'none'}")
    containers = report["containers"]
    lines.append(
        f"containers: docker={containers['docker'] or 'no'} "
        f"podman={containers['podman'] or 'no'}"
    )

    sources = report["macpm_sources"]
    missing = [path for path, found in sources.items() if not found]
    lines.append("")
    lines.append(f"macpm srcs: {len(sources) - len(missing)}/{len(sources)} present")
    if missing:
        lines.append(f"  missing: {missing}")

    caps = report.get("macpm_capabilities") or {}
    if "error" in caps:
        lines.append("")
        lines.append(f"macpm     : not importable ({caps['error']})")
    else:
        lines.append("")
        lines.append("macpm     : capability report")
        for key, value in caps.items():
            lines.append(f"  {key:<22} {value}")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    report = collect()
    if "--json" in argv:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
