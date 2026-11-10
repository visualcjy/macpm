"""Print SMC temperatures, fan speeds and a couple of power rails.

This is a diagnostic helper; the default macpm TUI is intentionally kept
identical to macpm and does not show these panels.
"""

import argparse
import json

from .smc import SMC
from .soc import get_soc_info


def collect() -> dict:
    soc = get_soc_info()
    with SMC() as smc:
        return {
            "chip": soc["name"],
            "temperature_c": smc.get_temperatures(soc["name"]),
            "fans": smc.get_fans(),
            "power_w": smc.get_power(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(prog="macpm-sensors")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args()

    try:
        data = collect()
    except Exception as error:
        raise SystemExit(f"macpm-sensors: {error}")

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    print(f"chip: {data['chip']}")
    print("temperatures:")
    for key, value in data["temperature_c"].items():
        print(f"  {key}: {value:.2f} C")
    print("fans:")
    for fan in data["fans"]:
        print(
            f"  {fan['name']}: {fan['rpm']} rpm "
            f"(min {fan['min_rpm']}, max {fan['max_rpm']}, mode {fan['mode']})"
        )
    if data["power_w"]:
        print("power:")
        for key, value in data["power_w"].items():
            print(f"  {key}: {value:.2f} W")


if __name__ == "__main__":
    main()
