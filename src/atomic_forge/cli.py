"""
atomic-forge CLI.

    python -m atomic_forge run --tasks tasks.json --project-dir ./out
        # agentic generate (tools) -> QA tests -> agentic SOTA repair loop

Phases: run | generate | qa | repair
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .batch_io import load_batch_json
from .llm import default_llm
from .qa import qa_phase
from .repair_agent import DEFAULT_TEST_CMD, repair_loop_agentic
from .reporter import make_reporter
from .sandbox import ensure_repo
from .tools import make_tools
from .trajectory import Trajectory


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="atomic-forge",
                                description="Agentic code generation + SOTA repair loop.")
    p.add_argument("phase", choices=["run", "generate", "qa", "repair"])
    p.add_argument("--tasks", default="tasks.json")
    p.add_argument("--project-dir", default="./forge_out")
    p.add_argument("--test-cmd", default=None, help="force a test command (default: auto-detect)")
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--samples", type=int, default=2, help="patch candidates per repair round")
    p.add_argument("--report", choices=["none", "jsonl"], default="none",
                   help="write artifacts/status/repair events to .forge/reports.jsonl")
    p.add_argument("--timeout", type=int, default=300)
    args = p.parse_args(argv)

    llm = default_llm()
    batch = load_batch_json(args.tasks)
    project_dir = Path(args.project_dir).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    traj = Trajectory(project_dir)
    repo_ok = ensure_repo(project_dir)
    tools = make_tools(project_dir)
    reporter = make_reporter(args.report, project_dir=str(project_dir))
    traj.log("start", phase=args.phase, tasks=len(batch.tasks), git=repo_ok)
    print(f"[forge] {len(batch.tasks)} task(s) -> {project_dir} "
          f"(report={reporter.name()}, git={'on' if repo_ok else 'OFF'})")

    gen_failed: list = []
    gen_skipped: list = []
    if args.phase in ("run", "generate"):
        print("[forge] phase 1/3: generating files...")
        from .generate_agent import generate_batch_agentic
        gen_result = generate_batch_agentic(project_dir, batch, llm, tools, traj, reporter=reporter)
        gen_failed, gen_skipped = gen_result.failed, gen_result.skipped
        for w in gen_result.written:
            print(f"  wrote {w.relative_to(project_dir)}")
        for f in gen_failed:
            print(f"  FAILED {f.file_path} (task {f.name}): {f.reason}")
        for s in gen_skipped:
            print(f"  SKIPPED {s.file_path} (task {s.name}): {s.reason}")

    if args.phase in ("run", "qa"):
        print("[forge] phase 2/3: generating tests from TestTriads...")
        for t in qa_phase(project_dir, batch, llm, tools, traj, reporter=reporter):
            print(f"  wrote {t.relative_to(project_dir)}")
        tools.reindex()

    if args.phase in ("run", "repair"):
        print("[forge] phase 3/3: testing + repair loop...")
        report = repair_loop_agentic(project_dir, llm, tools, traj,
                                     test_cmd=args.test_cmd, max_rounds=args.max_rounds,
                                     samples=args.samples, timeout=args.timeout,
                                     reporter=reporter,
                                     tasks_by_file={t.file_path: t.name for t in batch.dev_tasks()})
        state = "GREEN" if report["success"] else "EXHAUSTED"
        print(f"[repair] {state} — failures {report['initial_failures']} -> "
              f"{report['final_failures']} in {report['rounds']} round(s); "
              f"touched: {', '.join(report.get('repaired_files', [])) or 'none'}")
        ok = report["success"]
        usage = getattr(llm, "usage", None)
        if usage:
            print(f"[forge] {usage.summary()}")
        print(f"[forge] trajectory: {traj.path}")
        return 0 if ok else 1

    print(f"[forge] trajectory: {traj.path}")
    return 1 if gen_failed else 0


if __name__ == "__main__":
    sys.exit(main())
