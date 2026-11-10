# Changelog

## 2.0 — 2026-11-10

### Changed

- **Rootless by default**: macpm no longer runs `sudo`, `powermetrics` or any
  privileged helper. Data is collected from IOReport, IOKit, `psutil`,
  `sysctl`, `ioreg` and `pmset`.
- Reworked the sampler into `macpm.sampler`, with ctypes bindings in
  `macpm.ioreport` and SoC/DVFS discovery in `macpm.soc`.
- The terminal layout, panel titles, charts, colours and key bindings remain
  the same as macpm 0.x.
- Version bumped to 2.0.

### Added

- `macpm-sensors` / `macpm.sensors` — read SMC temperatures and fan speeds
  without root (read-only).
- `macpm-dump` / `macpm.dump` — print the rootless metrics as JSON for
  debugging.
- Optional per-core view improvements and reset/peak handling retained from
  0.24.
- `--version` flag.
- Compatibility notes for macOS 12–27, M1–M4 and partial M5/Ultra support.

### Fixed

- `--show_cores` now accepts a real boolean value and `--show_cores` alone.
- Zero-division guards for `--avg < --interval` and for chips without E cores.
- No child `powermetrics` process is left behind when the TUI exits.
- `ioreg` output is filtered at the source to avoid retaining tens of MB of
  XML in the Python allocator.
- Only the required `Energy Model` channels are subscribed, reducing memory
  and CPU usage.

### Removed

- `sudo echo` pre-authentication and the `sudo nice powermetrics` subprocess.
- The `macpm2` development package (merged into `macpm`).

## 0.24

- Last 0.x release: `sudo` + `powermetrics` based sampler, Python TUI with
  CPU/GPU/ANE power, RAM, disk and network panels.
