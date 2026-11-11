# Changelog

## 2.1 — 2026-11-11

### Changed

- The power panel is titled `Power` instead of `CPU+GPU+ANE Power`, and every
  panel title is clipped to its box so a long title never wraps the frame.
- The Temps & Fans panel now uses its spare rows for CPU/GPU peak temperatures
  and fan limits instead of leaving them blank.
- Per-core gauges are labelled `E1…` / `P1…` instead of `Core-1` / `C-1`, so
  narrow gauges keep the percentage readable.

### Added

- **Temps & Fans panel** in the default view — hottest CPU and GPU sensor in
  °C and the current RPM of every fan, read from `AppleSMC` without root.
- **Process view** on key `3` — a macmon-style two-box layout: a
  CPU/GPU/RAM summary box (with wired/active/inactive/free memory) and a
  process table with PID, NAME, USER, CPU%, MEM and GPU% columns, sorted with
  `s`, scrolled with `↑`/`↓` and paused with `Space`.  The table refreshes
  every 2 seconds to keep macpm's own CPU low.  There is no power column.

### Removed

- The separate `macpm-sensors` command.  The SMC reader is now part of the TUI
  (`macpm.sensors.SensorReader`).

### Fixed

- Impossible power peaks (such as `peak: 6208.38W`) caused by coalesced or
  reset Energy Model counters are now discarded instead of being recorded.
- When the unprivileged Energy Model counters never report, the power panel
  shows `n/a` instead of a misleading `0.00W`; the total is withheld only when
  both CPU and GPU are unreadable.

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
