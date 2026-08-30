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

## Run 2 — sympy/sympy#29382 (`show_linprog` SympifyError without A_eq)
pending