#!/usr/bin/env python3
"""Post-hoc legitimacy audit for round-3 campaign PRs.

Reads every distinct pr_url logged in sweep/results_round3.jsonl and
verifies, via `gh pr view` against the live GitHub API, that each one is:
  - a real PR that exists
  - state == OPEN
  - has a non-empty diff (additions+deletions > 0, or changedFiles > 0)
  - carries the forge_footer() provenance marker in its body
  - (informational) authored/forked from the expected account

This never gates PR creation — atomic_forge/fix.py already does that
internally (bootstrap gate, oracle check, ground-truth recheck, etc.).
This is an independent, read-only check that what the driver *logged* as
raised is actually real, out there, and honestly labeled. Rerun this at
each checkpoint during a long campaign run, not just at the end, so a
systemic problem (wrong fork owner, missing footer) surfaces after PR #1-2
rather than after all of them.

    python3 benchmarks/real_issues/audit_round3_prs.py [--expect-owner arunsoman]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "sweep" / "results_round3.jsonl"
OUT = HERE / "sweep" / "pr_audit_round3.json"

FOOTER = "<!-- atomic-forge:pr -->"


def load_pr_urls(path: Path) -> list[str]:
    urls: list[str] = []
    seen = set()
    if not path.exists():
        return urls
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = rec.get("pr_url")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def audit_one(url: str, expect_owner: str | None) -> dict:
    fields = "state,body,additions,deletions,changedFiles,author,headRepositoryOwner,url"
    r = subprocess.run(["gh", "pr", "view", url, "--json", fields],
                        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"url": url, "ok": False, "checks": {"exists": False},
                "reason": (r.stderr or r.stdout).strip()[:300]}

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {"url": url, "ok": False, "checks": {"exists": False},
                "reason": f"unparseable gh output: {e}"}

    diff_size = (data.get("additions") or 0) + (data.get("deletions") or 0)
    owner_login = ((data.get("headRepositoryOwner") or {}).get("login") or "")

    checks = {
        "exists": True,
        "open": data.get("state") == "OPEN",
        "non_empty_diff": diff_size > 0 or (data.get("changedFiles") or 0) > 0,
        "has_footer": FOOTER in (data.get("body") or ""),
    }
    if expect_owner:
        checks["expected_owner"] = owner_login == expect_owner

    ok = all(checks.values())
    return {
        "url": url, "ok": ok, "checks": checks,
        "state": data.get("state"),
        "author": (data.get("author") or {}).get("login"),
        "fork_owner": owner_login,
        "changed_files": data.get("changedFiles"),
        "additions": data.get("additions"), "deletions": data.get("deletions"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-owner", default=None,
                     help="fail the check if the PR's fork owner isn't this login")
    ap.add_argument("--sleep", type=float, default=0.5,
                     help="seconds between gh calls (stay well within rate limit)")
    args = ap.parse_args()

    urls = load_pr_urls(RESULTS)
    if not urls:
        print("no pr_url values logged yet in results_round3.jsonl", file=sys.stderr)
        OUT.write_text(json.dumps({"at": time.strftime("%F %T"), "results": []}, indent=2))
        return 0

    results = []
    for i, url in enumerate(urls):
        res = audit_one(url, args.expect_owner)
        results.append(res)
        status = "PASS" if res["ok"] else "FAIL"
        detail = res.get("reason") or ", ".join(k for k, v in res.get("checks", {}).items() if not v)
        print(f"[{status}] {url}" + (f"  ({detail})" if not res["ok"] else ""), file=sys.stderr)
        if i < len(urls) - 1:
            time.sleep(args.sleep)

    passed = sum(1 for r in results if r["ok"])
    print(f"\n{passed}/{len(results)} PR(s) passed audit", file=sys.stderr)

    OUT.write_text(json.dumps({"at": time.strftime("%F %T"), "results": results}, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
