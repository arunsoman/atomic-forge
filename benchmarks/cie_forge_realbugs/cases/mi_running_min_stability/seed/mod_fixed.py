"""Pre-fix (buggy) running_min / running_max — more_itertools recipes.py @ d992be0^.

Stability bug: the monotonically-increasing/decreasing subsequence uses a
STRICT comparison (`<` / `>`), so when an equal-but-different-type value
arrives (e.g. 0 then 0.0 then Fraction(0)) the earlier value is dropped and
the running min/max silently changes type. The fix uses `<=` / `>=` so an
equal value does not evict the incumbent — matching `min(x, y)` / `max(x, y)`
returning the LEFT operand when x == y.
"""
from collections import deque
from itertools import accumulate


def _windowed_running_min(iterator, maxlen):
    sis = deque()  # Strictly increasing subsequence
    for index, value in enumerate(iterator):
        if sis and sis[0][0] == index - maxlen:
            sis.popleft()
        while sis and not sis[-1][1] <= value:
            sis.pop()
        sis.append((index, value))  # Most recent value at position -1
        yield sis[0][1]  # Window minimum at position 0


def running_min(iterable, *, maxlen=None):
    """Smallest of values seen so far or values in a sliding window."""
    iterator = iter(iterable)
    if maxlen is None:
        return accumulate(iterator, func=min)
    if maxlen <= 0:
        raise ValueError('Window size should be positive')
    return _windowed_running_min(iterator, maxlen)


def _windowed_running_max(iterator, maxlen):
    sds = deque()  # Strictly decreasing subsequence
    for index, value in enumerate(iterator):
        if sds and sds[0][0] == index - maxlen:
            sds.popleft()
        while sds and not sds[-1][1] >= value:
            sds.pop()
        sds.append((index, value))  # Most recent value at position -1
        yield sds[0][1]  # Window maximum at position 0


def running_max(iterable, *, maxlen=None):
    """Largest of values seen so far or values in a sliding window."""
    iterator = iter(iterable)
    if maxlen is None:
        return accumulate(iterator, func=max)
    if maxlen <= 0:
        raise ValueError('Window size should be positive')
    return _windowed_running_max(iterator, maxlen)