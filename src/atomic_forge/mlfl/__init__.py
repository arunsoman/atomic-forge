"""
mlfl — multi-language fault-localization core.

`spectrum.py` in this package is the shared, language-agnostic Ochiai
scoring engine (line-level, 51 tests in `test_spectrum_regression.py`).
`atomic_forge.spectrum.spectrum_localize` imports it for the actual
suspiciousness math, while keeping its own sandboxed, hardened test-
execution plumbing (Docker `image` routing, ANSI-safe FAILED detection,
baked-in-test-path rescoping — see that module's docstring) rather than
this package's un-sandboxed `backends/`. See `mlfl/README.md` for what
else in here is still an unwired prototype (multi-language backends,
`fusion.py`, the `localize()` CLI orchestrator).
"""
from __future__ import annotations
