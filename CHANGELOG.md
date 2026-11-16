# Changelog

## Unreleased

### Changed

- macOS GPU view: a process stays listed while it holds GPU memory, even when
  its `GPU%` is momentarily 0 (a browser between frames), instead of flickering
  in and out of the table.  The `CARD` column is hidden on macOS, where a
  single integrated GPU would make every row read `-`.
- The macOS `footprint` poller now targets only processes that actually hold a
  Metal client (from the IORegistry), and rotates the idle targets beyond its
  32-target cap, so every GPU app is eventually measured instead of the
  lowest-PID daemons taking the slots.
- A single-card GPU view no longer repeats that card's power in the box title;
  the total stays on multi-GPU hosts, where each row is one card.
- Narrow terminals keep the card table's VRAM column: it first shrinks to a
  short bar with compact numbers, then to numbers only, and is dropped only
  below ~74 columns.  Previously the whole column disappeared under 86
  columns.

## 3.2.0 — 2026-11-16

### Added

- Linux AMD/Intel cards report per-process GPU memory from DRM `fdinfo`
  (`drm-resident-vram`/`gtt`/`local0`/`system0` plus `drm-shared-*`).  Bytes are
  charged only when the client is provably private: one visible holder, opened
  through a render node, every shared counter present and zero, and a complete
  scan.  Anything else shows `n/a` with a reason (`shared_client`,
  `shared_buffers`, `primary`, `legacy`, `partial_scan`, `incomplete`), so a
  shared buffer is never presented as private memory.
- The GPU view's single-GPU table gained `GPU MEM` and (when the card reports
  GTT) `SHARED` columns, and the per-card process line shows the memory of the
  card's own processes.
- macOS: the GPU view reads the IOAccelerator's `PerformanceStatistics` through
  the existing IOKit bindings, so the VRAM cell shows `Alloc system memory` out
  of unified RAM and the INFO box adds GPU cores plus the Renderer/Tiler split.
  An integrated GPU's `drm-resident-system0` bytes are charged as that
  process's GPU memory rather than as spill.
- macOS: per-process `GPU MEM` comes from `footprint`'s `(graphics)`
  categories, so the column agrees with the device number instead of showing
  `n/a` (or RSS).  A background thread measures only the processes on screen
  (~15 ms each, busy every 2 s, idle every 30 s, minute-long backoff after
  three failures, stops ~10 s after the view is closed); other users' processes
  need root and stay `n/a`.
- `tools/ane_load.py`: a manual ANE load (Core ML `mlprogram`) that prints the
  same residency figure as the gauge, for checking it without root.

### Changed

- DRM clients are keyed by `drm-pdev`, so a process is attributed to the card
  it actually holds; on a multi-AMD host every process used to land on card 0.
- The GPU view never fills its memory column from RSS.  System memory in a
  GPU-memory column is a guess, so an unknown figure stays `n/a`; RSS keeps its
  own `MEM` column in the process view.
- `GpuMonitor.samples()` takes one `fdinfo` walk per refresh and hands it to
  every card; an 8-card host used to walk every process's fd table once per
  card plus once more for the process ranking.
- Intel `i915`/`xe` report per-card utilisation through `drm-pdev`; only an old
  kernel without `drm-pdev` falls back to `n/a` on multi-GPU hosts.
- The Linux process ranking no longer drops GPU-active but CPU-idle processes
  (an inference worker between requests) just because they are outside the
  top 60 by CPU time.
- The default view's network identity (interface/CIDR, gateway, public IP and
  city) sits on the Network IO box's bottom border and keeps the last non-empty
  text; the key-hint footer is a plain line below it.  The previous spare-row
  slot only existed at some terminal heights, and a processor-box title was
  repainted over.
- The Memory box draws a single RAM bar and gives the rows below it to swap and
  the macOS memory zones, instead of repeating the same bar on every spare row.
  The processor box and the Memory/Temps row keep a fixed content height, and
  surplus rows go to the charts; a one-column gutter separates side-by-side
  usage bars.

### Fixed

- The default view's `ANE Usage` gauge always read 0 %: it was derived from the
  Energy Model's ANE power counter, which reports 0 on recent Apple Silicon
  even while the ANE runs.  It now uses the `ANE` IOReport channel's `status`
  residency (the share of the interval spent `Running`), which reads 0 % idle
  and near 100 % under a Core ML loop; the watts suffix appears only when a
  sample actually measured ANE energy.
- `drm-engine-capacity-*` lines are no longer parsed as engine time, so a
  capacity value cannot pollute the per-process GPU percentage.

## 3.1.0 — 2026-11-15

### Changed

- Views 1 and 2 are painted by blessed, sharing the ANSI palette and border
  helpers of views 3-5.  dashing (and the 0.1/0.2 compatibility layer around
  it) is gone: the layout tree lives in `macpm/widgets.py` and
  `macpm/blessed_engine.py` paints it.  `install_requires` now lists
  `blessed>=1.20` explicitly.
- The Linux process view ranks `/proc/<pid>/stat` by CPU ticks and builds rows
  only for the busiest 60 processes; names come from `cmdline` argv[0] and the
  PID column fits seven digits.

### Added

- View 2 shows a "GPU usage" hint and one bordered mini gauge per GPU on
  multi-GPU hosts (two per line), with a horizontal bar, `G3 48% P2 70W` and
  the same rolling three-second maximum as the GPU view.
- The GPU view gained `MEM`/`P` (P-state) columns and shows the rolling
  three-second maximum next to the instantaneous bar (`0% ↑48%`).  `ENC`/`DEC`
  are only shown on single-GPU hosts.
- Linux view 4 keeps the per-process traffic table (`ss -tanie`, TCP only) and
  adds a "root/sudo for all" hint when running without root; listeners are
  deduplicated by displayed address (`*:22` once).
- The first sample runs immediately instead of waiting one refresh interval;
  GPU identity/process data is warmed in the background.

### Fixed

- Charts scroll in from the right edge (newest sample on the right) and the
  painter clears stale cells, so shrinking bars no longer leave a horizontal
  streak.
- GPU model names and multi-card ownership: `_VendorProcessSampler` forwards
  `model` and `gpus` to `ProcessRow`.
- Linux `ss` parsing reads `ino:` from the socket header line (iproute2), so
  per-process traffic is no longer empty even as root.
- Wide PIDs and listener duplicates no longer break the network table.
- Resizing the terminal debounces for 0.35s and then redraws fully; the
  startup "collecting" text is gone.

## 3.0.2 — 2026-11-15

### Fixed

- macpm declares `dashing>=0.1,<0.2` again: dashing 0.2 replaced the
  `Tile._display(tbox, parent)` / `_draw_title(tbox, ...)` drawing protocol
  with a buffer-based one, and macpm's custom `FillVSplit`/`SensorPanel`
  layout overrides rely on the 0.1 signatures.  Installing on a machine that
  already had dashing 0.2 produced
  `TypeError: Tile._draw_borders_and_title() missing 1 required positional
  argument: 'tbox'`.  A runtime guard now names the problem and the fix
  (`pip install 'dashing<0.2'`) if 0.2 is forced in.
- The 3.0.1 colour compatibility stays in place, so the code is ready for a
  future port to the 0.2 API.

## 3.0.1 — 2026-11-15

### Fixed

- The colour switch (`←` / `→`) crashed with
  `AttributeError: can't set attribute 'border_color'` on dashing 0.2: that
  release turned tile colours into read-only RGB properties.  macpm now sets
  colours through `Tile.set_color` when it exists (translating the ANSI index
  to RGB) and keeps the plain attributes on dashing 0.1, so both generations
  work.
- The applied colour is tracked internally instead of read back from
  `border_color`; on dashing 0.2 that value is an RGB object, so the comparison
  was always true and cleared the screen on every frame.
- dashing 0.2 rejects ANSI index 8 in constructors; the eighth theme maps to
  grey there instead of raising `IndexError`.

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
