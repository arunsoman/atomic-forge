#!/usr/bin/env python3
"""Single source of truth for the real-issue campaign's public numbers.

Reads every result ledger the campaign has ever written (round1 pilot
through round4, plus campaign50's astroid stream), dedupes issues that
were attempted more than once across rounds, checks live PR status via
`gh`, and prints one reconciled summary. RESULTS.md / benchmarks/README.md
/ the main README should be regenerated from this output, not hand-edited
— hand-editing multiple docs from multiple jsonl files is exactly how the
12-vs-18 PR count drift happened (RESULTS.md was written before
results_round4.jsonl existed and never got revisited).

Usage: python benchmarks/real_issues/reconcile.py [--json]
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SWEEP = HERE / "sweep"

# Round files in chronological order — later rounds' outcome for a given
# (repo, issue) supersede earlier ones when dedup'ing attempt counts, but
# a pr_raised in ANY round always wins (a later infra_fail retry of an
# already-PR'd issue doesn't erase the PR).
ROUND_FILES = [
    SWEEP / "results.jsonl",  # round1 pilot, pre-dates round-numbering
    SWEEP / "results_round2.jsonl",
    SWEEP / "results_round3.jsonl",
    SWEEP / "results_round4.jsonl",  # NOTE: untracked as of 2026-08-31
]

# Hand-run outside run_campaign.py's ledger (see campaign_log.md) — no
# jsonl row exists for these, so they're recorded here until/unless they
# get a proper ledger entry.
MANUAL_ASTROID_PRS = [
    ("pylint-dev/astroid", 3199, "https://github.com/pylint-dev/astroid/pull/3261", "closed"),
    ("pylint-dev/astroid", 3259, "https://github.com/pylint-dev/astroid/pull/3262", "closed"),
    ("pylint-dev/astroid", 3258, "https://github.com/pylint-dev/astroid/pull/3263", "closed"),
    ("pylint-dev/astroid", 3257, "https://github.com/pylint-dev/astroid/pull/3264", "closed"),
]

# The ledger rows for these two record the PR as originally opened from a
# personal fork; both were later closed and re-opened with identical
# commits from an org-owned (kannamma-labs) fork (see benchmarks/README.md).
# The ledger's pr_url is the closed original — redirect to the live one so
# status checks don't report a stale, superseded PR as the campaign result.
PR_URL_REDIRECTS = {
    "https://github.com/python-babel/babel/pull/1333": "https://github.com/python-babel/babel/pull/1334",
    "https://github.com/jd/tenacity/pull/704": "https://github.com/jd/tenacity/pull/705",
}


def load_rounds():
    attempts = {}  # (repo, number) -> dict(status, pr_url, sources=[])
    order = []
    for fn in ROUND_FILES:
        if not fn.exists():
            continue
        with open(fn) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                key = (d.get("repo"), d.get("number"))
                status = d.get("status") or d.get("outcome")
                pr_url = d.get("pr_url")
                if key not in attempts:
                    attempts[key] = {"status": status, "pr_url": pr_url, "rounds": 0}
                    order.append(key)
                rec = attempts[key]
                rec["rounds"] += 1
                # A pr_raised anywhere wins over a later retry's failure.
                if pr_url and not rec["pr_url"]:
                    rec["pr_url"] = pr_url
                    rec["status"] = status
                elif not rec["pr_url"]:
                    rec["status"] = status  # latest non-PR status supersedes
    return attempts, order


def check_pr_status(pr_url):
    try:
        out = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt,title"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return {"state": "UNKNOWN", "mergedAt": None}
        return json.loads(out.stdout)
    except Exception:
        return {"state": "UNKNOWN", "mergedAt": None}


def main():
    attempts, order = load_rounds()

    total_attempts = len(attempts)
    prs = [
        (k, PR_URL_REDIRECTS.get(v["pr_url"], v["pr_url"]))
        for k, v in attempts.items() if v["pr_url"]
    ]
    for repo_issue in MANUAL_ASTROID_PRS:
        repo, num, url, _ = repo_issue
        prs.append(((repo, num), url))

    live = []
    for (repo, num), url in prs:
        st = check_pr_status(url)
        live.append({"repo": repo, "number": num, "pr_url": url, **st})

    open_ = sum(1 for r in live if r["state"] == "OPEN")
    closed = sum(1 for r in live if r["state"] == "CLOSED")
    merged = sum(1 for r in live if r.get("mergedAt"))
    unknown = sum(1 for r in live if r["state"] == "UNKNOWN")

    summary = {
        "total_tracked_attempts": total_attempts,
        "total_prs_raised": len(prs),
        "open": open_,
        "closed_unmerged": closed - merged,
        "merged": merged,
        "unknown": unknown,
        "prs": live,
    }

    if "--json" in sys.argv:
        print(json.dumps(summary, indent=2))
        return

    print(f"Tracked attempts (deduped across rounds): {total_attempts}")
    print(f"Total PRs raised: {len(prs)}")
    print(f"  open: {open_}  closed-unmerged: {closed - merged}  merged: {merged}  unknown: {unknown}")
    print()
    for r in sorted(live, key=lambda r: r["repo"]):
        flag = "MERGED" if r.get("mergedAt") else r["state"]
        print(f"  {r['repo']:35s} #{r['number']:<6} {r['pr_url']:55s} {flag}")


if __name__ == "__main__":
    main()
