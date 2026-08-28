"""
Real parametrized tests from humanize's `test_naturalsize`, adapted to
import the standalone `mod.py` instead of the package. The six
`# rollover` cases at the end are the real regression tests PR #329
added — they fail against the pre-fix `mod.py` (stale suffix after
rounding carries the mantissa to `base`) and are the oracle this case is
judged against. Every other case is a real pre-existing assertion, kept
so a fix that special-cases the rollover in a way that breaks normal
formatting is caught too.
"""
import pytest

import mod


@pytest.mark.parametrize(
    "test_args, expected",
    [
        ([300], "300 Bytes"),
        (["1000"], "1.0 kB"),
        ([10**3], "1.0 kB"),
        ([10**6], "1.0 MB"),
        ([10**9], "1.0 GB"),
        ([1000**1 * 31], "31.0 kB"),
        ([1000**2 * 32], "32.0 MB"),
        ([300, True], "300 Bytes"),
        ([1024**1 * 31, True], "31.0 KiB"),
        ([1024**2 * 32, True], "32.0 MiB"),
        ([1000**1 * 31, True], "30.3 KiB"),
        ([1000**1 * 31, False, True], "30.3K"),
        ([300, False, True], "300B"),
        ([3000, False, True], "2.9K"),
        ([3000000, False, True], "2.9M"),
        ([1024, False, True], "1.0K"),
        ([1, False, False], "1 Byte"),
        ([1.0, False, False], "1 Byte"),
        (["1", False, False], "1 Byte"),
        ([3141592, False, False, "%.2f"], "3.14 MB"),
        ([3000, False, True, "%.3f"], "2.930K"),
        ([3000000000, False, True, "%.0f"], "3G"),
        ([1.123456789], "1 Bytes"),
        ([1.123456789 * 10**3], "1.1 kB"),
        ([1.123456789 * 10**6], "1.1 MB"),
        # Real regression tests added by PR #329 (rollover at unit
        # boundaries) — these fail against the pre-fix mod.py.
        ([999999], "1.0 MB"),
        ([999999999], "1.0 GB"),
        ([999999999999], "1.0 TB"),
        ([1024**2 - 1, True], "1.0 MiB"),
        ([1024**3 - 1, True], "1.0 GiB"),
        ([1024**2 - 1, False, True], "1.0M"),
    ],
)
def test_naturalsize(test_args, expected):
    assert mod.naturalsize(*test_args) == expected

    # Retest with negative input — real test also does this.
    if isinstance(test_args[0], int):
        test_args[0] *= -1
    else:
        test_args[0] = f"-{test_args[0]}"
    assert mod.naturalsize(*test_args) == "-" + expected
