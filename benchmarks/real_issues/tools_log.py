#!/usr/bin/env python3
"""Tool-call analyzer: which tools did the LLM actually call, per phase?

Parses a forge trajectory.jsonl and produces:
  - a per-phase (testgen / localize / repair_s0 / repair_s1 / ...) tool-call
    histogram: {tool: count}, plus derived read/write/submit ratios
  - a JSON sidecar for the campaign ledger / wiki

Usage:
  python tools_log.py <trajectory.jsonl> [--out <tools.json>]
  python tools_log.py <logs/slug/trajectory.jsonl>            # writes sidecar

Run-4 baseline (astroid#769, nemotron, 2026-08-31): see
benchmarks/real_issues/logs/pylint-dev-astroid-769/tools.json — read-heavy
(view_file/search_symbol ~most calls), write-pressure absent, one submit.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

WRITE_TOOLS = {"write_file", "patch_file", "apply_patch", "edit_file"}
READ_TOOLS = {"view_file", "search_symbol", "read_file", "grep", "list_dir", "search_code"}
TEST_TOOLS = {"run_tests", "run_test", "run_shell"}


def parse(traj_path: Path) -> dict:
    turns = Counter()          # (phase, tool) -> count
    actions = Counter()        # phase -> tool-call count
    ops = Counter()            # structured non-LLM-loop events
    samples_aborted = Counter()
    submits = Counter()
    test_runs = 0
    for line in traj_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = r.get("event", "")
        if ev.endswith("_turn"):
            phase = ev[:-5] if not ev.endswith("_turn") else ev[:-len("_turn")]
            tool = str(r.get("payload_preview") or "?").strip()
            if r.get("action") == "tool_calls":
                turns[(phase, tool)] += 1
                actions[phase] += 1
        elif ev == "test_run":
            test_runs += 1
        else:
            ops[ev] += 1
            if "abort" in ev:
                samples_aborted[ev.rsplit("_", 1)[0].replace("repair_s", "s")] += 1
            if "submit" in ev:
                samples_aborted["submit-" + ev.split("sample=")[-1]] = samples_aborted.get("submit", 0) + 1

    per_phase: dict[str, dict] = {}
    for phase in sorted({p for (p, _t) in turns}):
        tool_counts = Counter({t: c for (p, t), c in turns.items() if p == phase})
        reads = sum(c for t, c in tool_counts.items() if t in READ_TOOLS)
        writes = sum(c for t, c in tool_counts.items() if t in WRITE_TOOLS)
        tests = sum(c for t, c in tool_counts.items() if t in TEST_TOOLS)
        total = sum(tool_counts.values())
        per_phase[phase] = {
            "tool_calls": total,
            "tools": dict(tool_counts.most_common()),
            "read_share": round(reads / total, 2) if total else None,
            "write_share": round(writes / total, 2) if total else None,
            "test_share": round(tests / total, 2) if total else None,
            "top_tool": tool_counts.most_common(1)[0][0] if tool_counts else None,
            "read_write_ratio": round(reads / writes, 2) if writes else float("inf"),
        }
    return {
        "source": str(traj_path),
        "test_runs": test_runs,
        "pipeline_ops": dict(ops.most_common()),
        "phases": per_phase,
        "headline": {
            p: f"{v['tool_calls']} calls, {v['read_write_ratio']} read:write"
            for p, v in per_phase.items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trajectory", type=Path)
    ap.add_argument("--out", type=Path, default=None,
                    help="write JSON sidecar here (default: beside trajectory)")
    args = ap.parse_args()
    report = parse(args.trajectory)
    out = args.out or args.trajectory.parent / "tools.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"tool-call report -> {out}\n")
    width = max(len(p) for p in report["phases"] or [""])
    for phase, v in report["phases"].items():
        print(f"  {phase:{width}s}  {v['tool_calls']:4d} calls | r:w {v['read_write_ratio']!s:6} | "
              f"writes {int(round((v['write_share'] or 0) * v['tool_calls'])):3d} | "
              f"top: {v['top_tool']}")
        for t, c in list(v["tools"].items())[:8]:
            print(f"      {t:22s} {c}")
    print(f"\n  test_runs: {report['test_runs']}  ops: {report['headline']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())