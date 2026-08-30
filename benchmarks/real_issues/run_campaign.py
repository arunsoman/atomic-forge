#!/usr/bin/env python3
"""Campaign50 runner — every `atomic-forge fix` becomes one audited ledger row.

This is the campaign's evidence engine (see docs/valuation_action_plan.md
Phase 0). Protocol implemented per issue:

  0. rate guard  — unauthenticated /rate_limit probe; stop when the core
                   budget drops below --min-core (forks/PRs are core REST)
  1. state gate  — issue must be `open` right now (unauth curl; authed
                   budget is saved for forks/PRs). GraphQL is dead for the
                   current account, so nothing here ever touches it.
  2. run         — `atomic-forge fix <url> [--repro S] [--test-file T]
                   --work-root <durable>` from the repo root; stdout/stderr
                   captured; model/provider comes from the environment.
  3. ledger row  — appended to campaign50.ledger.jsonl (F8 economics:
                   llm_calls, prompt/completion tokens, wall clock)
  4. artifacts   — .forge/{exit_audit.jsonl,learning.json,trajectory.jsonl}
                   copied to logs/<owner>-<repo>-<n>/ (F5 — /tmp is not
                   trustworthy storage; pilot lost trajectories to cleanup)

Usage:
  FORGE_MODEL=nemotron-3-super:cloud FORGE_BASE_URL=http://localhost:11434/v1 \
  FORGE_API_KEY=ollama \
  uv run python benchmarks/real_issues/run_campaign.py \
      --tasks benchmarks/real_issues/batch1.json \
      --work-root ~/forge_campaign/work --limit 4 --raise-pr

  # dry: show state-gate verdicts + planned commands, run nothing
  ... run_campaign.py --tasks ... --plan

Ledger schema (one JSON object per line):
  {at, url, owner, repo, number, repro, test_file, outcome, stage,
   exit_reason, pr_url, llm_calls, prompt_tokens, completion_tokens,
   wall_s, rate_core_remaining}
  outcome: raised | aborted | blocked_state | blocked_rate | error_spawn
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = Path(__file__).resolve().parent / "campaign50.ledger.jsonl"
LOGS = Path(__file__).resolve().parent / "logs"
USAGE_RE = re.compile(r"llm_calls=(\d+)\s+prompt_tokens=(\d+)\s+completion_tokens=(\d+)")


def curl_json(path: str) -> dict | None:
    """Unauthenticated GitHub REST — the IP bucket, never the authed quota."""
    r = subprocess.run(["curl", "-sfL", f"https://api.github.com/{path}"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def rate_core_remaining() -> int:
    d = curl_json("rate_limit")
    if not d:
        return -1  # unknown; don't block on a failed probe
    return d["resources"]["core"]["remaining"]


def issue_state(owner: str, repo: str, number: int) -> str | None:
    d = curl_json(f"repos/{owner}/{repo}/issues/{number}")
    return d.get("state") if d else None


def last_exit_audit(work_root: Path, repo: str, number: int) -> dict | None:
    p = work_root / f"{repo}-{number}" / ".forge" / "exit_audit.jsonl"
    if not p.exists():
        return None
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def copy_artifacts(work_root: Path, repo: str, number: int, slug: str) -> list[str]:
    src = work_root / f"{repo}-{number}" / ".forge"
    if not src.exists():
        return []
    dest = LOGS / slug
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("exit_audit.jsonl", "learning.json", "trajectory.jsonl"):
        f = src / name
        if f.exists():
            shutil.copy2(f, dest / name)
            copied.append(name)
    return copied


def build_cmd(task: dict, args) -> list[str]:
    cmd = ["uv", "run", "atomic-forge", "fix", task["url"]]
    if task.get("repro"):
        cmd += ["--repro", str(Path(task["repro"]))]
    if task.get("test_file"):
        cmd += ["--test-file", task["test_file"]]
    if args.work_root:
        cmd += ["--work-root", str(args.work_root)]
    if args.raise_pr:
        cmd.append("--raise-pr")
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def append_ledger(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def already_logged(url: str) -> bool:
    if not LEDGER.exists():
        return False
    return any(url in line for line in LEDGER.read_text().splitlines())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", required=True, help="batch JSON: [{url, repro?, test_file?}]")
    ap.add_argument("--work-root", required=True,
                    help="durable dir for cold-clone checkouts (not /tmp — F5)")
    ap.add_argument("--limit", type=int, default=None, help="max issues this invocation")
    ap.add_argument("--min-core", type=int, default=8,
                    help="stop when authenticated core remaining < this (forks/PRs)")
    ap.add_argument("--raise-pr", action="store_true", default=True)
    ap.add_argument("--no-raise-pr", dest="raise_pr", action="store_false")
    ap.add_argument("--dry-run", action="store_true", help="forge --dry-run (no push/PR)")
    ap.add_argument("--plan", action="store_true",
                    help="state-gate + print commands only; run nothing")
    ap.add_argument("--retry-logged", action="store_true",
                    help="re-run issues already present in the ledger")
    args = ap.parse_args()

    loaded = json.loads(Path(args.tasks).read_text())
    tasks = loaded["tasks"] if isinstance(loaded, dict) else loaded
    LOGS.mkdir(parents=True, exist_ok=True)
    work_root = Path(args.work_root).expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    ran = 0
    summary = []
    for task in tasks:
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", task["url"])
        owner, repo, number = m.group(1), m.group(2), int(m.group(3))
        slug = f"{owner}-{repo}-{number}"

        if already_logged(task["url"]) and not args.retry_logged:
            summary.append((slug, "skip_logged"))
            continue
        if args.limit is not None and ran >= args.limit:
            summary.append((slug, "skip_limit"))
            continue

        # 1. state gate (C1) — unauthenticated, conserves the authed quota
        state = issue_state(owner, repo, number)
        if state != "open":
            append_ledger({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "url": task["url"], "owner": owner, "repo": repo,
                           "number": number, "outcome": "blocked_state",
                           "exit_reason": f"issue_{state or 'unknown'}", "stage": "intake"})
            summary.append((slug, f"blocked_state({state})"))
            print(f"[campaign] {slug}: state={state} — skipped")
            continue

        # 0. rate guard (against forks/PRs, which are authenticated core REST)
        core_left = rate_core_remaining()
        if args.plan or (0 <= core_left < args.min_core):
            core_note = f"core<={args.min_core} remaining:{core_left}"
            if not args.plan:
                print(f"[campaign] {slug}: rate guard triggered ({core_note}) — stopping batch")
                summary.append((slug, "blocked_rate"))
                break

        cmd = build_cmd(task, args)

        if args.plan:
            print(f"[campaign plan] {slug} state=open\n  {' '.join(str(c) for c in cmd)}")
            summary.append((slug, "planned"))
            continue

        # 2. run
        print(f"[campaign] {slug}: firing ...")
        t0 = time.time()
        out_path = LOGS / f"{slug}.campaign.out"
        try:
            with out_path.open("w") as out:
                proc = subprocess.run(cmd, cwd=str(REPO_ROOT), stdout=out,
                                      stderr=subprocess.STDOUT,
                                      env=os.environ.copy(), timeout=7200)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -9
        except OSError as e:
            append_ledger({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "url": task["url"], "owner": owner, "repo": repo,
                           "number": number, "outcome": "error_spawn",
                           "exit_reason": str(e)[:120], "wall_s": round(time.time() - t0, 1)})
            summary.append((slug, "error_spawn"))
            continue
        wall_s = round(time.time() - t0, 1)

        # 3. evidence extraction
        out_text = out_path.read_text(errors="replace")
        usage = USAGE_RE.search(out_text)
        pr_m = re.search(r"pr-url=(\S+)", out_text)
        audit = last_exit_audit(work_root, repo, number) or {}
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "url": task["url"], "owner": owner, "repo": repo, "number": number,
               "repro": task.get("repro"), "test_file": task.get("test_file"),
               "rc": rc, "wall_s": wall_s,
               "outcome": ("raised" if (audit.get("reason") == "success" and args.raise_pr
                                        and not args.dry_run) else "aborted"),
               "stage": (audit.get("detail", "") or "")[:120] if audit else "",
               "exit_reason": audit.get("reason", f"rc={rc}"),
               "pr_url": pr_m.group(1) if pr_m and pr_m.group(1) != "" else None,
               "llm_calls": int(usage.group(1)) if usage else None,
               "prompt_tokens": int(usage.group(2)) if usage else None,
               "completion_tokens": int(usage.group(3)) if usage else None,
               "rate_core_remaining": core_left}
        # artifact preservation (F5) — before anything else can clean /tmp
        copied = copy_artifacts(work_root, repo, number, slug)
        if copied:
            row["artifacts"] = copied
        # tool-call log (which tools did the LLM call, per phase) — the
        # run-economics instrument; failures and successes both feed it
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from tools_log import parse as tools_parse  # noqa: PLC0415
            tool_report = tools_parse(LOGS / slug / "trajectory.jsonl")
            (LOGS / slug / "tools.json").write_text(json.dumps(tool_report, indent=2) + "\n")
            row["tool_calls"] = sum(v["tool_calls"] for v in tool_report["phases"].values())
            row["tool_report"] = tool_report["headline"]
        except Exception as e:  # noqa: BLE001 — logging must never fail the run
            row["tool_calls"] = None
            print(f"[campaign] {slug}: tool-log unavailable ({e})")
        row["model"] = os.environ.get("FORGE_MODEL", "default")
        append_ledger(row)
        print(f"[campaign] {slug}: {row['exit_reason']} ({row['wall_s']}s, "
              f"prompt={row['prompt_tokens']}, completion={row['completion_tokens']})")
        summary.append((slug, row["exit_reason"]))
        ran += 1
        time.sleep(5)  # be a good upstream citizen between runs

    print("\n=== campaign batch summary ===")
    for slug, outcome in summary:
        print(f"  {slug:40s} {outcome}")
    ok = sum(1 for _, o in summary if o in ("raised", "success"))
    print(f"\nledger: {LEDGER}  |  raised/success: {ok}/{len(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())