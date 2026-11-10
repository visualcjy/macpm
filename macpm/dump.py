"""Print rootless samples as JSON (debug helper, not part of the TUI)."""

import json
import time

from .sampler import Sampler


def main() -> None:
    sampler = Sampler(1)
    sampler.prime()
    try:
        for _ in range(3):
            time.sleep(1)
            print(json.dumps(sampler.sample(), indent=2, default=str))
    finally:
        sampler.close()


if __name__ == "__main__":
    main()
