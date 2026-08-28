"""
Standalone extract of `humanize.naturalsize` (+ its `suffixes` table), at
the pre-fix commit (python-humanize/humanize @ 976484a6, the base of
https://github.com/python-humanize/humanize/pull/329), for atomic-forge's
repair-loop benchmark. Verbatim real code, except `_gettext` (i18n) is
stubbed as identity — this module never activates a translation, so the
real function's English-locale behavior (what every test here checks) is
unchanged by that stub.

The bug: `naturalsize()` picks the suffix from the *unrounded* byte
count, then rounds the mantissa afterward. When rounding pushes the
mantissa up to `base`, the already-chosen suffix is stale:
`naturalsize(999999)` returns `'1000.0 kB'` instead of `'1.0 MB'`. Real
fix (PR #329): after rounding, if the mantissa reached `base` and a
larger suffix exists, step up one suffix.
"""
from __future__ import annotations

from math import log


def _(s: str) -> str:
    """Stub for humanize.i18n._gettext — identity (no translation active
    in this standalone extract); every test here is English-locale."""
    return s


suffixes = {
    "decimal": ("kB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB", "RB", "QB"),
    "binary": ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB", "RiB", "QiB"),
    "gnu": "KMGTPEZYRQ",
}


def naturalsize(
    value: float | str,
    binary: bool = False,
    gnu: bool = False,
    format: str = "%.1f",
) -> str:
    """Format a number of bytes like a human-readable filesize (e.g. 10 kB)."""
    if gnu:
        suffix = suffixes["gnu"]
    elif binary:
        suffix = suffixes["binary"]
    else:
        suffix = suffixes["decimal"]

    base = 1024 if (gnu or binary) else 1000
    bytes_ = float(value)
    abs_bytes = abs(bytes_)

    if abs_bytes == 1 and not gnu:
        return _("%d Byte") % int(bytes_)

    if abs_bytes < base:
        return f"{int(bytes_)}B" if gnu else _("%d Bytes") % int(bytes_)

    exp = int(min(log(abs_bytes, base), len(suffix)))
    space = "" if gnu else " "
    ret: str = format % (bytes_ / (base**exp)) + space + _(suffix[exp - 1])
    return ret
