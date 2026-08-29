#!/usr/bin/env python3
"""Round-2 curation: deliberately different bug shapes than the round-1
campaign (results in sweep/results.jsonl.round1.bak).

Round 1 hit three dead ends:
  - pydantic: repair exhausted twice — bugs grounded in the Rust
    pydantic-core extension, unfixable from the Python side.
  - attrs / click: testgen aborted after 10 calls with NO test written —
    the first file_skeleton call alone dumped 3.2-3.4MB into context.
  - rich: fix succeeded, but the repo gates PR creation to collaborators
    (confirmed via the actual gh error, not a guess).

Round 2: drop pydantic and rich entirely, cut attrs/click to a token
quota (2 each, in case the context-blowup was issue-specific rather than
repo-wide), and spend most of the pool on repos never attempted last
time — pure-Python, no C/Rust extension modules (avoids repeating the
pydantic-core failure shape), permissively licensed, actively maintained.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import curate  # noqa: E402

# (repo, license, per_repo_quota)
PLAN = [
    # carried over from round 1, full quota — never attempted last time
    ("mahmoud/boltons", "MIT", 10),
    ("more-itertools/more-itertools", "MIT", 10),
    ("marshmallow-code/marshmallow", "MIT", 10),
    ("python-dateutil/python-dateutil", "Apache-2.0", 10),
    ("tiangolo/typer", "MIT", 10),
    ("encode/httpx", "BSD-3-Clause", 10),
    ("python-poetry/cleo", "MIT", 10),
    ("simple-salesforce/simple-salesforce", "Apache-2.0", 10),
    # new this round — pure-Python, no C/Rust extensions, active trackers
    ("arrow-py/arrow", "Apache-2.0", 10),
    ("jd/tenacity", "Apache-2.0", 10),
    ("python-jsonschema/jsonschema", "MIT", 10),
    ("tqdm/tqdm", "MPL-2.0", 10),
    ("Delgan/loguru", "MIT", 10),
    ("encode/starlette", "BSD-3-Clause", 10),
    ("encode/uvicorn", "BSD-3-Clause", 10),
    ("benoitc/gunicorn", "MIT", 10),
    ("psf/black", "MIT", 10),
    # deprioritized — kept at a token quota only
    ("python-attrs/attrs", "MIT", 2),
    ("pallets/click", "BSD-3-Clause", 2),
    # pydantic/pydantic and Textualize/rich: excluded entirely (see docstring)
]


def main() -> int:
    rows: list[dict] = []
    for repo, lic, quota in PLAN:
        try:
            rows.extend(curate.curate_repo(repo, lic, quota, min_year=2023))
        except Exception as e:  # noqa: BLE001 - one bad repo shouldn't kill the run
            print(f"  ! {repo} failed: {e}", file=sys.stderr)
    rows.sort(key=lambda r: -r["score"])
    out = HERE / "sweep" / "candidates.jsonl"
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"{len(rows)} open issues -> {out}", file=sys.stderr)
    by_repo = {}
    for r in rows:
        by_repo[r["repo"]] = by_repo.get(r["repo"], 0) + 1
    for repo, n in sorted(by_repo.items(), key=lambda kv: -kv[1]):
        print(f"  {repo}: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
