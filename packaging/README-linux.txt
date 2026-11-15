macpm 3.1 - performance monitor for Linux
=========================================

No Python installation is required: the binary bundles its own runtime, so it
runs on any x86-64 (amd64) or arm64 (aarch64) Linux with glibc 2.17 or newer
(CentOS 7 / Ubuntu 16.04 / Debian 9 and later).  The system's libgcc_s is used
at run time; it is part of the base system on every mainstream distribution
(libgcc-s1 on Debian/Ubuntu, libgcc on RHEL/SUSE).

Run without installing
----------------------
    ./macpm

Install
-------
System-wide (needs root):

    sudo install -m 755 macpm /usr/local/bin/macpm

For one user only:

    install -D -m 755 macpm ~/.local/bin/macpm

On Debian/Ubuntu the matching .deb from the same release does the same thing:

    sudo dpkg -i macpm_3.1.0_amd64.deb     # or _arm64.deb

Options
-------
    --interval SECONDS   sampling / refresh interval (default 1)
    --color 1-8          colour (default 2)
    --avg SECONDS        averaging window for power values (default 30)
    --show_cores [BOOL]  start in the per-core view
    --no-public-ip       do not look up the public IP and location
    --version            print the version

Keys
----
    q / ESC             quit
    <- / ->             change colour
    1 / 2 / 3 / 4 / 5   default / per-core / process / network / GPU view
    s                   cycle the sort column
    Up / Down           scroll
    Ctrl+R              reset peaks

What needs extra tools
----------------------
    root                RAPL CPU power, other users' sockets, other processes'
                        GPU time (DRM fdinfo)
    iproute2 (`ss`)     per-process network traffic (TCP only)
    nvidia-smi          NVIDIA GPU utilisation, clocks, power, VRAM
    ixsmi / dlsmi       Iluvatar / Denglin accelerators
    teco-smi            Techo accelerators

Without them the affected panels show n/a; the CPU, memory, disk and network
rates come from psutil and always work.

Troubleshooting
---------------
    macpm --version                 # binary starts at all?
    macpm --help
    ldd ./macpm                     # libc, libdl, libgcc_s (from the system)

The binary extracts its Python runtime into a temporary directory on start; if
/tmp is mounted noexec, point TMPDIR at a writable, executable filesystem:

    TMPDIR=$HOME/.cache/macpm ./macpm
