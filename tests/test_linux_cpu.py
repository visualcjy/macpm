import os
import shutil
import tempfile
import unittest

from macpm.linux_backend.cpu import CpuSampler, classify_cores
from macpm.linux_backend.sysfs import SysRoot

from tests import linux_fixtures


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-cpu-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_intel_hybrid_split_uses_core_type(self):
        linux_fixtures.build(self.root, "hybrid")
        cores, method = classify_cores(SysRoot(self.root))

        self.assertEqual(method, "core_type")
        self.assertEqual(len(cores), 6)
        self.assertEqual(sum(1 for core in cores if core.cluster == "E"), 2)
        self.assertEqual(sum(1 for core in cores if core.cluster == "P"), 4)

    def test_arm_big_little_uses_cpu_capacity(self):
        linux_fixtures.build(self.root, "arm")
        cores, method = classify_cores(SysRoot(self.root))

        self.assertEqual(method, "capacity")
        self.assertEqual(sum(1 for core in cores if core.cluster == "E"), 4)
        self.assertEqual(sum(1 for core in cores if core.cluster == "P"), 4)

    def test_uniform_machine_has_one_cluster(self):
        for index in range(4):
            base = f"sys/devices/system/cpu/cpu{index}"
            linux_fixtures._write(
                self.root, f"{base}/cpufreq/cpuinfo_max_freq", "3000000\n"
            )
            linux_fixtures._write(
                self.root, f"{base}/cpufreq/scaling_cur_freq", "3000000\n"
            )

        cores, method = classify_cores(SysRoot(self.root))

        self.assertEqual(method, "uniform")
        self.assertTrue(all(core.cluster == "P" for core in cores))

    def test_clusters_expose_macpm_shape(self):
        linux_fixtures.build(self.root, "hybrid")
        sampler = CpuSampler(SysRoot(self.root))
        sampler.prime()
        clusters = sampler.sample()

        names = [cluster["name"] for cluster in clusters]
        self.assertEqual(names, ["E-Cluster", "P-Cluster"])
        e_cluster, p_cluster = clusters
        self.assertEqual(len(e_cluster["cpus"]), 2)
        self.assertEqual(len(p_cluster["cpus"]), 4)
        self.assertEqual(p_cluster["freq_hz"], 2_800_000_000)
        self.assertEqual(e_cluster["freq_hz"], 1_200_000_000)
        for cpu in e_cluster["cpus"]:
            self.assertLessEqual(cpu["idle_ratio"], 1.0)
            self.assertGreaterEqual(cpu["idle_ratio"], 0.0)


class FrequencyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-cpu-")
        linux_fixtures.build(self.root, "hybrid")
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _advance_time_in_state(self, cpu, states):
        text = "".join(f"{freq} {ticks}\n" for freq, ticks in states)
        linux_fixtures._write(
            self.root,
            f"sys/devices/system/cpu/cpu{cpu}/cpufreq/stats/time_in_state",
            text,
        )

    def test_residency_weighted_frequency(self):
        sampler = CpuSampler(self.sys)
        sampler.prime()

        # 100 extra ticks at 1200 MHz and 100 at 1800 MHz -> 1500 MHz.
        self._advance_time_in_state(0, [(1_200_000, 200), (1_800_000, 200)])
        self._advance_time_in_state(1, [(1_200_000, 200), (1_800_000, 200)])

        clusters = sampler.sample()
        e_cluster = clusters[0]

        self.assertEqual(e_cluster["name"], "E-Cluster")
        self.assertEqual(e_cluster["freq_hz"], 1_500_000_000)

    def test_missing_time_in_state_falls_back_to_scaling_cur_freq(self):
        shutil.rmtree(os.path.join(self.root, "sys/devices/system/cpu/cpu0/cpufreq/stats"))
        shutil.rmtree(os.path.join(self.root, "sys/devices/system/cpu/cpu1/cpufreq/stats"))

        sampler = CpuSampler(self.sys)
        sampler.prime()
        clusters = sampler.sample()

        self.assertEqual(clusters[0]["freq_hz"], 1_200_000_000)


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-cpu-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_frequency_is_detected(self):
        linux_fixtures.build(self.root, "hybrid")

        self.assertTrue(CpuSampler(SysRoot(self.root)).has_frequency())

    def test_frequency_is_not_detected_without_cpufreq(self):
        linux_fixtures.build(self.root, "empty")

        self.assertFalse(CpuSampler(SysRoot(self.root)).has_frequency())


if __name__ == "__main__":
    unittest.main()
