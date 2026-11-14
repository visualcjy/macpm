macpm 3.0 - rootless performance monitor for Apple Silicon
==========================================================

No Python installation is required.  The binaries bundle their own runtime.

Requirements
------------
- Apple Silicon Mac (M1/M2/M3/M4/M5)
- macOS 12 or later (tested on macOS 27.2, M3 Max)

Run without installing
----------------------
Double-click "macpm.command" (opens Terminal), or from Terminal:

    ./macpm

Install into ~/.local/bin
-------------------------
    ./install.sh

Then run:

    macpm

If ~/.local/bin is not on PATH, follow the printed instructions.

Temperatures and fans
---------------------
The default view shows a "Temps & Fans" box with the hottest CPU and GPU
sensors and the current fan speeds.  They are read from the SMC, read-only,
without root.

Options / keys
--------------
    --interval SECONDS   sampling / refresh interval (default 1)
    --color 1-8          colour (default 2)
    --avg SECONDS        averaging window (default 30)
    --show_cores         start in the per-core view
    --version            print the version

    q / ESC   quit
    <- / ->   change colour
    1 / 2 / 3 default / per-core / process view
    s         cycle process sort column
    Up / Down scroll process list
    Space     pause / resume process list
    Ctrl+R    reset peaks

Gatekeeper
----------
The binaries are not signed with an Apple Developer ID.  If macOS blocks a
downloaded copy, either right-click "macpm.command" and choose Open, or run:

    xattr -d com.apple.quarantine ./macpm
    xattr -d com.apple.quarantine ./macpm.command

Only do this for a build you trust.

Data sources
------------
CPU/GPU utilisation, frequencies and power come from IOReport (the same
counters powermetrics uses), RAM/disk/network from psutil, and thermal state
from pmset.  macpm never calls sudo or powermetrics.

Compatibility
-------------
M1-M4 are the primary target.  M5/M5 Pro/Max need additional CPU tier/channel
support; Intel Macs are not supported.  See README.md in the source tree for
details.
