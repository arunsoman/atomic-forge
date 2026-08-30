# mlfl — multi-language fault localization (prototype, unwired)

**Status: not wired into anything.** `repair_agent.py` imports
`SpectrumHit`/`spectrum_localize` from **`atomic_forge/spectrum.py`**
(one level up) — that's the live path, Python-only via `coverage.py` +
junit-xml, bugfixed 2026-08-30 (commit `b286510`) after real bugs found
chasing astroid#769. Nothing here is used by it.

## What this is

A self-contained, multi-language generalization of the same idea
(line-level Ochiai SBFL): a pluggable `CoverageBackend` per language
(`backends/` — python, javascript, go, rust, java) behind one
language-agnostic `spectrum.py` core, plus `fusion.py` (bounded
two-tier fusion with auxiliary signals like callgraph distance) and
`fault_localization.py` (the `localize()` orchestrator + CLI).

Its API (`localize()`, `spectrum.compute_line_ochiai()`,
`SpectrumResult`/`FusedCandidate`) is **not** compatible with the live
`spectrum.py`'s API (`spectrum_localize()`, `SpectrumHit`) — this was
never meant as a drop-in replacement, just a parallel exploration of
"what if this worked across 5 languages instead of just Python."

51/51 of its own tests pass standalone (`python3 -m unittest
test_spectrum_regression` from this directory — imports are flat
`sys.path` hacks, not package-relative, so run it from here, not via
pytest's normal collection; `testpaths = ["tests"]` in the root
`pyproject.toml` already keeps it out of CI).

## If someone picks this up

Real design decisions before this could replace or feed into the live
path:
- Reconcile with `spectrum.py`'s statement/line-level fixes from
  `b286510` (file-level flatness, self-contamination) — this
  prototype predates that commit and may not have them.
- The live path passes `image`/`test_cmd` for sandboxed execution
  (see `spectrum.py`'s `_run_one_with_coverage`); this prototype's
  backends shell out directly with bare `subprocess.run` — no sandbox
  story yet.
- `fusion.py`'s `callgraph_distance` auxiliary signal presumably wants
  to come from `cie_backend.py` — not wired here either.
