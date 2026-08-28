#!/usr/bin/env python3
"""Regenerates the Results table in benchmarks/README.md from
benchmarks/results/<case_id>.json — so the published numbers can never
drift from what run_case.py actually recorded."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
README = BENCH_DIR / "README.md"

TABLE_START = "| Case | Source PR |"
TABLE_END_MARKER = "_(table auto-generated"


def build_table() -> str:
    rows = ["| Case | Source PR | Result | Rounds | Blast-radius rejects | Tokens (prompt+completion) | Time |",
            "|---|---|---|---|---|---|---|"]
    case_files = sorted(RESULTS_DIR.glob("*.json"))
    case_files = [f for f in case_files if f.name != "resume_measurement.json"]
    for f in case_files:
        r = json.loads(f.read_text())
        result = "PASS" if r["success"] else "FAIL"
        pr = r.get("source_pr") or "—"
        pr_cell = f"[link]({pr})" if pr != "—" else "—"
        tokens = f"{r.get('prompt_tokens', '—')}+{r.get('completion_tokens', '—')}"
        rows.append(
            f"| `{r['case_id']}` | {pr_cell} | {result} | {r.get('repair_rounds', '—')} "
            f"| {r.get('blast_radius_rejects', '—')} | {tokens} | {r.get('wall_time_s', '—')}s |"
        )
    return "\n".join(rows)


def main() -> int:
    text = README.read_text()
    start = text.index(TABLE_START)
    end = text.index(TABLE_END_MARKER)
    new_text = text[:start] + build_table() + "\n\n" + text[end:]
    README.write_text(new_text)
    print(f"wrote {README}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
