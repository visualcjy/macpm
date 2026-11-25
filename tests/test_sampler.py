import unittest

from macpm.metrics import EnergyTracker
from macpm.sampler import _energy_available, _energy_warming, _sane_energy


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
    def test_silent_counter_is_unavailable(self):
        self.assertFalse(_energy_available(False))

    def test_reporting_counter_is_available(self):
        self.assertTrue(_energy_available(True))

    def test_first_frames_are_warming(self):
        self.assertTrue(_energy_warming(1))
        self.assertTrue(_energy_warming(3))
        self.assertFalse(_energy_warming(4))


class EnergyTrackerTests(unittest.TestCase):
    """A counter that freezes must stop being trusted (macOS 27 CPU channel)."""

    def test_startup_grace_is_warming_but_not_available(self):
        tracker = EnergyTracker()

        self.assertTrue(tracker.warming())
        self.assertFalse(tracker.available())

    def test_silent_counter_leaves_the_grace_period(self):
        tracker = EnergyTracker()
        for _ in range(4):
            tracker.add(0.0)

        self.assertFalse(tracker.warming())
        self.assertFalse(tracker.available())

    def test_frozen_counter_falls_back_after_the_window(self):
        tracker = EnergyTracker(window=5)
        tracker.add(1.0)
        for _ in range(4):
            tracker.add(0.0)
        self.assertTrue(tracker.available())

        tracker.add(0.0)

        self.assertFalse(tracker.available())

    def test_recovering_counter_is_trusted_again(self):
        tracker = EnergyTracker(window=3)
        for _ in range(10):
            tracker.add(0.0)
        self.assertFalse(tracker.available())

        tracker.add(2.0)

        self.assertTrue(tracker.available())


if __name__ == "__main__":
    unittest.main()
