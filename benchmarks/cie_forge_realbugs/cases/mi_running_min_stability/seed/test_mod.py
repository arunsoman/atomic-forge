"""Regression test for running_min/running_max stability (more-itertools #d992be0).

Adapted from the real PR's TestRunningMin.test_stability /
TestRunningMax.test_stability — `min(x, y)` returns x when x == y, so the
running window minimum must keep the incumbent's type when an equal value
arrives, not silently swap to the new value's type.
"""
from fractions import Fraction

from mod import running_min, running_max


def test_stability_min():
    # min(x, y) returns x when x == y
    data = [0, 0.0, Fraction(0)]
    assert list(map(type, running_min(data, maxlen=2))) == [
        type(min(data[0:1])),
        type(min(data[0:2])),
        type(min(data[1:3])),
    ]


def test_stability_max():
    # max(x, y) returns x when x == y
    data = [0, 0.0, Fraction(0)]
    assert list(map(type, running_max(data, maxlen=2))) == [
        type(max(data[0:1])),
        type(max(data[0:2])),
        type(max(data[1:3])),
    ]