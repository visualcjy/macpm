"""macOS-only regression tests for the IOReport sampler.

The Linux port makes the macOS bindings lazy; these tests fail loudly if a
subscription silently turns into ``None`` (which used to make every gauge show
a plausible-looking but fake value).
"""

import sys
import unittest

from macpm.backend import create_sampler


@unittest.skipUnless(sys.platform == "darwin", "needs IOReport (macOS)")
class MacSamplerTests(unittest.TestCase):
    def test_subscriptions_are_live(self):
        sampler = create_sampler(1)
        try:
            self.assertIsNotNone(sampler._cpu_complex)
            self.assertIsNotNone(sampler._cpu_cores)
            self.assertIsNotNone(sampler._gpu)
            self.assertIsNotNone(sampler._energy)
        finally:
            sampler.close()

    def test_sample_has_clusters_and_dvfs_frequencies(self):
        sampler = create_sampler(1)
        try:
            sampler.prime()
            sample = sampler.sample()
        finally:
            sampler.close()

        clusters = sample["processor"]["clusters"]
        names = [cluster["name"] for cluster in clusters]
        self.assertIn("P-Cluster", names)
        p_cluster = next(c for c in clusters if c["name"] == "P-Cluster")
        # A DVFS table was parsed, so the cluster reports a real frequency.
        self.assertGreater(p_cluster["freq_hz"], 0)
        self.assertTrue(p_cluster["cpus"])

    def test_importing_the_sampler_stays_lazy(self):
        import os
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        environment = dict(os.environ)
        environment["PYTHONPATH"] = root
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, macpm.sampler, macpm.macpm, macpm.sensors; "
                "assert 'macpm.ioreport' not in sys.modules; "
                "assert 'macpm.smc' not in sys.modules",
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_gpu_reader_is_wired(self):
        from macpm.backend import process_gpu_reader
        from macpm.processes import ProcessSampler

        reader = process_gpu_reader()
        self.assertTrue(callable(reader))
        self.assertIsInstance(reader(), dict)

        sampler = ProcessSampler()
        self.assertTrue(sampler.gpu_available)
        self.assertIsInstance(sampler._gpu_times(), dict)


if __name__ == "__main__":
    unittest.main()
