#!/usr/bin/env python3
"""Fix-sweep driver: curate open real issues -> run `atomic-forge fix` per
issue -> raise fork-only PRs -> log everything, resumably.

Every run appends one JSON line to results.jsonl:
  {issue, repo, status: pr_raised|oracle_reject|bootstrap_fail|repair_fail|
   crashed, pr_url, seconds, rounds}

Sweep is resumable: issues already logged are skipped on re-run, so a
60-100+ issue campaign is just this command run repeatedly (cron or
`run`-from-sweeps). Etiquette caps built in: --per-repo limits how many
issues one sweep attempts per target repo.

    FORGE_MODEL=qwen3.5:cloud FORGE_BASE_URL=http://localhost:11434/v1 \
    FORGE_API_KEY=ollama FORGE_ENABLE_AGENTIC_BOOTSTRAP=1 \
    .venv/bin/python benchmarks/real_issues/sweep.py --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANDIDATES = HERE / "sweep" / "candidates.jsonl"
RESULTS = HERE / "sweep" / "results.jsonl"

PY = sys.executable

# statuses we emit; never silently swallowed
_OK = "pr_raised"
_FAIL = {"oracle_reject": "no (validated) regression test at HEAD",
         "bootstrap_fail": "environment bootstrap exceeded caps",
         "repair_fail": "repair loop ended non-green",
         "error": "other error"}


def load_results(path: Path) -> set[str]:
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                done.add(json.loads(line).get("issue", ""))
            except json.JSONDecodeError:
                continue
    return done


def classify(stdout: str) -> str:
    if "PR opened:" in stdout:
        return _OK
    if "upstream blocks PR creation" in stdout:
        return "pr_locked"   # maintainer-side PR gate (policy), not our failure
    if "abort at bootstrap gate" in stdout or "abort: no regression test" in stdout:
        return "bootstrap_fail" if "bootstrap" in stdout else "oracle_reject"
    if "no regression test generated; no PR raised" in stdout:
        return "oracle_reject"
    return "repair_fail"


def sweep(issues: list[dict], env: dict, budget_min: float,
          per_repo: int, results_path: Path, timeout_s: int,
          limit: int = 0, max_repo: int = 1,
          args_excludes: list[str] | None = None) -> None:
    args_excludes = args_excludes or []
    done = load_results(results_path)
    # per-repo prior-attempt quota: one logged attempt (any status) reserves a
    # repo's chance; hard repos shouldn't eat the whole sweep budget
    repo_counts: dict[str, int] = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            try:
                r = json.loads(line).get("repo", "")
                repo_counts[r] = repo_counts.get(r, 0) + 1
            except json.JSONDecodeError:
                continue
    # apply skip + per-repo caps BEFORE the limit slice, so `--limit N`
    # always means "N fresh attempts"
    issues = [c for c in issues if c["url"] not in done]
    skipped = [c for c in issues if not c.get("pr_ok", True)]
    for c in skipped:
        print(f"skip {c['url']} (upstream PRs gated to collaborators: "
              f"{c.get('pr_note', '')[:60]})", file=sys.stderr)
    issues = [c for c in issues if c.get("pr_ok", True)]
    issues = [c for c in issues
              if not any(x in c["repo"] for x in args_excludes)]
    clean = [c for c in issues if repo_counts.get(c["repo"], 0) < max_repo]
    print(f"repo quota({max_repo}): skipping {len(issues) - len(clean)} "
          f"issue(s) in at-quota repos", file=sys.stderr)
    issues = clean
    seen: dict[str, int] = {}
    issues = [c for c in issues
              if seen.update({c["repo"]: seen.get(c["repo"], 0) + 1}) is None
              and seen[c["repo"]] <= per_repo]
    if limit:
        issues = issues[:limit]
    started = time.monotonic()
    used_per_repo: dict[str, int] = {}
    ok = fail = 0
    for cand in issues:
        repo = cand["repo"]
        if used_per_repo.get(repo, 0) >= per_repo:
            print(f"skip {cand['url']} (per-repo cap {per_repo} at {repo})",
                  file=sys.stderr)
            continue
        if (time.monotonic() - started) > budget_min * 60:  # wall-clock stop
            print(f"budget exhausted ({used_per_repo} attempted)", file=sys.stderr)
            break
        print(f"=== {repo}#{cand['number']} — {cand['title'][:70]}", file=sys.stderr)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                ["atomic-forge", "fix", cand["url"], "--raise-pr",
                 "--max-rounds", "3", "--bootstrap-timeout", "900"],
                capture_output=True, text=True, timeout=timeout_s,
                env={**os.environ, **env})
            stdout, rc = proc.stdout + "\n" + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else ""
            rc = -9
        seconds = round(time.monotonic() - t0, 1)
        pr_url: str | None = None
        import re
        m = re.search(r"PR opened: (\S+)", stdout)
        if m:
            pr_url = m.group(1)
            status = _OK
            ok += 1
        else:
            pr_url = None
            status = classify(stdout)
            fail += 1
        tail = "\n".join(stdout.strip().splitlines()[-25:])
        rec = {"issue": cand["url"], "repo": repo, "number": cand["number"],
               "title": cand["title"], "status": status,
               "pr_url": pr_url, "seconds": seconds,
               "returncode": rc,
               "model": env.get("FORGE_MODEL"), "at": time.strftime("%F %T"),
               "log_tail": tail if pr_url is None else None}
        with results_path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if pr_url:
            print(f"  PR: {pr_url}  ({seconds}s)", file=sys.stderr)
        else:
            tail = [l for l in stdout.splitlines() if "abort" in l or "abort:" in l]
            print(f"  {status} ({seconds}s): {tail[-1][:110] if tail else ''}",
                  file=sys.stderr)
        used_per_repo[repo] = used_per_repo.get(repo, 0) + 1
    print(f"sweep: {ok} PR(s) raised, {fail} not — {ok + fail} attempted",
          file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default=str(HERE / "sweep" / "candidates.jsonl"))
    ap.add_argument("--results", default=str(RESULTS))
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--per-repo", type=int, default=2)
    ap.add_argument("--budget-minutes", type=float, default=180)
    ap.add_argument("--timeout-seconds", type=int, default=2100)
    ap.add_argument("--exclude", action="append", default=[],
                    help="skip this repo substring (repeatable); e.g. "
                         "--exclude pydantic for Python-only-fix batches")
    ap.add_argument("--max-repo", type=int, default=2,
                    help="max logged attempts per repo across sweeps (retry "
                         "with a higher value after pipeline improvements)")
    args = ap.parse_args()

    env = {k: v for k, v in os.environ.items()
           if k.startswith(("FORGE_", "GH_"))}
    env.setdefault("FORGE_MODEL", "qwen3.5:cloud")
    env.setdefault("FORGE_BASE_URL", "http://localhost:11434/v1")
    env.setdefault("FORGE_API_KEY", "ollama")
    env.setdefault("FORGE_ENABLE_AGENTIC_BOOTSTRAP", "1")
    # the fix pipeline can leave OUR venv's pydantic/pydantic-core desynced
    # (a known razor-burn: healed force-aligned before each batch)
    subprocess.run([PY, "-m", "pip", "install", "-q",
                    "pydantic==2.13.5", "pydantic-core==2.46.5"],
                   capture_output=True, timeout=300)
    rows = [json.loads(l) for l in
            Path(args.candidates).read_text().splitlines()]
    rows.sort(key=lambda r: -r["score"])
    sweep(rows, env, args.budget_minutes, args.per_repo,
          Path(args.results), args.timeout_seconds, limit=args.limit,
          max_repo=args.max_repo, args_excludes=args.exclude)
    return 0


if __name__ == "__main__":
    sys.exit(main())