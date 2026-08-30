"""Structured "why did this run stop" audit trail.

Separate from the full per-step `.forge/trajectory.jsonl` (which logs every
tool call/round in detail): this is one line per *terminal* event, written
at every exit point of the `fix` pipeline — success included — specifically
so a whole campaign (see `benchmarks/real_issues/`) can be aggregated with
`jq -r .reason exit_audit.jsonl | sort | uniq -c` instead of re-parsing every
trajectory or grepping stdout tails for magic strings (which is how the
round-2 campaign's own failure breakdown had to be reverse-engineered after
the fact — see the "no regression test generated" vs. "does not reproduce"
message-matching gap in `benchmarks/real_issues/run_round2.py`).

Reasons are a closed set on purpose: an ad hoc string per call site is
exactly what made post-hoc campaign analysis fragile the first time.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

EXIT_REASONS = {
    "success",
    "no_test_generated",        # CIE's test-gen agent produced no test file
    "test_not_reproducing",     # a test was generated but doesn't fail at HEAD
    "test_already_passing",     # repair loop's own initial check found it already green
    "ambiguous_branch_defaulted",  # gh couldn't resolve the default branch; guessed main/master
    "cie_unavailable",          # CIE/MCP server setup failed — indexing, bridge, or describe()
    "bootstrap_fail",
    "issue_already_fixed",      # F1 pre-flight repro probe exited 0 on HEAD: stale issue
    "repro_still_failing",      # F1 second witness: generated test green but probe still fails
    "repair_exhausted",
    "pr_create_failed",
}


def record_exit(project_dir, *, reason: str, detail: str = "",
                 extra: Optional[dict] = None) -> Path:
    """Append one audit record to `<project_dir>/.forge/exit_audit.jsonl`.
    Returns the path written to. Raises ValueError on an unregistered
    `reason` — add it to EXIT_REASONS rather than let the set drift."""
    if reason not in EXIT_REASONS:
        raise ValueError(f"unknown exit reason {reason!r} — add it to EXIT_REASONS")
    path = Path(project_dir) / ".forge" / "exit_audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "reason": reason, "detail": detail}
    if extra:
        rec.update(extra)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def read_exits(project_dir) -> list[dict]:
    """All audit records for this project, oldest first. Empty list if none
    were ever written (e.g. a run that crashed before any exit point)."""
    path = Path(project_dir) / ".forge" / "exit_audit.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
