"""Regression test for Bits length-bound off-by-one (boltons #c1c25da)."""
import pytest

from mod import Bits


def test_bits_len_bound():
    # The largest value representable in n bits is 2 ** n - 1, so 2 ** n must
    # be rejected rather than silently producing an over-long Bits.
    # Largest value that fits is accepted and round-trips.
    assert Bits(3, 2).as_bin() == '11'
    # 2 ** len_ does not fit in len_ bits and must raise.
    with pytest.raises(ValueError):
        Bits(4, 2)
    with pytest.raises(ValueError):
        Bits(1, 0)