"""Regression test for sliced() rejecting negative n (more-itertools #958990e)."""
import pytest

from mod import sliced


def test_negative():
    """Negative slice sizes should raise instead of silently yielding a
    wrong result."""
    seq = 'ABCDEFG'
    with pytest.raises(ValueError):
        list(sliced(seq, -1))
    with pytest.raises(ValueError):
        list(sliced(seq, -1, strict=True))


def test_positive_still_works():
    assert list(sliced('ABCDEF', 3)) == ['ABC', 'DEF']
    assert list(sliced('ABCDEF', 4)) == ['ABCD', 'EF']