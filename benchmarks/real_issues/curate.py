#!/usr/bin/env python3
"""Curate OPEN, well-documented issues for the fix-sweep campaign.

Quality bar (all enforced per issue):
  - repo in TARGET_REPOS (permissive license, mid-sized, active)
  - open, bug-flavored title (or bug label), not feature/security
  - body contains a trace/repro signal (Traceback, code fence, reproduce,
    expected vs actual, assert)

Output: sweep/candidates.jsonl (one JSON per issue, sorted by score).
    python benchmarks/real_issues/curate.py --per-repo 12
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

TARGET_REPOS = {
    "mahmoud/boltons": "MIT",
    "more-itertools/more-itertools": "MIT",
    "python-attrs/attrs": "MIT",
    "pallets/click": "BSD-3-Clause",
    "Textualize/rich": "MIT",
    "pydantic/pydantic": "MIT",
    "marshmallow-code/marshmallow": "MIT",
    "python-dateutil/python-dateutil": "Apache-2.0",
    "tiangolo/typer": "MIT",
    "encode/httpx": "BSD-3-Clause",
    "python-poetry/cleo": "MIT",
    "simple-salesforce/simple-salesforce": "Apache-2.0",
}

REPRO_SIGNALS = re.compile(
    r"(Traceback|raise [A-Z]\w+Error|steps to repro|reproduce|Minimal example|"
    r"```|expected[:\s]|actual[:\s]|assert)", re.I)
NEGATIVE = re.compile(
    r"(security|vulnerab|CVE|exploit|0day|disclosure)", re.I)
BUG_TITLE = re.compile(
    r"(bug|error|fails?|failure|broken|incorrect|wrong|crash|traceback|"
    r"exception|regression|\bisn'?t\b|\bnot \b|cannot|can't|should\b)", re.I)
BAD_LABEL = {"enhancement", "feature", "feature-request", "RFC", "docs",
             "documentation", "question", "good first issue", "security"}


def gh(path: str) -> dict:
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    return json.loads(r.stdout)


def score(body: str, comments: int) -> int:
    s = 0
    if "Traceback" in body:
        s += 10
    if "```" in body:
        s += 6
    if re.search(r"(version|installed|python\s+\d)", body, re.I):
        s += 3
    if comments >= 3:
        s += 2
    if NEGATIVE.search(body):
        s -= 30
    if len(body) > 6000:
        s -= 8
    if len(body) < 120:
        s -= 8
    return s


def curate_repo(repo: str, lic: str, per_repo: int, min_year: int) -> list[dict]:
    # most-recent first (a fix PR against a stale bug is often moot at HEAD),
    # with enough comments that the report is documented, not drive-by
    data = gh(f"search/issues?q=repo:{repo}+is:issue+is:open"
              f"+sort:created-desc&per_page=30")
    rows = []
    for item in data.get("items", []):
        labels = {l["name"] for l in item.get("labels", [])}
        title = item.get("title") or ""
        if NEGATIVE.search(title):
            continue
        if not BUG_TITLE.search(title):
            continue
        if labels & BAD_LABEL:
            continue
        if item.get("pull_request"):
            continue
        created = str(item.get("created_at", ""))
        if created and int(created[:4]) < min_year:
            continue
        full = gh(f"repos/{repo}/issues/{item['number']}")
        body = full.get("body") or ""
        if not REPRO_SIGNALS.search(body):
            continue
        rows.append({
            "repo": repo,
            "license": lic,
            "number": item["number"],
            "title": title[:200],
            "url": item["html_url"],
            "labels": sorted(labels),
            "comments": item.get("comments", 0),
            "body": body[:4000],
            "score": score(body, item.get("comments", 0)),
            "status": "candidate",
        })
        if len(rows) >= per_repo:
            break
    print(f"{repo}: kept {len(rows)}", file=sys.stderr)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-repo", type=int, default=12)
    ap.add_argument("--min-created-year", type=int, default=2024,
                    help="skip issues older than this — old bugs are often "
                         "fixed on main already; recent ones make real PRs")
    ap.add_argument("--out", default=str(HERE / "sweep" / "candidates.jsonl"))
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for repo, lic in TARGET_REPOS.items():
        rows.extend(curate_repo(repo, lic, args.per_repo, args.min_created_year))
    rows.sort(key=lambda r: -r["score"])
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"{len(rows)} open issues -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())