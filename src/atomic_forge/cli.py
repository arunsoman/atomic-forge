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

    python -m atomic_forge fix https://github.com/<owner>/<repo>/issues/<N>
        # one-shot issue -> PR: CIE (required) indexes the repo and generates a
        # failing regression test from the issue, forge's repair loop fixes the
        # bug against it, and on green a PR is opened from your FORK (never
        # pushed to origin). --dry-run does everything except the push/PR.

    python -m atomic_forge fix-comment --repo <owner>/<repo> --file <path> \
        --comment-body "this looks off-by-one" [--line N] [--source-url ...]
        # review-comment-driven fix (R8): same pipeline as `fix`, but the bug
        # description comes from a review comment already anchored to a file
        # (+ optional line) instead of a GitHub issue fetch — localization
        # starts scoped to that file. --comment-body-file - reads the comment
        # from stdin. Same fork-only PR / --dry-run semantics as `fix`.

Phases: run | generate | qa | repair | decompose | watch | fix | fix-comment
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from .batch_io import load_batch_json
from .decompose import decompose_spec, write_draft_json
from .llm import default_llm
from .pr import prepare_pr_branch, raise_pr, summarize_repair_for_pr
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
    p.add_argument("phase", choices=["run", "generate", "qa", "repair", "decompose", "watch", "fix", "fix-comment"])
    p.add_argument("--tasks", default="tasks.json")
    p.add_argument("--project-dir", default="./forge_out")
    p.add_argument("--test-cmd", default=None, help="force a test command (default: auto-detect)")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="repair/fix max rounds (default: 3 for repair, 5 for fix)")
    p.add_argument("--samples", type=int, default=2, help="patch candidates per repair round")
    p.add_argument("--architect", action="store_true",
                   help="[repair/fix] opt-in planner pass before each round's K-sampling "
                        "(one extra LLM call; not yet validated to improve fix-rate — see "
                        "the wiki page 'Planner / Executor Split' (R3)). Default off.")
    p.add_argument("--local-only", action="store_true",
                   help="refuse to run against a non-loopback/private LLM endpoint (R15) — "
                        "enforces that nothing leaves this machine, e.g. a local Ollama/"
                        "vLLM/llama.cpp server. Rejects OpenAI/OpenRouter/any hosted "
                        "endpoint outright instead of silently proceeding.")
    p.add_argument("--report", choices=["none", "jsonl"], default="none",
                   help="write artifacts/status/repair events to .forge/reports.jsonl")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--raise-pr", action="store_true",
                   help="[repair] after a green repair, push the fix on a fresh branch to "
                        "`origin` and open a GitHub PR with `gh` (needs gh authenticated).")
    p.add_argument("--pr-base", default=None, help="[--raise-pr] PR base branch (default: repo default).")
    p.add_argument("--pr-branch", default=None, help="[--raise-pr] feature branch name (default: forge/fix-<ts>).")
    p.add_argument("--pr-title", default=None, help="[--raise-pr] override the PR title.")
    p.add_argument("--pr-body-file", default=None, help="[--raise-pr] path to a markdown PR body.")
    # --- fix <github_issue_url> ---
    p.add_argument("url", nargs="?", default=None,
                   help="[fix] GitHub issue URL: https://github.com/<owner>/<repo>/issues/<N>")
    p.add_argument("--install-cmd", default=None,
                   help="[fix] override the project install command (e.g. 'pip install -e .'); "
                        "pass an empty string to skip installing the project entirely.")
    p.add_argument("--max-turns", type=int, default=10, help="[fix] max test-generation agent turns")
    p.add_argument("--dry-run", action="store_true",
                   help="[fix] do everything except push to the fork / open the PR.")
    p.add_argument("--repo", default=None,
                   help="[fix-comment] owner/repo (e.g. 'octocat/Hello-World')")
    p.add_argument("--comment-body", default=None,
                   help="[fix-comment] the review comment text (or use --comment-body-file, "
                        "or pipe via --comment-body-file -)")
    p.add_argument("--comment-body-file", default=None,
                   help="[fix-comment] read the comment text from this file ('-' for stdin)")
    p.add_argument("--file", dest="comment_file_path", default=None,
                   help="[fix-comment] the file path (repo-relative) the comment was anchored to")
    p.add_argument("--line", type=int, default=None, help="[fix-comment] the line the comment was anchored to")
    p.add_argument("--source-url", default=None, help="[fix-comment] the PR/comment URL, for the PR body's 'Fixes' link")
    p.add_argument("--issue-body-file", default=None,
                   help="[fix] use this file as the issue body instead of fetching it via gh "
                        "(the URL is still needed for owner/repo/number). Pass '-' to read "
                        "the body from stdin instead, e.g. `echo \"bug text\" | atomic-forge "
                        "fix <url> --issue-body-file -`.")
    p.add_argument("--repro", default=None,
                   help="[fix] path to a repro probe script (.py runs under the project "
                        "venv; anything else under bash). Contract: exit non-zero while "
                        "the bug is present, exit 0 once fixed. forge runs it on HEAD "
                        "before any LLM spend — exit 0 aborts as issue_already_fixed — "
                        "and again after repair; a non-zero exit then blocks the PR as "
                        "repro_still_failing (independent second witness).")
    p.add_argument("--skip-bootstrap", action="store_true",
                   help="[fix] skip the R16 bootstrap gate (test-probe) on a cold clone "
                        "whose suite you already know runs. --project-dir checkouts "
                        "never gate.")
    p.add_argument("--bootstrap-timeout", type=int, default=600,
                   help="[fix] seconds the bootstrap gate's test probe may run")
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

    try:
        llm = default_llm(local_only=args.local_only)
    except RuntimeError as e:
        print(f"[forge] {e}", file=sys.stderr)
        return 2

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

    if args.phase == "fix-comment":
        from .fix import run_fix_from_comment
        missing = [n for n, v in (("--repo", args.repo), ("--file", args.comment_file_path)) if not v]
        if missing:
            print(f"[forge] fix-comment requires {', '.join(missing)}", file=sys.stderr)
            return 2
        if args.comment_body_file:
            comment_body = sys.stdin.read() if args.comment_body_file == "-" else Path(args.comment_body_file).read_text()
        elif args.comment_body:
            comment_body = args.comment_body
        else:
            print("[forge] fix-comment requires --comment-body or --comment-body-file", file=sys.stderr)
            return 2
        owner, repo = args.repo.split("/", 1) if "/" in args.repo else (None, None)
        if not owner:
            print(f"[forge] --repo must be 'owner/repo', got {args.repo!r}", file=sys.stderr)
            return 2
        project_dir = (Path(args.project_dir) if args.project_dir and args.project_dir != "./forge_out"
                       else None)
        r = run_fix_from_comment(
            owner, repo, comment_body, args.comment_file_path, llm,
            line=args.line, source_url=args.source_url, project_dir=project_dir,
            install_cmd=args.install_cmd, max_rounds=args.max_rounds or 5,
            max_turns=args.max_turns, dry_run=args.dry_run, pr_base=args.pr_base,
            pr_branch=args.pr_branch, pr_title=args.pr_title, samples=args.samples,
            architect_mode=args.architect, skip_bootstrap=args.skip_bootstrap,
            bootstrap_timeout=args.bootstrap_timeout,
        )
        # Machine-parseable, in addition to the human-readable prints
        # already inside run_fix_from_comment — entrypoint.sh (the GitHub
        # Action wrapper) greps this exact prefix to populate the
        # Action's `pr-url` output without CLI/Action coupling beyond one
        # stable line.
        print(f"[forge] pr-url={r.get('pr_url') or ''}")
        return 0 if r.get("success") else 1

    if args.phase == "fix":
        from .fix import run_fix
        if not args.url:
            print("[forge] fix requires a GitHub issue URL:\n"
                  "  atomic-forge fix https://github.com/<owner>/<repo>/issues/<N>", file=sys.stderr)
            return 2
        project_dir = (Path(args.project_dir) if args.project_dir and args.project_dir != "./forge_out"
                       else None)
        issue_body_file = Path(args.issue_body_file) if args.issue_body_file else None
        repro = Path(args.repro) if args.repro else None
        r = run_fix(args.url, llm, project_dir=project_dir, install_cmd=args.install_cmd,
                    max_rounds=args.max_rounds or 5, max_turns=args.max_turns,
                    dry_run=args.dry_run, pr_base=args.pr_base, pr_branch=args.pr_branch,
                    pr_title=args.pr_title, issue_body_file=issue_body_file, samples=args.samples,
                    architect_mode=args.architect, skip_bootstrap=args.skip_bootstrap,
                    bootstrap_timeout=args.bootstrap_timeout, repro=repro)
        print(f"[forge] pr-url={r.get('pr_url') or ''}")
        return 0 if r.get("success") else 1

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
        if args.raise_pr and args.phase == "repair":
            try:
                pr_branch = prepare_pr_branch(project_dir, args.pr_branch)
                print(f"[forge] --raise-pr: working on branch {pr_branch}")
            except Exception as e:
                print(f"[forge] --raise-pr: could not prepare branch ({e}); continuing on current branch")
        print("[forge] phase 3/3: testing + repair loop...")
        report = repair_loop_agentic(project_dir, llm, tools, traj,
                                     test_cmd=args.test_cmd, max_rounds=args.max_rounds or 3,
                                     samples=args.samples, timeout=args.timeout,
                                     reporter=reporter, architect_mode=args.architect,
                                     tasks_by_file={t.file_path: t.name for t in batch.dev_tasks()})
        state = "GREEN" if report["success"] else "EXHAUSTED"
        print(f"[repair] {state} — failures {report['initial_failures']} -> "
              f"{report['final_failures']} in {report['rounds']} round(s); "
              f"touched: {', '.join(report.get('repaired_files', [])) or 'none'}")
        ok = report["success"]
        usage = getattr(llm, "usage", None)
        if usage:
            print(f"[forge] {usage.summary()}")
        if ok and args.raise_pr:
            body = ""
            if args.pr_body_file:
                try:
                    body = Path(args.pr_body_file).read_text()
                except OSError:
                    body = ""
            if not body:
                _title, body = summarize_repair_for_pr(report)
            title = args.pr_title or _title
            try:
                pr = raise_pr(project_dir, title=title, body=body, base=args.pr_base)
                print(f"[forge] PR opened: {pr.get('pr_url')} "
                      f"(base {pr.get('base')} <- {pr.get('branch')})")
            except Exception as e:
                print(f"[forge] --raise-pr failed: {e}")
                ok = False
        print(f"[forge] trajectory: {traj.path}")
        return 0 if ok else 1

    print(f"[forge] trajectory: {traj.path}")
    return 1 if gen_failed else 0


if __name__ == "__main__":
    sys.exit(main())
