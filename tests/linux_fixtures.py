"""Build a fake /proc + /sys tree to exercise the Linux backend anywhere.

The Linux readers take a ``SysRoot`` prefix, so the whole backend can run on
macOS (or in CI) against a captured machine.  ``build(root, variant)`` writes
the interesting files for three kinds of machine:

* ``hybrid``  — x86 Intel hybrid (2 E + 4 P), RAPL package, coretemp/amdgpu
  hwmon, an amdgpu card and a few sockets.
* ``arm``     — arm64 big.LITTLE via ``cpu_capacity``, no RAPL, ``cpu_thermal``
  hwmon, no GPU.
* ``multi``   — four AMD cards with different load.
* ``empty``   — nothing; every reading must degrade to ``n/a``.
"""

from __future__ import annotations

import os
import shutil

CPU = "sys/devices/system/cpu"

CPUINFO_INTEL = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Core(TM) i7-12700H
cpu MHz\t\t: 1200.000

processor\t: 1
model name\t: Intel(R) Core(TM) i7-12700H
cpu MHz\t\t: 1200.000
"""

CPUINFO_ARM = """processor\t: 0
BogoMIPS\t: 48.00
Features\t: fp asimd evtstrm
Model\t\t: Radxa Rock 5B

processor\t: 1
BogoMIPS\t: 48.00
Model\t\t: Radxa Rock 5B
"""


def _write(root, relative, text):
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _link(root, relative, target):
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.islink(path) or os.path.exists(path):
        os.unlink(path)
    os.symlink(target, path)


def _cpu(root, index, core_type, capacity, cur_khz, max_khz, states=None):
    base = f"{CPU}/cpu{index}"
    if core_type is not None:
        _write(root, f"{base}/topology/core_type", f"{core_type}\n")
    if capacity is not None:
        _write(root, f"{base}/cpu_capacity", f"{capacity}\n")
    _write(root, f"{base}/cpufreq/scaling_cur_freq", f"{cur_khz}\n")
    _write(root, f"{base}/cpufreq/cpuinfo_max_freq", f"{max_khz}\n")
    if states is not None:
        text = "".join(f"{freq} {ticks}\n" for freq, ticks in states)
        _write(root, f"{base}/cpufreq/stats/time_in_state", text)


def _socket_files(root, variant):
    tcp = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode\n"
        # 127.0.0.1:8080 listening, inode 11111
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 "
        "00000000  1000        0 11111 1 0 0 0 0\n"
        # 127.0.0.1:8081 -> 127.0.0.1:80 established, inode 22222
        "   1: 0100007F:1F91 0100007F:0050 01 00000000:00000000 00:00000000 "
        "00000000  1000        0 22222 1 0 0 0 0\n"
    )
    tcp6 = (
        "  sl  local_address                         remote_address                        "
        "st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000000000000000000001000000:0050 00000000000000000000000000000000:0000 "
        "0A 00000000:00000000 00:00000000 00000000     0        0 33333 1 0 0 0 0\n"
    )
    udp = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode\n"
        "   0: 00000000:0035 00000000:0000 07 00000000:00000000 00:00000000 "
        "00000000     0        0 44444 2 0 0 0 0\n"
    )
    _write(root, "proc/net/tcp", tcp)
    _write(root, "proc/net/tcp6", tcp6)
    _write(root, "proc/net/udp", udp)
    if variant != "hybrid":
        return
    # Socket owners: nginx owns 11111 and 22222, resolved owns 44444.
    _write(root, "proc/4242/comm", "nginx\n")
    _link(root, "proc/4242/fd/3", "socket:[11111]")
    _link(root, "proc/4242/fd/4", "socket:[22222]")
    _link(root, "proc/4242/fd/5", "/dev/null")
    _write(root, "proc/4343/comm", "systemd-resolve\n")
    _link(root, "proc/4343/fd/3", "socket:[44444]")



def add_card(
    root,
    index,
    driver="amdgpu",
    vendor="0x1002",
    busy=37,
    freq=1200,
    power=12.0,
    vram=(2048.0, 8192.0),
    temp=43.0,
):
    """Add one DRM card so multi-GPU layouts can be replayed."""
    base = f"sys/class/drm/card{index}/device"
    _link(root, base + "/driver", f"../../../../bus/pci/drivers/{driver}")
    _write(root, base + "/vendor", vendor + "\n")
    if busy is not None:
        _write(root, base + "/gpu_busy_percent", f"{busy}\n")
    if freq:
        _write(root, base + "/pp_dpm_sclk", f"0: 500Mhz\n1: {freq}Mhz *\n")
    if power or temp:
        _write(root, f"{base}/hwmon/hwmon{10 + index}/name", f"{driver}\n")
    if power:
        _write(
            root,
            f"{base}/hwmon/hwmon{10 + index}/power1_average",
            f"{int(power * 1_000_000)}\n",
        )
    if vram:
        used, total = vram
        _write(root, base + "/mem_info_vram_used", f"{int(used * 1024 * 1024)}\n")
        _write(root, base + "/mem_info_vram_total", f"{int(total * 1024 * 1024)}\n")
    if temp:
        _write(
            root,
            f"{base}/hwmon/hwmon{10 + index}/temp1_input",
            f"{int(temp * 1000)}\n",
        )


def build(root, variant="hybrid"):
    root = str(root)
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    if variant == "empty":
        _write(root, "proc/cpuinfo", "")
        return root

    if variant == "hybrid":
        _write(root, "proc/cpuinfo", CPUINFO_INTEL)
        for index in range(2):
            _cpu(root, index, 0, 400, 1_200_000, 1_800_000,
                 states=[(1_200_000, 100), (1_800_000, 100)])
        for index in range(2, 6):
            _cpu(root, index, 1, 1024, 2_800_000, 4_200_000,
                 states=[(2_800_000, 50), (4_200_000, 150)])

        # RAPL package counter (1 J consumed so far)
        _write(root, "sys/class/powercap/intel-rapl:0/name", "package-0\n")
        _write(root, "sys/class/powercap/intel-rapl:0/energy_uj", "1000000\n")
        _write(root, "sys/class/powercap/intel-rapl:0/max_energy_range_uj",
               "262143328850\n")
        _write(root, "sys/class/powercap/intel-rapl:0/constraint_0_max_power_uw",
               "45000000\n")

        # hwmon: CPU package, GPU and a fallback board sensor
        _write(root, "sys/class/hwmon/hwmon0/name", "coretemp\n")
        _write(root, "sys/class/hwmon/hwmon0/temp1_label", "Package id 0\n")
        _write(root, "sys/class/hwmon/hwmon0/temp1_input", "52000\n")
        _write(root, "sys/class/hwmon/hwmon0/temp2_label", "Core 0\n")
        _write(root, "sys/class/hwmon/hwmon0/temp2_input", "48000\n")

        _write(root, "sys/class/hwmon/hwmon1/name", "amdgpu\n")
        _write(root, "sys/class/hwmon/hwmon1/temp1_label", "edge\n")
        _write(root, "sys/class/hwmon/hwmon1/temp1_input", "43000\n")
        _write(root, "sys/class/hwmon/hwmon1/fan1_input", "2318\n")
        _write(root, "sys/class/hwmon/hwmon1/fan1_max", "4000\n")
        _write(root, "sys/class/hwmon/hwmon1/power1_average", "12000000\n")

        # AMD GPU card
        _link(root, "sys/class/drm/card0/device/driver",
              "../../../../bus/pci/drivers/amdgpu")
        _write(root, "sys/class/drm/card0/device/vendor", "0x1002\n")
        _write(root, "sys/class/drm/card0/device/gpu_busy_percent", "37\n")
        _write(root, "sys/class/drm/card0/device/mem_info_vram_used",
               "2147483648\n")
        _write(root, "sys/class/drm/card0/device/mem_info_vram_total",
               "8589934592\n")
        _write(root, "sys/class/drm/card0/device/pp_dpm_sclk",
               "0: 500Mhz\n1: 1200Mhz *\n")
        _write(root, "sys/class/drm/card0/device/hwmon/hwmon3/power1_average",
               "12000000\n")
        _write(root, "sys/class/drm/card0/device/hwmon/hwmon3/temp1_input",
               "43000\n")

        _write(root, "proc/pressure/cpu",
               "some avg10=12.50 avg60=5.00 avg300=1.00 total=12345\n"
               "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        _socket_files(root, variant)
        return root

    if variant == "multi":
        # Four AMD cards with different load, as on a small GPU server.
        _write(root, "proc/cpuinfo", CPUINFO_INTEL)
        for index in range(2):
            _cpu(root, index, None, None, 2_000_000, 3_000_000)
        for index in range(4):
            add_card(
                root,
                index,
                busy=10 + index * 25,
                freq=900 + index * 100,
                temp=40.0 + index * 3,
                vram=(1024.0 * (index + 1), 8192.0),
            )
        _socket_files(root, variant)
        return root

    if variant == "arm":
        _write(root, "proc/cpuinfo", CPUINFO_ARM)
        _write(root, "sys/firmware/devicetree/base/model", "Radxa Rock 5B\x00")
        for index in range(4):
            _cpu(root, index, None, 400, 1_000_000, 1_800_000)
        for index in range(4, 8):
            _cpu(root, index, None, 1024, 2_000_000, 2_400_000)
        _write(root, "sys/class/hwmon/hwmon0/name", "cpu_thermal\n")
        _write(root, "sys/class/hwmon/hwmon0/temp1_input", "46000\n")
        _write(root, "sys/class/thermal/thermal_zone0/type", "cpu-thermal\n")
        _write(root, "sys/class/thermal/thermal_zone0/temp", "46000\n")
        _socket_files(root, variant)
        return root

    raise ValueError(f"unknown fixture variant: {variant}")


SS_OUTPUT = """State Recv-Q Send-Q Local Address:Port Peer Address:Port
ESTAB 0 0 127.0.0.1:8081 127.0.0.1:80
	 skmem:(r0,rb131072,t0,tb46080,f0,w0,o0,bl0,d0) ts sack cubic
	 rto:204 rtt:0.5/0.25 ato:40 mss:1448 cwnd:10 bytes_acked:500 bytes_received:1200
	 ino:22222 sk:1 <-> sk:2
ESTAB 0 0 10.0.0.5:50000 93.184.216.34:443
	 bytes_acked:900 bytes_received:4000 ino:55555
	 ino:55555
"""

if __name__ == "__main__":  # pragma: no cover - manual fixture dump
    import sys

    print(build(sys.argv[1] if len(sys.argv) > 1 else "/tmp/macpm-fake",
                sys.argv[2] if len(sys.argv) > 2 else "hybrid"))
