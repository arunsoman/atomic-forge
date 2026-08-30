# Live campaign log — `forge fix --raise-pr`

Target universe: `campaign50_targets.json` (tier-1 + tier-2, 18 repos).
Protocol: per-issue runs, 1 open PR/repo, AI-policy grep before raising, honest provenance footer.

## Run 1 — dask/dask#11884 (`.str.split` coerces dtype to object)

| | |
|---|---|
| Result | **No PR — issue not reproducible on HEAD** |
| Loop | llm_calls=11, prompt=64,380 tok, completion=8,178 tok; CIE graph 42,004 nodes / 112,210 edges |
| Exit reason | `repair_exhausted` — but root-caused: bootstrap venv lacked `.[dataframe]` extras + `pytest-cov` (cov-config error blocked suite execution) |
| Verdict | Stale issue: manual repro on dask `2026.8.0.post0+g9dc535daa` shows dtype *is* preserved (`string[pyarrow]`), matching pandas |
| Discovered | **New candidate bug**: `str.split(expand=True)` meta/computed mismatch — `_meta` claims `StringDtype(na_value=nan)` (python backend) while computed returns `StringDtype(na_value=<NA>)` (pyarrow backend) → validate + optionally file new upstream issue |
| Postmortem | `.forge/learning.json` — 3 untried paths incl. the correct fix direction (`pass dtype when building _meta`) |

### Campaign-env action items
1. bootstrap gate must install repo extras (e.g. `.[dataframe]`) or probe tests from the affected module — "suite runnable" ≠ "affected module runnable"
2. `pytest-cov` should join the default probe venv when repo pytest config requests cov

## Run 3 — sphinx-doc/sphinx#13180 (napoleon section ordering)

Ops-fixed relaunch (REST-fed issue body + user-vouched clone) reached the LLM pipeline:
- CIE index on sphinx: OK (full graph)
- testgen: **abort after 10 turns** — `no_test_generated`, 10 llm_calls, 99.6k prompt tok
- exit reason: `no_test_generated`
- lesson: render-order bugs (HTML section sequence, docstring rendering) are a weak spot
  for the testgen agent — candidate for operator-supplied tests (`--test-file`) or more turns

## F1 family — implemented 2026-08-30 (this session)

- **F1** `fix --repro <script>`: probe on HEAD before any LLM spend; exit 0 → abort
  `issue_already_fixed`; after repair must flip to exit 0 else abort `repro_still_failing`
- **F1b** clone integrity: post-clone `git rev-parse --verify HEAD` + one retry (sphinx's
  zero-commit clone can never be returned again)
- **F1c** fetch_issue channel fallback: GraphQL → `gh api` REST → unauthenticated curl REST;
  `state` now rides along in the issue dict (cheap open/closed re-verification)
- new EXIT_REASONS: `issue_already_fixed`, `repro_still_failing`
- tests: `tests/test_issue_fetch_and_integrity.py` (16 tests) — full suite 314 passed

## Run 4 — pending

astroid#769 (inference through class constructor + type hints), this time WITH a `--repro`
script written from the issue text — exercising F1 end-to-end.