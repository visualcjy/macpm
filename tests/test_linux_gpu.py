import shutil
import tempfile
import unittest
from unittest import mock

from macpm.linux_backend.gpu import (
    GpuBackend,
    GpuMonitor,
    GpuSample,
    reset_shared,
)
from macpm.linux_backend.proc import per_process_gpu_ns, scan_drm_clients
from macpm.linux_backend.sysfs import SysRoot

from tests import linux_fixtures


class AmdGpuTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "hybrid")
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_detection(self):
        backend = GpuBackend.detect(self.sys)

        self.assertTrue(backend.available)
        self.assertEqual(backend.driver, "amdgpu")
        self.assertEqual(backend.name, "AMD GPU (amdgpu)")
        self.assertTrue(backend.per_process_supported)

    def test_sample(self):
        backend = GpuBackend.detect(self.sys)
        sample = backend.sample(1.0)

        self.assertEqual(sample.busy_pct, 37.0)
        self.assertEqual(sample.freq_mhz, 1200.0)
        self.assertEqual(sample.power_w, 12.0)
        self.assertEqual(sample.mem_used_mb, 2048.0)
        self.assertEqual(sample.mem_total_mb, 8192.0)
        self.assertEqual(sample.temp_c, 43.0)

    def test_capabilities(self):
        caps = GpuBackend.detect(self.sys).capabilities()

        self.assertEqual(
            caps, {"util": True, "freq": True, "power": True, "proc": True}
        )

    def test_no_gpu(self):
        linux_fixtures.build(self.root, "arm")
        backend = GpuBackend.detect(SysRoot(self.root))

        self.assertFalse(backend.available)
        self.assertFalse(backend.sample(1.0).available)
        self.assertEqual(
            backend.capabilities(),
            {"util": False, "freq": False, "power": False, "proc": False},
        )


class IntelFdinfoTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "empty")
        base = "sys/class/drm/card1/device"
        linux_fixtures._link(
            self.root, base + "/driver", "../../../../bus/pci/drivers/i915"
        )
        linux_fixtures._write(self.root, base + "/vendor", "0x8086\n")
        linux_fixtures._write(self.root, base + "/gt_cur_freq_mhz", "850\n")
        linux_fixtures._write(self.root, base + "/gt_max_freq_mhz", "1300\n")
        linux_fixtures._write(self.root, "proc/777/comm", "blender\n")
        linux_fixtures._link(self.root, "proc/777/fd/9", "/dev/dri/renderD128")
        self.fdinfo = "proc/777/fdinfo/9"
        self._write_fdinfo(1_000_000_000, client=1)
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_fdinfo(self, nanos, client=1):
        linux_fixtures._write(
            self.root,
            self.fdinfo,
            "pos:\t0\nflags:\t02100002\nmnt_id:\t24\nino:\t1234\n"
            "drm-driver:\ti915\ndrm-client-id:\t{}\n"
            "drm-engine-render:\t{} ns\ndrm-engine-copy:\t0 ns\n".format(
                client, nanos
            ),
        )

    def test_detection_and_frequency(self):
        backend = GpuBackend.detect(self.sys)
        sample = backend.sample(1.0)

        self.assertEqual(backend.driver, "i915")
        self.assertEqual(sample.freq_mhz, 850.0)
        self.assertEqual(sample.max_freq_mhz, 1300.0)
        # The first sample only establishes the baseline.
        self.assertIsNone(sample.busy_pct)

    def test_engine_delta_becomes_utilisation(self):
        backend = GpuBackend.detect(self.sys)
        backend.scan_interval = 0.0
        backend.sample(1.0)
        self._write_fdinfo(1_500_000_000)

        sample = backend.sample(1.0)

        self.assertEqual(sample.busy_pct, 50.0)

    def test_per_process_ns_uses_the_busiest_engine(self):
        totals = per_process_gpu_ns(self.sys)

        self.assertEqual(totals[777], 1_000_000_000)

    def test_duplicate_fds_do_not_double_count(self):
        linux_fixtures._link(self.root, "proc/777/fd/10", "/dev/dri/renderD128")
        linux_fixtures._write(
            self.root,
            "proc/777/fdinfo/10",
            "drm-client-id:\t1\ndrm-engine-render:\t1000000000 ns\n",
        )

        clients = scan_drm_clients(self.sys)

        self.assertEqual(clients[777]["render"], 1_000_000_000)


class MultiGpuTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "multi")
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_every_card_is_enumerated(self):
        monitor = GpuMonitor.detect(self.sys)

        self.assertEqual(monitor.count, 4)
        self.assertTrue(monitor.available)
        self.assertEqual(monitor.driver, "amdgpu")

    def test_samples_keep_card_order_and_index(self):
        monitor = GpuMonitor.detect(self.sys)
        samples = monitor.samples(1.0)

        self.assertEqual([sample.index for sample in samples], [0, 1, 2, 3])
        self.assertEqual([sample.busy_pct for sample in samples], [10.0, 35.0, 60.0, 85.0])
        self.assertEqual([sample.freq_mhz for sample in samples], [900.0, 1000.0, 1100.0, 1200.0])
        self.assertEqual([sample.temp_c for sample in samples], [40.0, 43.0, 46.0, 49.0])

    def test_primary_is_the_busiest_card(self):
        monitor = GpuMonitor.detect(self.sys)
        primary = monitor.primary(monitor.samples(1.0))

        self.assertEqual(primary.index, 3)
        self.assertEqual(primary.busy_pct, 85.0)

    def test_single_card_convenience_sample_still_works(self):
        monitor = GpuMonitor.detect(self.sys)

        self.assertEqual(monitor.sample(1.0).busy_pct, 85.0)

    def test_capabilities_are_the_union_of_cards(self):
        monitor = GpuMonitor.detect(self.sys)

        self.assertEqual(
            monitor.capabilities(),
            {"util": True, "freq": True, "power": True, "proc": True},
        )


class NvidiaMultiTests(unittest.TestCase):
    ROWS = "\n".join(
        [
            "0, NVIDIA A100-SXM4-80GB, 87, 1410, 312.4, 71000, 81920, 58",
            "1, NVIDIA A100-SXM4-80GB, 12, 1410, 98.5, 2048, 81920, 41",
        ]
    )

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "empty")
        linux_fixtures._write(self.root, "proc/driver/nvidia/gpus/0000:01:00.0/information",
                              "Model: NVIDIA A100-SXM4-80GB\n")
        linux_fixtures._write(self.root, "proc/driver/nvidia/gpus/0000:02:00.0/information",
                              "Model: NVIDIA A100-SXM4-80GB\n")
        self.sys = SysRoot(self.root)
        from macpm.linux_backend import gpu as module

        module.reset_nvidia_cache()

    def tearDown(self):
        from macpm.linux_backend import gpu as module

        module.reset_nvidia_cache()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_each_card_reads_its_own_row(self):
        from macpm.linux_backend import gpu as module

        with mock.patch.object(module, "_run_smi", return_value=self.ROWS):
            monitor = GpuMonitor.detect(self.sys)
            samples = monitor.samples(1.0)

        self.assertEqual(monitor.count, 2)
        self.assertEqual([sample.index for sample in samples], [0, 1])
        self.assertEqual([sample.busy_pct for sample in samples], [87.0, 12.0])
        self.assertEqual(samples[0].mem_total_mb, 81920.0)
        self.assertEqual(samples[1].power_w, 98.5)
        self.assertEqual(samples[0].name, "NVIDIA A100-SXM4-80GB")


class GenericAcceleratorTests(unittest.TestCase):
    """Unknown drivers are listed and probed for the usual counter names."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "empty")
        for index in range(2):
            linux_fixtures.add_card(
                self.root, index, driver="accel", busy=None, freq=None,
                power=30.0, vram=(4096.0, 32768.0), temp=55.0,
            )
            linux_fixtures._write(
                self.root,
                f"sys/class/drm/card{index}/device/utilization",
                f"{40 + index * 20}\n",
            )
            linux_fixtures._write(
                self.root,
                f"sys/class/drm/card{index}/device/devfreq/accel/cur_freq",
                "1500000000\n",
            )
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_both_cards_are_listed_with_their_counters(self):
        monitor = GpuMonitor.detect(self.sys)
        samples = monitor.samples(1.0)

        self.assertEqual(monitor.count, 2)
        self.assertEqual([sample.busy_pct for sample in samples], [40.0, 60.0])
        self.assertEqual([sample.freq_mhz for sample in samples], [1500.0, 1500.0])
        self.assertEqual(samples[0].mem_total_mb, 32768.0)
        self.assertEqual(samples[0].temp_c, 55.0)
        self.assertEqual(samples[0].power_w, 30.0)

    def test_capabilities_reflect_the_probed_files(self):
        monitor = GpuMonitor.detect(self.sys)

        self.assertEqual(
            monitor.capabilities(),
            {"util": True, "freq": True, "power": True, "proc": False},
        )


class SharedBackendTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "hybrid")
        reset_shared()

    def tearDown(self):
        reset_shared()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_shared_backend_is_cached(self):
        from macpm.linux_backend.gpu import shared_backend

        first = shared_backend(SysRoot(self.root))
        second = shared_backend(SysRoot(self.root))

        self.assertIs(first, second)
        reset_shared()
        self.assertIsNot(shared_backend(SysRoot(self.root)), first)


if __name__ == "__main__":
    unittest.main()


class PrimaryTests(unittest.TestCase):
    def test_zero_busy_cards_are_ranked_by_power_then_clock(self):
        samples = [
            GpuSample(available=True, index=0, busy_pct=0, power_w=20, freq_mhz=210),
            GpuSample(available=True, index=3, busy_pct=0, power_w=70, freq_mhz=2520),
        ]

        self.assertEqual(GpuMonitor.primary(samples).index, 3)

    def test_busy_percentage_still_wins(self):
        samples = [
            GpuSample(available=True, index=0, busy_pct=40, power_w=30, freq_mhz=900),
            GpuSample(available=True, index=1, busy_pct=5, power_w=90, freq_mhz=2500),
        ]

        self.assertEqual(GpuMonitor.primary(samples).index, 0)


# -- DRM memory attribution ------------------------------------------------


def amd_fdinfo(
    pdev="0000:01:00.0",
    vram_kib=0,
    gtt_kib=0,
    shared_vram=0,
    shared_gtt=0,
    client=1,
    gfx_ns=0,
    capacity=True,
):
    text = (
        "drm-driver:\tamdgpu\n"
        f"drm-client-id:\t{client}\n"
        f"drm-pdev:\t{pdev}\n"
        f"drm-resident-vram:\t{vram_kib} KiB\n"
        f"drm-shared-vram:\t{shared_vram} KiB\n"
        f"drm-resident-gtt:\t{gtt_kib} KiB\n"
        f"drm-shared-gtt:\t{shared_gtt} KiB\n"
        "drm-resident-system:\t0 KiB\n"
        f"drm-engine-gfx:\t{gfx_ns} ns\n"
    )
    if capacity:
        text += "drm-engine-capacity-video:\t2\n"
    return text


def write_client(root, pid, fd, target, fdinfo):
    linux_fixtures._write(root, f"proc/{pid}/comm", "worker\n")
    linux_fixtures._link(root, f"proc/{pid}/fd/{fd}", target)
    linux_fixtures._write(root, f"proc/{pid}/fdinfo/{fd}", fdinfo)


class FdinfoMemoryTests(unittest.TestCase):
    def test_amdgpu_memory_and_engines_are_parsed(self):
        from macpm.linux_backend.proc import parse_fdinfo

        client = parse_fdinfo(
            amd_fdinfo(vram_kib=2 * 1024 * 1024, gtt_kib=1024 * 1024, gfx_ns=1234)
        )

        self.assertEqual(client["driver"], "amdgpu")
        self.assertEqual(client["pdev"], "0000:01:00.0")
        self.assertEqual(client["vram"], 2 * 1024 ** 3)
        self.assertEqual(client["gtt"], 1024 ** 3)
        self.assertEqual(client["shared"], 0)
        self.assertEqual(client["engines"], {"render": 1234})

    def test_engine_capacity_is_not_an_engine(self):
        from macpm.linux_backend.proc import parse_fdinfo

        client = parse_fdinfo(amd_fdinfo())

        self.assertNotIn("capacity-video", client["engines"])

    def test_missing_shared_counter_keeps_the_gate_closed(self):
        from macpm.linux_backend.proc import parse_fdinfo

        text = amd_fdinfo().replace("drm-shared-gtt:\t0 KiB\n", "")
        client = parse_fdinfo(text)

        self.assertIsNone(client["shared"])

    def test_integrated_gpu_has_no_local_memory(self):
        from macpm.linux_backend.proc import parse_fdinfo

        client = parse_fdinfo(
            "drm-driver:\ti915\n"
            "drm-client-id:\t3\n"
            "drm-pdev:\t0000:00:02.0\n"
            "drm-resident-system0:\t2048 KiB\n"
            "drm-shared-system0:\t0 KiB\n"
            "drm-resident-stolen-system0:\t512 KiB\n"
            "drm-engine-render:\t10 ns\n"
        )

        self.assertIsNone(client["vram"])
        self.assertEqual(client["gtt"], (2048 + 512) * 1024)
        self.assertEqual(client["shared"], 0)

    def test_amd_resident_system_is_not_charged_as_gpu_memory(self):
        from macpm.linux_backend.proc import parse_fdinfo

        client = parse_fdinfo(
            amd_fdinfo(vram_kib=1024).replace(
                "drm-resident-system:\t0 KiB", "drm-resident-system:\t4096 KiB"
            )
        )

        self.assertEqual(client["vram"], 1024 * 1024)
        self.assertEqual(client["gtt"], 0)


class AttributionTests(unittest.TestCase):
    """The private-client gate: charge bytes only when the kernel proves them."""

    def _scan(self, clients, partial=False, unreadable=None):
        from macpm.linux_backend.proc import DrmScan

        return DrmScan(list(clients), partial=partial, unreadable=unreadable or set())

    def _client(self, pid=111, client_id="1", vram=2048, gtt=512, shared=0,
                render_node=True, pdev="0000:01:00.0"):
        from macpm.linux_backend.proc import DrmClient

        return DrmClient(
            pid=pid, client_id=client_id, pdev=pdev, driver="amdgpu",
            render_node=render_node, vram=vram, gtt=gtt, shared=shared,
            engines={"render": 10},
        )

    def test_private_client_is_charged(self):
        from macpm.linux_backend.proc import attribute_drm_clients

        record = attribute_drm_clients(self._scan([self._client()]), "0000:01:00.0")[111]

        self.assertEqual(record["vram_bytes"], 2048)
        self.assertEqual(record["gtt_bytes"], 512)
        self.assertEqual(record["reason"], "")

    def test_integrated_gpu_charges_its_gtt(self):
        from macpm.linux_backend.proc import attribute_drm_clients

        client = self._client(vram=None, gtt=4096)
        record = attribute_drm_clients(self._scan([client]), "")[111]

        self.assertIsNone(record["vram_bytes"])
        self.assertEqual(record["gtt_bytes"], 4096)
        self.assertEqual(record["reason"], "")

    def test_shared_buffers_are_not_charged(self):
        from macpm.linux_backend.proc import REASON_SHARED_BUFFERS, attribute_drm_clients

        record = attribute_drm_clients(
            self._scan([self._client(shared=4096)]), "0000:01:00.0"
        )[111]

        self.assertIsNone(record["vram_bytes"])
        self.assertEqual(record["reason"], REASON_SHARED_BUFFERS)

    def test_shared_client_is_not_charged(self):
        from macpm.linux_backend.proc import REASON_SHARED_CLIENT, attribute_drm_clients

        scan = self._scan([self._client(pid=111), self._client(pid=222)])
        records = attribute_drm_clients(scan, "0000:01:00.0")

        self.assertEqual(records[111]["reason"], REASON_SHARED_CLIENT)
        self.assertEqual(records[222]["reason"], REASON_SHARED_CLIENT)

    def test_primary_node_is_not_charged(self):
        from macpm.linux_backend.proc import REASON_PRIMARY, attribute_drm_clients

        record = attribute_drm_clients(
            self._scan([self._client(render_node=False)]), "0000:01:00.0"
        )[111]

        self.assertEqual(record["reason"], REASON_PRIMARY)

    def test_unknown_shared_counter_is_legacy(self):
        from macpm.linux_backend.proc import REASON_LEGACY, attribute_drm_clients

        record = attribute_drm_clients(
            self._scan([self._client(shared=None)]), "0000:01:00.0"
        )[111]

        self.assertEqual(record["reason"], REASON_LEGACY)

    def test_partial_scan_is_not_charged(self):
        from macpm.linux_backend.proc import REASON_PARTIAL, attribute_drm_clients

        record = attribute_drm_clients(
            self._scan([self._client()], partial=True), "0000:01:00.0"
        )[111]

        self.assertEqual(record["reason"], REASON_PARTIAL)

    def test_unreadable_fds_are_not_charged(self):
        from macpm.linux_backend.proc import REASON_INCOMPLETE, attribute_drm_clients

        record = attribute_drm_clients(
            self._scan([self._client()], unreadable={111}), "0000:01:00.0"
        )[111]

        self.assertEqual(record["reason"], REASON_INCOMPLETE)

    def test_other_cards_clients_are_ignored(self):
        from macpm.linux_backend.proc import attribute_drm_clients

        scan = self._scan(
            [self._client(pid=111), self._client(pid=222, client_id="2", pdev="0000:02:00.0")]
        )
        records = attribute_drm_clients(scan, "0000:01:00.0")

        self.assertIn(111, records)
        self.assertNotIn(222, records)


class DrmCardAttributionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "empty")
        linux_fixtures.add_card(self.root, 0, vram=(1024.0, 8192.0))
        linux_fixtures.add_card(self.root, 1, vram=(2048.0, 8192.0))
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_processes_land_on_the_card_their_client_belongs_to(self):
        write_client(
            self.root, 111, 3, "/dev/dri/renderD128",
            amd_fdinfo("0000:01:00.0", vram_kib=2 * 1024 * 1024, gfx_ns=1),
        )
        write_client(
            self.root, 222, 4, "/dev/dri/renderD129",
            amd_fdinfo("0000:02:00.0", vram_kib=4 * 1024 * 1024, gfx_ns=1, client=2),
        )
        monitor = GpuMonitor.detect(self.sys)

        details = monitor.process_details()

        self.assertEqual(details[111]["cards"], [0])
        self.assertEqual(details[111]["vram_bytes"], 2 * 1024 ** 3)
        self.assertEqual(details[222]["cards"], [1])
        self.assertEqual(details[222]["vram_bytes"], 4 * 1024 ** 3)

    def test_a_process_on_two_cards_keeps_both(self):
        write_client(
            self.root, 111, 3, "/dev/dri/renderD128",
            amd_fdinfo("0000:01:00.0", vram_kib=1024 * 1024, gfx_ns=1),
        )
        write_client(
            self.root, 111, 4, "/dev/dri/renderD128",
            amd_fdinfo("0000:02:00.0", vram_kib=2 * 1024 * 1024, gfx_ns=9, client=2),
        )
        monitor = GpuMonitor.detect(self.sys)

        details = monitor.process_details()

        self.assertEqual(details[111]["cards"], [0, 1])
        self.assertEqual(details[111]["vram_bytes"], 3 * 1024 ** 3)
        # One row, busiest card wins: 9 ns beats 1 ns.
        self.assertEqual(monitor.per_process()[111], 9)

    def test_client_held_by_two_processes_is_unattributed(self):
        write_client(
            self.root, 111, 3, "/dev/dri/renderD128",
            amd_fdinfo("0000:01:00.0", vram_kib=1024, client=7),
        )
        write_client(
            self.root, 222, 5, "/dev/dri/renderD128",
            amd_fdinfo("0000:01:00.0", vram_kib=1024, client=7),
        )
        monitor = GpuMonitor.detect(self.sys)

        details = monitor.process_details()

        self.assertEqual(details[111]["reason"], "shared_client")
        self.assertIsNone(details[111]["vram_bytes"])
        self.assertIsNone(details[222]["vram_bytes"])

    def test_card_node_client_is_unattributed(self):
        write_client(
            self.root, 111, 3, "/dev/dri/card0",
            amd_fdinfo("0000:01:00.0", vram_kib=1024),
        )
        monitor = GpuMonitor.detect(self.sys)

        self.assertEqual(monitor.process_details()[111]["reason"], "primary")

    def test_samples_walk_fdinfo_once_for_every_card(self):
        from macpm.linux_backend import gpu as module

        write_client(
            self.root, 111, 3, "/dev/dri/renderD128",
            amd_fdinfo("0000:01:00.0", gfx_ns=1),
        )
        calls = []
        real = module.scan_drm

        def counting(sysroot, budget=0.75):
            calls.append(sysroot)
            return real(sysroot, budget=budget)

        with mock.patch.object(module, "scan_drm", side_effect=counting):
            monitor = GpuMonitor.detect(self.sys)
            monitor.samples(1.0)

        self.assertEqual(len(calls), 1)


class IntelCardSplitTests(unittest.TestCase):
    """drm-pdev decides which card a client's engine time belongs to."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-gpu-")
        linux_fixtures.build(self.root, "empty")
        for index in range(2):
            linux_fixtures.add_card(self.root, index, driver="i915", vram=None)
        self.sys = SysRoot(self.root)
        write_client(
            self.root, 111, 3, "/dev/dri/renderD128",
            "drm-driver:\ti915\ndrm-client-id:\t1\n"
            "drm-pdev:\t0000:01:00.0\ndrm-engine-render:\t1000000000 ns\n",
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_each_card_reads_its_own_clients(self):
        monitor = GpuMonitor.detect(self.sys)
        for backend in monitor.backends:
            backend.scan_interval = 0.0
        monitor.samples(1.0)
        write_client(
            self.root, 111, 3, "/dev/dri/renderD128",
            "drm-driver:\ti915\ndrm-client-id:\t1\n"
            "drm-pdev:\t0000:01:00.0\ndrm-engine-render:\t1500000000 ns\n",
        )

        samples = monitor.samples(1.0)

        self.assertEqual(samples[0].busy_pct, 50.0)
        self.assertEqual(samples[1].busy_pct, 0.0)

    def test_without_pdev_neither_card_claims_the_aggregate(self):
        for index in range(2):
            linux_fixtures._write(
                self.root, f"sys/class/drm/card{index}/device/uevent",
                "DRIVER=i915\n",
            )
        write_client(
            self.root, 111, 3, "/dev/dri/renderD128",
            "drm-driver:\ti915\ndrm-client-id:\t1\n"
            "drm-engine-render:\t1000000000 ns\n",
        )
        monitor = GpuMonitor.detect(self.sys)

        samples = monitor.samples(1.0)

        self.assertEqual([sample.busy_pct for sample in samples], [None, None])
