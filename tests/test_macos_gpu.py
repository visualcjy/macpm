"""GPU memory and engine split from the IOAccelerator's PerformanceStatistics.

The ctypes glue needs a live registry, so the mapping is a pure function and
these tests cover it anywhere; one live test runs on this Mac.
"""

import sys
import unittest

from macpm.macos_gpu import device_stats_from_raw, physical_memory


class DeviceStatsMappingTests(unittest.TestCase):
    def test_maps_the_accelerator_statistics(self):
        stats = device_stats_from_raw(
            {
                "allocated_bytes": 5406457856,
                "utilization_pct": 45,
                "renderer_pct": 44,
                "tiler_pct": 45,
            },
            model="Apple M3 Max",
            cores=30,
        )

        self.assertEqual(stats["model"], "Apple M3 Max")
        self.assertEqual(stats["cores"], 30)
        self.assertEqual(stats["allocated_bytes"], 5406457856)
        self.assertEqual(stats["renderer_pct"], 44.0)
        self.assertEqual(stats["tiler_pct"], 45.0)

    def test_missing_counters_stay_unknown(self):
        stats = device_stats_from_raw({"allocated_bytes": 1024})

        self.assertEqual(stats["allocated_bytes"], 1024)
        self.assertIsNone(stats["renderer_pct"])
        self.assertIsNone(stats["tiler_pct"])
        self.assertIsNone(stats["cores"])
        self.assertEqual(stats["model"], "")

    def test_empty_registry_returns_none(self):
        self.assertIsNone(device_stats_from_raw({}))
        self.assertIsNone(device_stats_from_raw({}, model=""))

    def test_model_without_statistics_is_still_useful(self):
        stats = device_stats_from_raw({}, model="Apple M3 Max")

        self.assertEqual(stats["model"], "Apple M3 Max")
        self.assertIsNone(stats["allocated_bytes"])


@unittest.skipUnless(sys.platform == "darwin", "needs the IORegistry (macOS)")
class LiveDeviceStatsTests(unittest.TestCase):
    def test_this_machine_reports_its_gpu_memory(self):
        from macpm.macos_gpu import read_device_stats

        stats = read_device_stats()

        self.assertIsNotNone(stats)
        if stats["allocated_bytes"] is not None:
            self.assertGreaterEqual(stats["allocated_bytes"], 0)
        self.assertGreater(physical_memory(), 0)


if __name__ == "__main__":
    unittest.main()
