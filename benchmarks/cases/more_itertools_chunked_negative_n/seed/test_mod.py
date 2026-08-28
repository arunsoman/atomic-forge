"""
Real tests from more-itertools' `ChunkedTests`, adapted to import the
standalone `mod.py` instead of the full package. `test_negative` is the
actual new test PR #1223 added — it fails against the pre-fix `mod.py`
and is the oracle atomic-forge's repair loop is judged against. The rest
are the real pre-existing tests, included so a "fix" that breaks other
behavior (e.g. treating any n<0 including via a lazy/broad except) is
caught, not just the one new assertion.
"""
from unittest import TestCase

import mod as mi


class ChunkedTests(TestCase):
    def test_even(self):
        self.assertEqual(
            list(mi.chunked('ABCDEF', 3)), [['A', 'B', 'C'], ['D', 'E', 'F']]
        )

    def test_odd(self):
        self.assertEqual(
            list(mi.chunked('ABCDE', 3)), [['A', 'B', 'C'], ['D', 'E']]
        )

    def test_none(self):
        self.assertEqual(
            list(mi.chunked('ABCDE', None)), [['A', 'B', 'C', 'D', 'E']]
        )

    def test_strict_false(self):
        self.assertEqual(
            list(mi.chunked('ABCDE', 3, strict=False)),
            [['A', 'B', 'C'], ['D', 'E']],
        )

    def test_strict_being_true(self):
        def f():
            return list(mi.chunked('ABCDE', 3, strict=True))

        self.assertRaisesRegex(ValueError, "iterable is not divisible by n", f)
        self.assertEqual(
            list(mi.chunked('ABCDEF', 3, strict=True)),
            [['A', 'B', 'C'], ['D', 'E', 'F']],
        )

    def test_strict_being_true_with_size_none(self):
        def f():
            return list(mi.chunked('ABCDE', None, strict=True))

        self.assertRaisesRegex(
            ValueError, "n must not be None when using strict mode.", f
        )

    def test_negative(self):
        """Real regression test added by PR #1223 — fails against the
        pre-fix mod.py, is the oracle for this benchmark case."""
        self.assertRaisesRegex(
            ValueError,
            "n must be at least 0",
            lambda: list(mi.chunked('ABCDE', -1)),
        )
