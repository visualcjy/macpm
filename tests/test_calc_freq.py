import unittest

from macpm.sampler import calc_freq


class CalcFreqTests(unittest.TestCase):
    def test_weighted_active_frequency(self):
        states = [("IDLE", 50), ("P0", 50), ("P1", 50)]
        average, active = calc_freq(states, [1000, 2000])
        self.assertAlmostEqual(average, 1500.0)
        self.assertAlmostEqual(active, 100 / 150)

    def test_all_idle(self):
        self.assertEqual(calc_freq([("IDLE", 100)], [1000]), (0.0, 0.0))

    def test_no_states(self):
        self.assertEqual(calc_freq([], [1000]), (0.0, 0.0))

    def test_missing_frequency_table(self):
        average, active = calc_freq([("IDLE", 50), ("P0", 50)], [])
        self.assertEqual(average, 0.0)
        self.assertAlmostEqual(active, 0.5)

    def test_shorter_frequency_table_uses_last_value(self):
        states = [("OFF", 50), ("P1", 25), ("P2", 25), ("P3", 25)]
        average, active = calc_freq(states, [100, 200])
        self.assertAlmostEqual(average, 500 / 3)
        self.assertAlmostEqual(active, 75 / 125)


if __name__ == "__main__":
    unittest.main()
