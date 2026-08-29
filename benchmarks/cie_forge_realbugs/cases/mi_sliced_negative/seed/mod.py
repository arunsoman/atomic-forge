"""Pre-fix (buggy) sliced() — more_itertools more.py @ 958990e^.

Missing guard: a negative slice size `n` is silently accepted and yields a
wrong result (an empty final slice / odd partial) instead of raising. The
real fix adds `if n < 0: raise ValueError('n must be at least 0')` at the
top of the function.
"""
from itertools import count, takewhile


def sliced(seq, n, strict=False):
    """Yield slices of length *n* from the sequence *seq*."""
    iterator = takewhile(len, (seq[i : i + n] for i in count(0, n)))
    if strict:

        def ret():
            for _slice in iterator:
                if len(_slice) != n:
                    raise ValueError("seq is not divisible by n.")
                yield _slice

        return ret()
    else:
        return iterator