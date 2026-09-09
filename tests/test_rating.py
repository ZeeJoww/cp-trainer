import math
import unittest

from cptrainer.rating import (
    difficulty_for_probability,
    estimate_performance,
    solve_probability,
    update_local_rating,
)


class RatingTests(unittest.TestCase):
    def test_probability_inverse(self):
        for p in (0.1, 0.25, 0.5, 0.75, 0.9):
            d = difficulty_for_probability(2200, p)
            self.assertAlmostEqual(solve_probability(2200, d), p, places=10)

    def test_probability_monotonic(self):
        self.assertGreater(solve_probability(2300, 2200), solve_probability(2100, 2200))

    def test_performance_is_finite(self):
        estimate = estimate_performance([1800, 2000, 2200, 2400], [1, 1, 0, 0])
        self.assertIsNone(estimate.censored)
        self.assertGreater(estimate.value, 1800)
        self.assertLess(estimate.value, 2400)

    def test_all_solved_is_right_censored(self):
        estimate = estimate_performance([1800, 2000], [1, 1])
        self.assertEqual(estimate.censored, ">=")
        self.assertGreater(estimate.value, 2000)

    def test_update_is_conservative(self):
        new, deviation = update_local_rating(2200, 2600, 6, 120)
        self.assertGreater(new, 2200)
        self.assertLessEqual(new - 2200, 140)
        self.assertLess(deviation, 120)


if __name__ == "__main__":
    unittest.main()
