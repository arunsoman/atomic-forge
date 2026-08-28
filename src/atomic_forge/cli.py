"""
atomic-forge CLI.

    python -m atomic_forge run --tasks tasks.json --project-dir ./out
        # agentic generate (tools) -> QA tests -> agentic SOTA repair loop

    python -m atomic_forge decompose --spec spec.md --out tasks.draft.json
        # optional on-ramp: LLM drafts AtomicTask JSON from a loose spec,
        # for a human to review/edit BEFORE it's handed to `run`

    python -m atomic_forge watch --project-dir ./out --log-file /var/log/app.log \
        --deploy-cmd "python app.py {port}"
        # production watchdog: tail a log for tracebacks, repair them with
        # the same localize/sample/select loop `repair` uses, canary the
        # fix (real subprocess + real traffic split) and promote/rollback
        # on a real health check. --deploy-cmd is optional: omit it to
        # just detect+patch+commit with no canary phase.

Phases: run | generate | qa | repair | decompose | watch
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from .batch_io import load_batch_json
from .decompose import decompose_spec, write_draft_json
from .llm import default_llm
from .qa import qa_phase
from .repair_agent import DEFAULT_TEST_CMD, repair_loop_agentic
from .reporter import make_reporter
from .sandbox import ensure_repo
from .tools import make_tools
from .trajectory import Trajectory
from .watchdog import LocalProcessCanaryDeployer, LogFailureDetector, WatchdogLoop


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="atomic-forge",
                                description="Agentic code generation + SOTA repair loop.")
    p.add_argument("phase", choices=["run", "generate", "qa", "repair", "decompose", "watch"])
    p.add_argument("--tasks", default="tasks.json")
    p.add_argument("--project-dir", default="./forge_out")
    p.add_argument("--test-cmd", default=None, help="force a test command (default: auto-detect)")
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--samples", type=int, default=2, help="patch candidates per repair round")
    p.add_argument("--report", choices=["none", "jsonl"], default="none",
                   help="write artifacts/status/repair events to .forge/reports.jsonl")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--backend", choices=["local", "graph"], default="local",
                   help="tool backend: 'local' (in-memory, rebuilt per process) or "
                        "'graph' (persisted SQLite call graph, .forge/codegraph.db)")
    p.add_argument("--spec", default=None, help="[decompose] path to a spec/issue text or markdown file")
    p.add_argument("--out", default="tasks.draft.json", help="[decompose] where to write the draft AtomicTaskBatch JSON")
    p.add_argument("--log-file", default=None, help="[watch] log file to tail for tracebacks")
    p.add_argument("--deploy-cmd", default=None,
                   help="[watch] shell-quoted argv to start the app, with a literal {port} token, "
                        "e.g. 'python app.py {port}'. Omit to patch+commit with no canary phase.")
    p.add_argument("--health-path", default="/", help="[watch] HTTP path the canary is health-checked on")
    p.add_argument("--canary-percent", type=int, default=10, help="[watch] traffic %% sent to the canary")
    p.add_argument("--health-checks", type=int, default=5, help="[watch] consecutive healthy checks required to promote")
    p.add_argument("--poll-interval", type=float, default=5.0, help="[watch] seconds between log polls")
    p.add_argument("--max-cycles", type=int, default=None,
                   help="[watch] stop after N poll cycles (omit to run forever)")
    args = p.parse_args(argv)

    llm = default_llm()

    if args.phase == "decompose":
        if not args.spec:
            print("[forge] decompose requires --spec <file>", file=sys.stderr)
            return 2
        spec_text = Path(args.spec).read_text()
        result = decompose_spec(spec_text, llm)
        out_path = write_draft_json(result, args.out)
        print(f"[forge] decompose: {result.summary()} -> {out_path}")
        for r in result.rejected:
            print(f"  REJECTED {r.raw.get('file_path', r.raw.get('name', '?'))}: {r.error.splitlines()[0]}")
        print("[forge] DRAFT ONLY — review and edit before running `atomic-forge run --tasks "
              f"{out_path}`; this was not validated the way a hand-written batch is.")
        return 0 if result.tasks else 1

    if args.phase == "watch":
        if not args.log_file:
            print("[forge] watch requires --log-file <path>", file=sys.stderr)
            return 2
        project_dir = Path(args.project_dir).resolve()
        project_dir.mkdir(parents=True, exist_ok=True)
        traj = Trajectory(project_dir)
        ensure_repo(project_dir)
        tools = make_tools(project_dir, backend=args.backend)
        reporter = make_reporter(args.report, project_dir=str(project_dir))
        detector = LogFailureDetector(args.log_file)
        deployer = None
        if args.deploy_cmd:
            deployer = LocalProcessCanaryDeployer(
                start_cmd=shlex.split(args.deploy_cmd), health_path=args.health_path,
            )
        loop = WatchdogLoop(project_dir, llm, tools, traj, detector, deployer=deployer,
                            reporter=reporter, canary_percent=args.canary_percent,
                            health_checks=args.health_checks)
        print(f"[forge] watch: tailing {args.log_file} -> {project_dir} "
              f"(canary={'on' if deployer else 'off'}, poll={args.poll_interval}s)")
        try:
            if args.max_cycles is not None:
                loop.run_forever(poll_interval=args.poll_interval, max_cycles=args.max_cycles)
            else:
                loop.run_forever(poll_interval=args.poll_interval)
        finally:
            if deployer is not None:
                deployer.teardown_all()
        return 0

    batch = load_batch_json(args.tasks)
    project_dir = Path(args.project_dir).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    traj = Trajectory(project_dir)
    repo_ok = ensure_repo(project_dir)
    tools = make_tools(project_dir, backend=args.backend)
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
