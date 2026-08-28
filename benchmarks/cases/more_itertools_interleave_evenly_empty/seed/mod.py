"""
Standalone extract of `more_itertools.interleave_evenly`, at the pre-fix
commit (more-itertools @ 5d946b35, the base of
https://github.com/more-itertools/more-itertools/pull/1193), for
atomic-forge's repair-loop benchmark. Verbatim real code.

The bug: an empty `iterables` (or `lengths=[]`) leaves `dims == 0`, then
`lengths_desc[0]` on the next line indexes an empty list -> IndexError,
instead of returning an empty iterator the way every other degenerate
case here does. Real fix (PR #1193): return immediately when dims == 0.
"""


def interleave_evenly(iterables, lengths=None):
    """
    Interleave multiple iterables so that their elements are evenly distributed
    throughout the output sequence.

        >>> iterables = [1, 2, 3, 4, 5], ['a', 'b']
        >>> list(interleave_evenly(iterables))
        [1, 2, 'a', 3, 4, 'b', 5]

        >>> iterables = [[1, 2, 3], [4, 5], [6, 7, 8]]
        >>> list(interleave_evenly(iterables))
        [1, 6, 4, 2, 7, 3, 8, 5]

    This function requires iterables of known length. Iterables without
    ``__len__()`` can be used by manually specifying lengths with *lengths*:

        >>> from itertools import combinations, repeat
        >>> iterables = [combinations(range(4), 2), ['a', 'b', 'c']]
        >>> lengths = [4 * (4 - 1) // 2, 3]
        >>> list(interleave_evenly(iterables, lengths=lengths))
        [(0, 1), (0, 2), 'a', (0, 3), (1, 2), 'b', (1, 3), (2, 3), 'c']

    Based on Bresenham's algorithm.
    """
    if lengths is None:
        try:
            lengths = [len(it) for it in iterables]
        except TypeError:
            raise ValueError(
                'Iterable lengths could not be determined automatically. '
                'Specify them with the lengths keyword.'
            )
    elif len(iterables) != len(lengths):
        raise ValueError('Mismatching number of iterables and lengths.')

    dims = len(lengths)

    # sort iterables by length, descending
    lengths_permute = sorted(
        range(dims), key=lambda i: lengths[i], reverse=True
    )
    lengths_desc = [lengths[i] for i in lengths_permute]
    iters_desc = [iter(iterables[i]) for i in lengths_permute]

    # the longest iterable is the primary one (Bresenham: the longest
    # distance along an axis)
    delta_primary, deltas_secondary = lengths_desc[0], lengths_desc[1:]
    iter_primary, iters_secondary = iters_desc[0], iters_desc[1:]
    errors = [delta_primary // dims] * len(deltas_secondary)

    to_yield = sum(lengths)
    while to_yield:
        yield next(iter_primary)
        to_yield -= 1
        # update errors for each secondary iterable
        errors = [e - delta for e, delta in zip(errors, deltas_secondary)]

        # those iterables for which the error is negative are yielded
        # ("diagonal step" in Bresenham)
        for i, e_ in enumerate(errors):
            if e_ < 0:
                yield next(iters_secondary[i])
                to_yield -= 1
                errors[i] += delta_primary
