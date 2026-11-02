"""Numerical checks for event-neutral, assumption-explicit probability helpers."""
import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probability_estimation import beta_estimate, selection_scenario


class ProbabilityEstimationTests(unittest.TestCase):
    def test_empty_sample_does_not_publish_prior_as_estimate(self):
        value = beta_estimate(0, 0)
        self.assertEqual(value["status"], "insufficient")
        self.assertIsNone(value["estimate"])
        self.assertIsNone(value["interval"])
        self.assertEqual(value["prior"], {"name": "Jeffreys", "alpha": .5, "beta": .5})
        self.assertFalse(value["calibrated"])

    def test_four_of_four_has_finite_mean_and_wide_interval(self):
        value = beta_estimate(4, 4)
        self.assertEqual(value["estimate"], .9)
        self.assertEqual(value["posterior"], {"alpha": 4.5, "beta": .5})
        self.assertAlmostEqual(value["interval"][0], .5552373823423603, places=12)
        self.assertAlmostEqual(value["interval"][1], .9998846672117617, places=12)
        self.assertLess(value["interval"][0], .6)
        self.assertLess(value["estimate"], 1)

    def test_no_observed_events_does_not_claim_zero_probability(self):
        value = beta_estimate(0, 4)
        self.assertEqual(value["estimate"], .1)
        self.assertGreater(value["interval"][0], 0)
        self.assertGreater(value["interval"][1], .4)

    def test_more_comparable_observations_sharpen_same_rate(self):
        small, large = beta_estimate(2, 4), beta_estimate(200, 400)
        self.assertEqual(small["estimate"], .5)
        self.assertEqual(large["estimate"], .5)
        self.assertGreater(small["interval"][1] - small["interval"][0],
                           large["interval"][1] - large["interval"][0])

    def test_complement_symmetry_and_event_evidence_monotonicity(self):
        estimates = []
        for events in range(11):
            value, complement = beta_estimate(events, 10), beta_estimate(10-events, 10)
            self.assertAlmostEqual(value["estimate"], 1-complement["estimate"])
            self.assertAlmostEqual(value["interval"][0], 1-complement["interval"][1])
            self.assertAlmostEqual(value["interval"][1], 1-complement["interval"][0])
            estimates.append(value["estimate"])
        self.assertEqual(estimates, sorted(estimates))

    def test_credible_level_controls_interval_width(self):
        narrow, wide = beta_estimate(4, 12, confidence=.5), beta_estimate(4, 12, confidence=.99)
        self.assertEqual(narrow["estimate"], wide["estimate"])
        self.assertLess(wide["interval"][0], narrow["interval"][0])
        self.assertGreater(wide["interval"][1], narrow["interval"][1])

    def test_invalid_binomial_counts_and_credible_levels_are_rejected(self):
        for events, total in ((-1, 4), (5, 4), (0, -1), (True, 4), (1, False), (1., 4), (1, 4.), ("1", 4)):
            with self.subTest(events=events, total=total), self.assertRaises(ValueError):
                beta_estimate(events, total)
        for level in (0, 1, -1, 2, math.inf, math.nan, True, ".95", None):
            with self.subTest(level=level), self.assertRaises(ValueError):
                beta_estimate(1, 4, confidence=level)

    def test_selection_ratio_identity_and_direction(self):
        self.assertAlmostEqual(selection_scenario(.2, 1)["estimate"], .2)
        self.assertAlmostEqual(selection_scenario(.2, .5)["estimate"], 1/3)
        self.assertAlmostEqual(selection_scenario(.2, 2)["estimate"], 1/9)
        for ratio in (.01, .5, 1, 2, 100):
            self.assertEqual(selection_scenario(0, ratio)["estimate"], 0)
            self.assertEqual(selection_scenario(1, ratio)["estimate"], 1)

    def test_scenario_transforms_interval_without_mutating_input(self):
        value = beta_estimate(4, 12)
        original = copy.deepcopy(value)
        result = selection_scenario(value, .5)
        self.assertEqual(value, original)
        self.assertEqual(result["interval"], [selection_scenario(q, .5)["estimate"] for q in value["interval"]])
        self.assertFalse(result["identified"])
        self.assertFalse(result["calibrated"])
        self.assertTrue(result["assumptions"])

    def test_empty_scenario_is_not_a_probability_point(self):
        for value in (None, beta_estimate(0, 0)):
            self.assertEqual(selection_scenario(value, 1)["status"], "insufficient")
            self.assertIsNone(selection_scenario(value, 1)["estimate"])

    def test_scenario_invalid_inputs_are_rejected(self):
        for ratio in (0, -1, math.inf, math.nan, True, "1", None):
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                selection_scenario(.2, ratio)
        for point in (-.1, 1.1, math.inf, math.nan, True, ".2"):
            with self.subTest(point=point), self.assertRaises(ValueError):
                selection_scenario(point, 1)
        for value in ({"estimate": .5, "interval": [.8, .2]}, {"estimate": .5, "interval": [0]},
                      {"estimate": None, "interval": [.1, .9]}, {"estimate": .5, "interval": [0, 2]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                selection_scenario(value, 1)

    def test_scenario_extreme_finite_ratios_do_not_overflow(self):
        self.assertTrue(0 <= selection_scenario(.2, 1e308)["estimate"] <= 1)
        self.assertTrue(0 <= selection_scenario(.2, 1e-308)["estimate"] <= 1)


if __name__ == "__main__":
    unittest.main()
