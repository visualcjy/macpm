import unittest

from macpm.sampler import _energy_available, _sane_energy


class SaneEnergyTests(unittest.TestCase):
    def test_normal_delta_is_kept(self):
        self.assertEqual(_sane_energy(10.0, 1.0, 240.0), 10.0)

    def test_spike_is_dropped(self):
        self.assertEqual(_sane_energy(6208.38, 1.0, 240.0), 0.0)

    def test_negative_and_missing_values_are_zeroed(self):
        self.assertEqual(_sane_energy(-1.0, 1.0, 240.0), 0.0)
        self.assertEqual(_sane_energy(0.0, 1.0, 240.0), 0.0)

    def test_zero_elapsed_is_never_plausible(self):
        self.assertEqual(_sane_energy(1.0, 0.0, 240.0), 0.0)

    def test_value_at_the_limit_is_kept(self):
        self.assertEqual(_sane_energy(240.0, 1.0, 240.0), 240.0)


class EnergyAvailabilityTests(unittest.TestCase):
    def test_counter_must_be_silent_past_the_grace_period(self):
        self.assertTrue(_energy_available(False, 1))
        self.assertTrue(_energy_available(False, 3))
        self.assertFalse(_energy_available(False, 4))

    def test_seen_counter_stays_available(self):
        self.assertTrue(_energy_available(True, 10))


if __name__ == "__main__":
    unittest.main()
