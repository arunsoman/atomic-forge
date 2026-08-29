#!/usr/bin/env python3
"""PR-writability probe: before spending fix work on an issue, check that
the upstream repo even ACCEPTS pull requests from non-collaborators.

Several prominent repos (Textualize/rich as of 2026-07) now gate PR
creation to collaborators — explicitly to stem AI-slop floods. A normal
pipeline only discovers this at the LAST step (like our rich#4208 run);
this probe discovers it up front, without creating anything:

    Attempt REST `POST /repos/<upstream>/pulls` with the head pointing at a
    NONEXISTENT branch on the authenticated user's fork.

  - open repo   -> validation runs and fails on the missing head ref
                   (422, field=head invalid) — nothing is created.
  - gated repo  -> the permission gate runs BEFORE validation and answers
                   404 — nothing is created.
Both outcomes are equally cheap; no branch is pushed, no PR exists after.

Usage:
  pr_writable.py --probe sweep/candidates.jsonl   # enrich + write back
  pr_writable.py --repo Textualize/rich           # one-off verdict
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def gh_json(*argv: str, timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(["gh", "api", *argv], capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout or r.stderr or "").strip()


def ensure_fork(upstream: str, tries: int = 3) -> str:
    """Idempotent `gh repo fork` (clone=false); returns fork owner/repo."""
    login = subprocess.run(["gh", "api", "user", "-q", ".login"],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    fork = f"{login}/{upstream.split('/')[1]}"
    def fork_visible() -> bool:
        return subprocess.run(["gh", "api", f"repos/{fork}", "-q", ".full_name"],
                              capture_output=True, text=True, timeout=30).returncode == 0
    if fork_visible():
        return fork
    subprocess.run(["gh", "repo", "fork", upstream, "--clone=false"],
                   capture_output=True, text=True, timeout=120)
    for i in range(tries):          # fork propagation
        if fork_visible():
            return fork
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"fork for {upstream} did not appear as {fork}")


def default_branch(upstream: str) -> str:
    r = subprocess.run(["gh", "api", f"repos/{upstream}", "-q", ".default_branch"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or "main"


def probe_repo(upstream: str) -> dict:
    """top-level {owner}/{repo} -> pr_writable verdict, no side effects."""
    try:
        ensure_fork(upstream)
    except RuntimeError as e:
        return {"repo": upstream, "pr": "unknown", "reason": str(e)}
    base = default_branch(upstream)
    head = f"{upstream.split('/')[0]}-owner-no-such-branch-{int(time.time())}"
    fork_owner = subprocess.run(["gh", "api", "user", "-q", ".login"],
                                capture_output=True, text=True, timeout=30).stdout.strip()
    head = f"{fork_owner}:no-such-branch-probe-{int(time.time())}"
    rc, out = gh_json("-X", "POST", f"repos/{upstream}/pulls",
                      "-f", "title=__probe__", "-f", "body=__probe__",
                      "-f", f"base={base}", "-f", f"head={head}")
    txt = out.strip()
    if '"status":"404"' in txt:
        return {"repo": upstream, "pr": "locked",
                "reason": "PR creation gated to collaborators "
                          "(anti-AI-slop lockdown or equivalent)"}
    if '"status":"422"' in txt or '"code":"invalid"' in txt:
        # head ref doesn't exist -> validation reached -> PRs are open
        return {"repo": upstream, "pr": "open",
                "reason": "validation reached with a missing head ref"}
    return {"repo": upstream, "pr": "unknown", "reason": txt[-160:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", help="candidates.jsonl to enrich in place")
    ap.add_argument("--repo", help="single owner/repo verdict")
    args = ap.parse_args()
    if args.repo:
        print(json.dumps(probe_repo(args.repo), indent=2))
        return 0
    if not args.probe:
        ap.error("need --probe or --repo")
    path = Path(args.probe)
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    verdicts: dict[str, dict] = {}
    for r in rows:
        repo = r["repo"]
        if repo in verdicts:
            continue
        v = probe_repo(repo)
        verdicts[repo] = v
        print(f"{repo:38s} {v['pr']:8s} {v['reason'][:64]}", file=sys.stderr)
        time.sleep(1)
    for r in rows:
        v = verdicts[r["repo"]]
        r["pr_ok"] = v["pr"] == "open"
        r["pr_note"] = v["reason"]
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
    open_n = sum(1 for v in verdicts.values() if v["pr"] == "open")
    print(f"probed {len(verdicts)} repos: {open_n} PR-open, "
          f"{sum(1 for v in verdicts.values() if v['pr'] == 'locked')} locked, "
          f"{len(verdicts) - open_n} other", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())