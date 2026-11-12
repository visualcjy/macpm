# Changelog

## 2.2 — 2026-11-12

### Added

- **Network view** on key `4` — a rootless overview built from `nettop`: TCP
  socket counts by state (`ESTABLISHED`, `LISTEN`, `CLOSE_WAIT`, `TIME_WAIT`),
  UDP socket count, interface throughput, a listening table (address, PID,
  process) and a per-process traffic table with socket count and download /
  upload rates.  `Tab` switches the focused table, `s` cycles its sort column,
  `↑` / `↓` scroll it and `Space` pauses sampling.  The traffic table
  refreshes every 2 seconds and the full socket snapshot every 5 seconds while
  the view is open.  `nettop` needs no root and sees sockets owned by root
  daemons; sockets without an owner (typically `TIME_WAIT`) are not reported.
- The network summary box has a line with the active IPv4 interface and prefix,
  the default gateway, and the public IP with its city.  For Chinese addresses
  the carrier abbreviation (Telecom, Unicom, Mobile) is appended after the
  country code, e.g. `Shanghai, CN Telecom`.
  The public lookup is one HTTPS request to `ipinfo.io` (falling back to
  `ipwho.is`), cached for 10 minutes and disabled with `--no-public-ip`.

### Changed

- The network summary uses fixed-width fields, so the labels no longer move as
  the counter and rate values change.  The traffic table is sorted by the
  visible `↓/s` column by default; the combined rate is no longer a hidden sort
  key.
- The Temps & Fans panel now pairs each component's temperature with its peak
  (CPU on one row, GPU on the next), followed by fan RPMs and one fan-load bar
  per fan.  The bars omit the numeric percentage so they can use the full panel
  width.  The network identity line is only shown in view 1, not in the
  per-core view.
- README: screenshots for the process and network views and refreshed
  highlights.

### Fixed

- Arrow keys were delivered as a bare `ESC` — which quits macpm — because the
  curses window never enabled keypad mode.  `↑` / `↓` now scroll the process
  and network tables as documented.
- The views reset only the foreground colour after drawing, not the whole SGR
  state.  A full reset also cleared the black background set by curses, so the
  terminal profile's background (often grey) showed through in the default,
  process and network views.

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
- **Process view** on key `3` — a two-box layout: a
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
