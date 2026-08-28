"""
Standalone extract of `more_itertools.chunked` + its `take` dependency,
at the pre-fix commit (more-itertools @ 516f0a80, the base of
https://github.com/more-itertools/more-itertools/pull/1223), for
atomic-forge's repair-loop benchmark. Verbatim real code — not
paraphrased or simplified — trimmed to a standalone module so the
benchmark doesn't have to pull in the whole 5,500-line real file.

The bug: `chunked()` has no `n < 0` guard, so a negative `n` leaks
`islice`'s internal error message instead of a clear one. Real fix
(PR #1223): raise `ValueError('n must be at least 0')` up front.
"""
from itertools import islice
from functools import partial


def take(n, iterable):
    """Return first *n* items of the *iterable* as a list."""
    return list(islice(iterable, n))


def chunked(iterable, n, strict=False):
    """Break *iterable* into lists of length *n*:

        >>> list(chunked([1, 2, 3, 4, 5, 6], 3))
        [[1, 2, 3], [4, 5, 6]]

    By the default, the last yielded list will have fewer than *n* elements
    if the length of *iterable* is not divisible by *n*:

        >>> list(chunked([1, 2, 3, 4, 5, 6, 7, 8], 3))
        [[1, 2, 3], [4, 5, 6], [7, 8]]

    If the length of *iterable* is not divisible by *n* and *strict* is
    ``True``, then ``ValueError`` will be raised before the last
    list is yielded.
    """
    iterator = iter(partial(take, n, iter(iterable)), [])
    if strict:
        if n is None:
            raise ValueError('n must not be None when using strict mode.')

        def ret():
            for chunk in iterator:
                if len(chunk) != n:
                    raise ValueError('iterable is not divisible by n.')
                yield chunk

        return ret()
    else:
        return iterator
