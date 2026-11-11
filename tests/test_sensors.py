import unittest

from macpm.sensors import SensorReader, _peak


class SensorReaderTests(unittest.TestCase):
    def test_lines_with_temperatures_and_fans(self):
        reader = SensorReader("Apple M3 Max")
        reader.ready = True
        reader.cpu_c = 52.44
        reader.gpu_c = 43.07
        reader.fans = [{"id": 0, "rpm": 2317.6}, {"id": 1, "rpm": 2502.0}]

        self.assertEqual(
            reader.lines(),
            [
                "CPU 52.4\u00b0C   GPU 43.1\u00b0C",
                "Fan 1 2318 rpm  Fan 2 2502 rpm",
            ],
        )

    def test_missing_gpu_and_fanless(self):
        reader = SensorReader("Apple M1")
        reader.ready = True
        reader.cpu_c = 40.0

        self.assertEqual(reader.lines(), ["CPU 40.0\u00b0C   GPU --", "Fan: none"])

    def test_unavailable_smc(self):
        reader = SensorReader("Apple M1")
        reader.error = "AppleSMC service not found"

        self.assertEqual(
            reader.lines(),
            ["SMC unavailable", "AppleSMC service not found"],
        )

    def test_initial_state(self):
        reader = SensorReader("Apple M1")

        self.assertEqual(reader.lines(), ["reading..."])
        self.assertEqual(reader.details(), [])

    def test_details_include_peaks_and_fan_limits(self):
        reader = SensorReader("Apple M3 Max")
        reader.ready = True
        reader.cpu_c = 52.0
        reader.gpu_c = 43.0
        reader.cpu_peak = 61.5
        reader.gpu_peak = 50.0
        reader.fans = [
            {"id": 0, "rpm": 1000.0, "max_rpm": 6898.0},
            {"id": 1, "rpm": 900.0, "max_rpm": 7450.0},
        ]

        self.assertEqual(
            reader.details(),
            [
                "CPU peak 61.5\u00b0C   GPU peak 50.0\u00b0C",
                "Fan 1 max 6898  Fan 2 max 7450 rpm",
            ],
        )

    def test_details_without_fans(self):
        reader = SensorReader("Apple M1")
        reader.ready = True
        reader.cpu_peak = 45.0
        reader.gpu_peak = 40.0

        self.assertEqual(
            reader.details(),
            ["CPU peak 45.0\u00b0C   GPU peak 40.0\u00b0C"],
        )

    def test_peak_and_reset(self):
        self.assertEqual(_peak(None, None), None)
        self.assertEqual(_peak(None, 40.0), 40.0)
        self.assertEqual(_peak(40.0, 52.0), 52.0)
        self.assertEqual(_peak(52.0, 40.0), 52.0)

        reader = SensorReader("Apple M1")
        reader.cpu_c = 41.0
        reader.cpu_peak = 60.0
        reader.reset_peaks()
        self.assertEqual(reader.cpu_peak, 41.0)


if __name__ == "__main__":
    unittest.main()
