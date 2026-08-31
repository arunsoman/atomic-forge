#!/usr/bin/env python3
"""Round-4 curation: fresh repos only — everything below was NOT attempted
in round1/round2/round3 (see RESULTS.md for the touched-repo list).

Deliberately pure-Python-surface repos, per round2's own learned lesson
(pydantic-core/Rust bugs are unfixable from the Python side; C-extension
repos like numpy/pandas/matplotlib risk the same failure shape). Also
excludes Textualize/rich (PR creation gated to collaborators, confirmed)
and keeps attrs/click off the list entirely (round2: context-blowup on
file_skeleton, already tried and deprioritized once).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import curate  # noqa: E402

# (repo, license, per_repo_quota) — all untouched by round1-3.
# python/mypy excluded (2026-08-31, round4): #21907 and #21904 both died
# at testgen (11 LLM calls / ~38k tokens each, no repair loop ever
# reached) — mypy's actual test suite isn't ordinary pytest test
# functions, it's a custom `[case ...]` DSL driven by
# `pytest_plugins = ["mypy.test.data"]` in conftest.py (confirmed by
# cloning and reading it directly), which testgen has no way to author
# into. Same shape of lesson as round2's pydantic/rich exclusions —
# see RESULTS.md.
PLAN = [
    ("mlflow/mlflow", "Apache-2.0", 8),
    ("sympy/sympy", "BSD-3-Clause", 8),
    ("pydata/xarray", "Apache-2.0", 8),
    ("pylint-dev/pylint", "GPL-2.0", 8),
    ("mahmoud/boltons", "BSD-3-Clause", 8),
    ("more-itertools/more-itertools", "MIT", 8),
    ("dateutil/dateutil", "Apache-2.0", 8),
    ("fastapi/typer", "MIT", 8),
    ("encode/httpx", "BSD-3-Clause", 8),
    ("python-jsonschema/jsonschema", "MIT", 8),
    ("encode/starlette", "BSD-3-Clause", 8),
    ("encode/uvicorn", "BSD-3-Clause", 8),
    ("simple-salesforce/simple-salesforce", "Apache-2.0", 8),
]


def main() -> int:
    rows: list[dict] = []
    for repo, lic, quota in PLAN:
        try:
            rows.extend(curate.curate_repo(repo, lic, quota, min_year=2024))
        except Exception as e:  # noqa: BLE001 - one bad repo shouldn't kill the run
            print(f"  ! {repo} failed: {e}", file=sys.stderr)
    rows.sort(key=lambda r: -r["score"])
    out = HERE / "sweep" / "candidates_round4.jsonl"
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"{len(rows)} open issues -> {out}", file=sys.stderr)
    by_repo: dict[str, int] = {}
    for r in rows:
        by_repo[r["repo"]] = by_repo.get(r["repo"], 0) + 1
    for repo, n in sorted(by_repo.items(), key=lambda kv: -kv[1]):
        print(f"  {repo}: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
