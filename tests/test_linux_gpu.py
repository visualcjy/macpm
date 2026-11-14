import shutil
import tempfile
import unittest
from unittest import mock

from macpm.linux_backend.gpu import GpuBackend, GpuMonitor, reset_shared
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
