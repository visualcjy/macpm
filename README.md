# macpm

**Rootless performance monitoring CLI for Apple Silicon.**<br>
macpm 2.0 reads the same hardware counters as `powermetrics`, but through
IOReport/IOKit/SMC — no `sudo`, no password, no privileged helper.

![macpm](https://raw.githubusercontent.com/visualcjy/macpm/main/images/macpm-v.gif)

A Python-based `nvtop`-inspired terminal monitor for Apple Silicon Macs,
forked from [asitop](https://github.com/tlkh/asitop).

## Highlights

- **No sudo required.** `macpm` runs as a normal user process.
- CPU E/P core utilisation and frequency, plus an optional per-core view
- GPU utilisation and frequency
- CPU / GPU / ANE power, rolling average, peaks and throttle state
- RAM and swap usage
- Disk IOPS/throughput and network throughput
- Optional temperature/fan reader (`macpm-sensors`)
- Same terminal layout, colours, charts and key bindings as macpm 0.x

## Install

Requirements: **Apple Silicon Mac**, **macOS 12+**, **Python 3.9+**.

```sh
python3 -m pip install --upgrade macpm
```

Then run:

```sh
macpm
```

There is **no `sudo macpm`** in 2.0. Running it with `sudo` still works, but it
is unnecessary and no longer recommended.

Prefer a standalone binary with no Python installation? See
[Standalone binaries](#standalone-binaries).

## Usage

```text
macpm [--interval INTERVAL] [--color COLOR] [--avg AVG] [--show_cores [BOOL]] [--version]
```

| Option | Meaning |
|---|---|
| `--interval SECONDS` | Sampling and display interval (default `1`) |
| `--color 1-8` | Display colour (default `2`) |
| `--avg SECONDS` | Averaging window for power values (default `30`) |
| `--show_cores [BOOL]` | Start in the per-core view |
| `--version` | Print the version |

Keys:

```text
q / ESC     quit
← / →       change colour
1 / 2       default view / per-core view
Ctrl+R      reset peaks
```

## Why no sudo?

macpm 0.x called `sudo powermetrics`. In 2.0 the sampler reads the underlying
counters directly.  The data is not secret; the old `powermetrics` CLI simply
requires root and Apple's private entitlements.

| Metric | Source |
|---|---|
| CPU E/P load and frequency | IOReport `CPU Stats` performance states |
| Per-core load | IOReport `CPU Core Performance States` |
| GPU load and frequency | IOReport `GPU Stats` / `GPU Performance States` |
| CPU / GPU / ANE power | IOReport `Energy Model` |
| RAM / swap | `psutil` |
| Disk / network | `psutil` counters |
| Thermal throttle | `pmset -g therm` |
| SoC name / core counts | `sysctl` |
| GPU core count | `ioreg` `gpu-core-count` |

The DVFS frequency tables are parsed from the `pmgr` IORegistry node;
M1–M3 store Hz there, M4+ store kHz.

## Temperatures and fans

The default TUI intentionally stays identical to macpm 0.x, which has no
temperature/fan panel.  The standalone SMC reader is available as:

```sh
macpm-sensors
macpm-sensors --json
```

It reads `AppleSMC` through the IOKit user client (read-only, no root) and
reports the raw sensor keys present on the chip, fan RPM/min/max, and
`PSTR`/`PPBR` power rails.  Fan **control** requires root and is not
implemented.

## Compatibility

Tested on macOS 27.2 with an M3 Max.  Behaviour depends more on the chip
generation than on the macOS version:

| Environment | Status |
|---|---|
| macOS 12–15, M1–M4 | Works |
| macOS 26, M1–M4 | Likely works |
| macOS 26, M5 / M5 Pro / Max | Partial: new CPU tier/channel names (`MCPU`/`SCPU`) and `acc-clusters` DVFS data are not fully handled |
| macOS 15/26, M1/M2 Ultra | Needs verification: multi-die complex channels may be `ECPU0/1` |
| macOS 27 | Works on the tested M3 Max; some 27 builds restrict unprivileged `Energy Model` refreshes |
| Intel Macs | Not supported; the Apple-Silicon IOReport groups are absent |

## Standalone binaries

Standalone unsigned/ad-hoc arm64 binaries (`macpm`, `macpm-sensors`) can be
built with `packaging/build.sh` and attached to a GitHub release.  They bundle
Python and all dependencies, so the target Mac needs no Python installation.

On first launch from a browser download, macOS Gatekeeper may ask you to
right-click → Open, or you can remove the quarantine flag:

```sh
xattr -d com.apple.quarantine ./macpm ./macpm-sensors ./macpm.command
```

See [packaging/README.txt](https://github.com/visualcjy/macpm/blob/main/packaging/README.txt) for details.

## Development

```sh
git clone https://github.com/visualcjy/macpm.git
cd macpm
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

Build source and wheel distributions:

```sh
python3 -m pip install build twine
python3 -m build
python3 -m twine check dist/*
```

Build standalone arm64 binaries (build machine needs Python + PyInstaller +
runtime dependencies; the output has no Python dependency):

```sh
PYTHON=python3 packaging/build.sh
```

Release steps are documented in [RELEASE.md](https://github.com/visualcjy/macpm/blob/main/RELEASE.md).

## Troubleshooting

- **`macpm: command not found`** — add your Python user-base `bin` directory to
  `PATH`, or run `python3 -m macpm`.
- **Power readings freeze on macOS 27** — some builds restrict unprivileged
  `Energy Model` refreshes.  CPU/GPU usage and frequency still work.  Run
  `macpm-dump` to inspect the raw values.
- **M5/Ultra values look wrong** — CPU tier/channel mapping is not complete;
  see the compatibility table.
- **`macpm-sensors` shows nothing** — not every SMC key exists on every chip;
  the reader only reports the keys that are present.

## Changelog

See [CHANGELOG.md](https://github.com/visualcjy/macpm/blob/main/CHANGELOG.md).  The 2.0 release removes the dependency on
`sudo`, `powermetrics` and temporary files.

## Credits

- [asitop](https://github.com/tlkh/asitop) — original Python TUI this project
  was forked from
- [macmon](https://github.com/vladkens/macmon) and
  [Stats](https://github.com/exelban/stats) — IOReport/SMC reference
  implementations
- The IOReport sample-delta and DVFS-table ideas follow those projects

## License

MIT.  See [LICENSE](https://github.com/visualcjy/macpm/blob/main/LICENSE).
