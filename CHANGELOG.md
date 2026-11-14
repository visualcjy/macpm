# Changelog

## 3.0 — 2026-11-14

The Linux release.  The same package and the same TUI now run on x86_64 and
arm64 Linux, and the GPU view grew into a per-card monitor that scales from one
Apple GPU to a 64-card accelerator server.  Verified on 16× Iluvatar BI-V150S,
64× Denglin KS58, 8× Techo TECO_AICARD_01 and 8× RTX 4090.

### Added

- **Linux backend** (`macpm/backend.py`, `macpm/linux_backend/`): the platform
  is chosen at runtime, and every reading is normalised into the powermetrics
  shaped sample the UI already understood, so all four views and key bindings
  are unchanged.
  - CPU: per-core utilisation from `/proc/stat`, frequency from
    `cpufreq/scaling_cur_freq` (residency-weighted when
    `cpu/stats/time_in_state` exists), and an E/P split inferred from
    `topology/core_type`, `cpu_capacity` or `cpuinfo_max_freq`.
  - Power: RAPL `energy_uj` (root on kernels >= 5.10), GPU hwmon power and the
    battery's `power_now` as a whole-system fallback.
  - GPU: amdgpu (`gpu_busy_percent`, `pp_dpm_sclk`, hwmon), Intel i915/xe
    (frequency plus aggregate DRM `fdinfo` engine time) and NVIDIA via
    `nvidia-smi`.
  - Thermals: hwmon temperatures/fans classified by chip name and label, plus
    a PSI/x86-throttle approximation of the thermal state.
  - Process view: per-process GPU time from `drm-engine-*` `fdinfo`; Linux lets
    `psutil` read every user's CPU/memory as root.
  - Network view: `/proc/net/{tcp,tcp6,udp,udp6}` for counts and listeners and
    `ss -tanie` `tcp_info` joined by inode for the per-process traffic table
    (TCP only).
  - Capabilities say what the kernel and permission level actually expose;
    everything else is shown as `n/a` instead of a fake zero, and the UI hides
    panels it cannot fill.
- **GPU view** on key `5` — one table with **two lines per card**: the metrics
  line (utilisation bar, clock, power, a 10-cell VRAM bar, temperature) and the
  processes on that card (`PID NAME [cards] GPU% MEM`), with the served model
  name right-aligned.  A process holding several cards appears under each of
  them with its range in brackets; a card with more processes than fit shows
  `… +N`.  `s` cycles the in-card order (PID → NAME → MEM), `↑` / `↓` turn
  whole pages and the footer shows `Page N/M`.  A single-GPU machine keeps the
  card summary and the separate process table.
- **GPU INFO box** above the table: vendor card name, device count, card count
  and dies per card (devices ÷ unique serials), driver, CUDA, PCIe link
  (maximum, so idle cards do not read as generation 1) and VBIOS — each hidden
  when the tool does not report it.  Consumer cards that report `[N/A]` serials
  leave the card/die counts out instead of guessing.
- **Vendor accelerator backends** — cards with no DRM counters are read through
  the vendor's NVML-style CLI: `ixsmi` (Iluvatar / 天数, `--query-gpu`),
  `dlsmi` (Denglin / 登临, `--query-gpu`) and `teco-smi` (Techo / 太初,
  `--query-device`).  Each tool's switch and property names are mapped onto the
  same fields, the power property is negotiated per tool, and the binaries are
  found on `PATH`, in their install directories or via `MACPM_IXSMI`,
  `MACPM_DLSMI` and `MACPM_TECO`.
- **Vendor compute processes** feed the GPU view: `ixsmi pmon` joined with
  `--query-compute-apps` (memory), `dlsmi --query-compute-apps` and
  `teco-smi pmon`.  One row per PID with the cards it holds (compressed to
  `0-7`), its card memory and the vendor's GPU% where the tool reports one
  (`—` otherwise, e.g. `dlsmi`).  Processes that hold several cards are listed
  under each of them, and `teco-smi`'s per-device repetition is preserved
  rather than pretending a process lives on one card.
- **Served model name** on the process line, right-aligned: resolved from the
  launcher's command line (`--served-model-name`, `--model`, `--model-path`,
  `--model-id`, `vllm serve`, `sglang.launch_server`, TGI, llama.cpp, including
  k8s `bash -c` wrappers) by walking the parent chain, cached for ten seconds.
  Only procfs is read; model directories are never opened because a stalled
  network mount can block that indefinitely.
- **NVIDIA GPU view** — one `nvidia-smi --query-gpu` call for the card rows,
  `--query-compute-apps` (joined with `pmon` for SM%) for the process lines and
  the same tool for the INFO box.  `nvidia-smi` is resolved from
  `MACPM_NVIDIA_SMI`, `PATH` or the WSL mount, and NVIDIA DRM nodes are skipped
  when `nvidia-smi` is available so cards are not listed twice.
- **`python3 -m macpm.probe`** — a standard-library inventory (cards, drivers,
  vendor tools, capability report) for compute nodes, with no psutil or
  terminal needed.  `macpm-dump --capabilities` prints the platform, root
  state and the same report.
- **Deterministic tests on any machine** — `MACPM_SYSROOT` replays a captured
  `/proc` + `/sys` tree, `MACPM_PLATFORM` forces the Linux backend, and
  `tests/linux_fixtures.py` builds hybrid x86, arm64 big.LITTLE, multi-card and
  empty trees.
- Every view's footer starts with the app name and version
  (`macpm v3.0 · q quit …`), so a screenshot or pasted frame identifies the
  build.

### Changed

- CoreFoundation/IOKit/IOReport/AppleSMC are loaded lazily, so importing macpm
  (and `--version` / `--help`) works on platforms without those frameworks.
- The UI builds its gauges from the detected capabilities: no E cluster means a
  single `CPU Usage` gauge, no ANE means no ANE gauge, no GPU means no GPU
  gauge, and the power panel is hidden when no power source exists.
- The default route and public-IP lookup use `urllib` instead of `curl`, and
  `ip route` / `/proc/net/route` instead of `route -n get default` on Linux.
- The sensor panel says `sensors unavailable` instead of `SMC unavailable`
  (the backend may be hwmon).

### Fixed

- NVIDIA GPUs were listed twice on drivers that expose DRM nodes (16 rows for
  8 cards); the DRM duplicate is skipped when `nvidia-smi` is present.
- Drivers that reject `cuda_version` (e.g. 580) made the whole INFO query fail;
  the query now falls back to the remaining fields.
- `teco-smi` cards/dies are no longer derived from serials: it exposes eight
  cards of four SDAA dies each without a board grouping, so only the device
  count is shown.
- VRAM cells pad the used-memory number to a fixed width, so busy and idle
  cards keep their bars aligned.

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
