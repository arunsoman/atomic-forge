"""
Real tests from more-itertools' `InterleaveEvenlyTests`, adapted to
import the standalone `mod.py` instead of the full package.
`test_no_iterables` is the actual new test PR #1193 added — it fails
against the pre-fix `mod.py` (IndexError instead of an empty result) and
is the oracle this case is judged against. The rest are real
pre-existing tests, included so a "fix" that special-cases empty input in
a way that breaks the normal Bresenham interleaving is caught too.
"""
from itertools import combinations
from unittest import TestCase

import mod as mi


class InterleaveEvenlyTests(TestCase):
    def test_equal_lengths(self):
        a = [1, 2, 3]
        b = [5, 6, 7]
        actual = list(mi.interleave_evenly([a, b]))
        expected = [1, 5, 2, 6, 3, 7]
        self.assertEqual(actual, expected)

    def test_proportional(self):
        a = [1, 2, 3, 4]
        b = [5, 6]
        actual = list(mi.interleave_evenly([a, b]))
        expected = [1, 2, 5, 3, 4, 6]
        self.assertEqual(actual, expected)
        actual_swapped = list(mi.interleave_evenly([b, a]))
        self.assertEqual(actual_swapped, expected)

    def test_not_proportional(self):
        a = [1, 2, 3, 4, 5, 6, 7]
        b = [8, 9, 10]
        expected = [1, 2, 8, 3, 4, 9, 5, 6, 10, 7]
        actual = list(mi.interleave_evenly([a, b]))
        self.assertEqual(actual, expected)

    def test_degenerate_one(self):
        a = [0, 1, 2, 3, 4]
        b = [5]
        expected = [0, 1, 2, 5, 3, 4]
        actual = list(mi.interleave_evenly([a, b]))
        self.assertEqual(actual, expected)

    def test_degenerate_empty(self):
        a = [1, 2, 3]
        b = []
        expected = [1, 2, 3]
        actual = list(mi.interleave_evenly([a, b]))
        self.assertEqual(actual, expected)

    def test_three_iters(self):
        a = ["a1", "a2", "a3", "a4", "a5"]
        b = ["b1", "b2", "b3"]
        c = ["c1"]
        actual = list(mi.interleave_evenly([a, b, c]))
        expected = ["a1", "b1", "a2", "c1", "a3", "b2", "a4", "b3", "a5"]
        self.assertEqual(actual, expected)

    def test_manual_lengths(self):
        a = combinations(range(4), 2)
        len_a = 4 * (4 - 1) // 2  # == 6
        b = combinations(range(4), 3)
        len_b = 4
        expected = [
            (0, 1), (0, 1, 2), (0, 2), (0, 3), (0, 1, 3),
            (1, 2), (0, 2, 3), (1, 3), (2, 3), (1, 2, 3),
        ]
        actual = list(mi.interleave_evenly([a, b], lengths=[len_a, len_b]))
        self.assertEqual(expected, actual)

    def test_no_length_raises(self):
        iterables = [range(5), combinations(range(5), 2)]
        with self.assertRaises(ValueError):
            list(mi.interleave_evenly(iterables))

    def test_argument_mismatch_raises(self):
        iterables = [range(3)]
        lengths = [3, 4]
        with self.assertRaises(ValueError):
            list(mi.interleave_evenly(iterables, lengths=lengths))

    def test_no_iterables(self):
        """Real regression test added by PR #1193 — fails against the
        pre-fix mod.py (IndexError), is the oracle for this benchmark
        case."""
        self.assertEqual(list(mi.interleave_evenly([])), [])
        self.assertEqual(list(mi.interleave_evenly([], lengths=[])), [])
