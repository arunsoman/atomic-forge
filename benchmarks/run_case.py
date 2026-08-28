#!/usr/bin/env python3
"""
Runs one benchmark case (see cases/<id>/case.json) through the real
atomic-forge CLI — subprocess, not an in-process call, so this measures
exactly what a user running the CLI gets — and records real numbers:
resolution, repair rounds, blast-radius-gate rejects, tokens, wall time.

Usage:
    python benchmarks/run_case.py more_itertools_chunked_negative_n
    python benchmarks/run_case.py --all
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
CASES_DIR = BENCH_DIR / "cases"
RESULTS_DIR = BENCH_DIR / "results"

REPAIR_LINE_RE = re.compile(
    r"\[repair\] (GREEN|EXHAUSTED) — failures (\d+) -> (\d+) in (\d+) round"
)
USAGE_LINE_RE = re.compile(
    r"llm_calls=(\d+) prompt_tokens=(\d+) completion_tokens=(\d+)"
)


def run_case(case_id: str, forge_bin: str = "atomic-forge") -> dict:
    case_dir = CASES_DIR / case_id
    case_meta = json.loads((case_dir / "case.json").read_text())
    task_path = case_dir / "task.json"

    work_dir = RESULTS_DIR / case_id / "workdir"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    for f in (case_dir / "seed").iterdir():
        if f.is_file():
            shutil.copy(f, work_dir / f.name)

    phase = "repair" if case_meta.get("mode") == "repair_only" else "run"
    t0 = time.time()
    proc = subprocess.run(
        [forge_bin, phase, "--tasks", str(task_path), "--project-dir", str(work_dir),
         "--report", "jsonl"],
        capture_output=True, text=True, timeout=case_meta.get("timeout_s", 900),
    )
    elapsed = time.time() - t0
    stdout = proc.stdout

    m = REPAIR_LINE_RE.search(stdout)
    usage_m = USAGE_LINE_RE.search(stdout)
    trajectory_path = work_dir / ".forge" / "trajectory.jsonl"
    blast_rejects = 0
    if trajectory_path.exists():
        for line in trajectory_path.read_text().splitlines():
            if '"winner rejected by blast-radius gate"' in line:
                blast_rejects += 1

    result = {
        "case_id": case_id,
        "source_pr": case_meta.get("source", {}).get("pr_url"),
        "phase": phase,
        "success": bool(m and m.group(1) == "GREEN"),
        "initial_failures": int(m.group(2)) if m else None,
        "final_failures": int(m.group(3)) if m else None,
        "repair_rounds": int(m.group(4)) if m else None,
        "blast_radius_rejects": blast_rejects,
        "llm_calls": int(usage_m.group(1)) if usage_m else None,
        "prompt_tokens": int(usage_m.group(2)) if usage_m else None,
        "completion_tokens": int(usage_m.group(3)) if usage_m else None,
        "wall_time_s": round(elapsed, 1),
        "exit_code": proc.returncode,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": proc.stderr[-2000:],
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{case_id}.json"
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("case_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--forge-bin", default="atomic-forge")
    args = p.parse_args(argv)

    case_ids = (
        sorted(d.name for d in CASES_DIR.iterdir() if d.is_dir())
        if args.all else [args.case_id]
    )
    if not case_ids or case_ids == [None]:
        p.error("pass a case_id or --all")

    ok = True
    for cid in case_ids:
        print(f"--- {cid} ---")
        r = run_case(cid, forge_bin=args.forge_bin)
        state = "PASS" if r["success"] else "FAIL"
        print(f"  {state}  failures {r['initial_failures']} -> {r['final_failures']} "
              f"in {r['repair_rounds']} round(s), "
              f"blast_radius_rejects={r['blast_radius_rejects']}, "
              f"tokens={r['prompt_tokens']}+{r['completion_tokens']}, "
              f"{r['wall_time_s']}s")
        ok = ok and r["success"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
