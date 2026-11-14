"""Print samples as JSON, or the capability report, without the TUI.

    macpm-dump                 # three samples
    macpm-dump --capabilities  # what this machine/permission level can read

``MACPM_SYSROOT=/path`` replays a captured /proc + /sys tree (tests, bug
reports); it works for both the sampler and the capability report.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys

from .backend import create_sampler, default_capabilities, platform_name


def _capability_report() -> dict:
    from .linux_backend.sysfs import SysRoot

    caps = default_capabilities()
    report = {
        "macpm_platform": platform_name(),
        "python": platform.python_version(),
        "root": caps.root,
        "sysroot": SysRoot().root or "/",
        "capabilities": caps.as_dict(),
        "missing": caps.missing(),
    }
    if caps.platform == "linux" and not caps.root:
        report["hint"] = (
            "run as root for RAPL CPU power, GPU engine data of other "
            "processes and other users' sockets"
        )
    try:
        sampler = create_sampler(1)
        report["soc"] = sampler.soc
        report["sampler"] = type(sampler).__name__
        monitor = getattr(sampler, "gpu", None)
        if monitor is not None and hasattr(monitor, "count"):
            report["gpu_count"] = monitor.count
        sampler.close()
    except Exception as error:  # pragma: no cover - diagnostics only
        report["sampler_error"] = str(error)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="macpm-dump",
        description="Print macpm samples or the capability report as JSON",
    )
    parser.add_argument("--capabilities", action="store_true",
                        help="print what macpm can read on this machine and exit")
    parser.add_argument("--count", type=int, default=3,
                        help="number of samples (default 3)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between samples (default 1)")
    args = parser.parse_args(argv)

    if args.capabilities:
        print(json.dumps(_capability_report(), indent=2, default=str))
        return 0

    import time

    sampler = create_sampler(max(0.1, args.interval))
    sampler.prime()
    try:
        for _ in range(max(1, args.count)):
            time.sleep(max(0.1, args.interval))
            print(json.dumps(sampler.sample(), indent=2, default=str))
    finally:
        sampler.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
