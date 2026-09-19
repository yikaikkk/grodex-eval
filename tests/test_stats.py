"""Unit tests for the stats helpers."""

import unittest

from grodex_eval.stats import count_by, percentile, rate, summarize


class PercentileTests(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(percentile([], 50))

    def test_single_value(self):
        self.assertEqual(percentile([7], 90), 7)

    def test_interpolates_between_points(self):
        # [0, 10] at q=50 sits exactly halfway.
        self.assertAlmostEqual(percentile([0, 10], 50), 5.0)
        # n=4 -> sorted [0,1,2,3]; q=50 lands at index 1.5 -> 1.5
        self.assertAlmostEqual(percentile([0, 1, 2, 3], 50), 1.5)
        # q=0 and q=100 are the extremes.
        self.assertAlmostEqual(percentile([0, 1, 2, 3], 0), 0.0)
        self.assertAlmostEqual(percentile([0, 1, 2, 3], 100), 3.0)

    def test_rejects_out_of_range_q(self):
        with self.assertRaises(ValueError):
            percentile([1, 2, 3], 101)


class SummarizeTests(unittest.TestCase):
    def test_ignores_none_and_reports_none_when_empty(self):
        summary = summarize([None, None])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["mean"])
        self.assertIsNone(summary["p90"])

    def test_percentiles_flagged_unreliable_on_tiny_samples(self):
        # With 2 samples a "p50" is just the midpoint; it must not be presented
        # as a median.
        self.assertFalse(summarize([1, 2])["enough"])
        self.assertFalse(summarize([1])["enough"])
        self.assertFalse(summarize([])["enough"])
        self.assertTrue(summarize([1, 2, 3])["enough"])

    def test_basic_fields(self):
        summary = summarize([10, 20, 30, 40])
        self.assertEqual(summary["count"], 4)
        self.assertAlmostEqual(summary["mean"], 25.0)
        self.assertEqual(summary["max"], 40)
        self.assertAlmostEqual(summary["sum"], 100.0)
        self.assertAlmostEqual(summary["p50"], 25.0)


class RateTests(unittest.TestCase):
    def test_zero_denominator_is_none_not_zero(self):
        # "no data" must not be silently reported as 0% .
        self.assertIsNone(rate(5, 0))
        self.assertIsNone(rate(0, None))

    def test_none_numerator_counts_as_zero(self):
        self.assertEqual(rate(None, 4), 0.0)

    def test_normal_case(self):
        self.assertAlmostEqual(rate(1, 4), 0.25)


class CountByTests(unittest.TestCase):
    def test_groups_and_sorts_desc(self):
        rows = [{"k": "a"}, {"k": "b"}, {"k": "a"}]
        self.assertEqual(count_by(rows, "k"), {"a": 2, "b": 1})

    def test_none_key_becomes_dash(self):
        self.assertEqual(count_by([{"k": None}], "k"), {"-": 1})


if __name__ == "__main__":
    unittest.main()
