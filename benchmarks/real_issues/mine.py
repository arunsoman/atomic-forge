#!/usr/bin/env python3
"""Mine real closed issues + their merged bug-fix PRs into benchmark cases.

Methodology (SWE-bench-shaped, fully attributable):
  a merged PR links a closed issue, touches at least one test file, and is
  a tractable size. The PR's *test patch* becomes the oracle regression
  test (fails on the pre-fix tree, passes on the post-fix tree); the rest
  of the diff is the reference fix. Every case ships:
    repo / issue URL / PR URL / base_sha / fix_sha / test patches /
    fixed-file list / size stats / license note

Nothing here is synthetic: each case is a real closed issue's real merged
fix, reshaped into an harness case. Curation-time validation (clone ->
checkout base -> run the PR's new tests -> apply reference fix -> rerun)
is `validate.py`'s job; cases start `status: unverified`.

    python benchmarks/real_issues/mine.py                  # all repos
    python benchmarks/real_issues/mine.py --limit-per-repo 10 --out cases/
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"

# (repo, license) — mid-sized, popular, MIT/BSD/Apache, active maintenance.
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

# words that mark non-bug PRs
_TITLE_BLACKLIST = ("chore", "docs", "bump", "release", "upgrade", "update dep",
                    "ci ", "typos", "typo", "readme", "cSpell", "version")
MAX_FILES = 8
MAX_PATCH_LINES = 400
TEST_FILE_RE = re.compile(r"(?:^|/)(?:tests?/|.*_test\.py$|test_[^/]*\.py$)")


def gh(*args: str) -> dict:
    """gh api helper (json)."""
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:3])}: {r.stderr.strip()[:200]}")
    return __import__("json").loads(r.stdout)


def is_bugfix_candidate(pr: dict, files: list[dict], body: str) -> str | None:
    """Return the referenced closed-issue number if this PR qualifies."""
    title = (pr.get("title") or "").lower()
    if any(w in title for w in _TITLE_BLACKLIST):
        return None
    if any(fn(t) < 1 for t in [lambda f: f.get("filename")] for f in files):
        return None
    if not any(TEST_FILE_RE.search(f["filename"]) for f in files):
        return None
    if not f["is_python"] if False else False:
        return None
    changed = [f for f in files if not TEST_FILE_RE.search(f["filename"])]
    if not changed:                      # tests-only, nothing to repair
        return None
    if sum(f.get("changes", 0) for f in files) > MAX_PATCH_LINES:
        return None
    if len(files) > MAX_FILES:
        return None
    m = re.search(r"(?:fix(?:es|ed)?|closes?e?s?|resolves?)\s+#(\d+)",
                  (body or ""), re.I)
    return m.group(1) if m else None


def python_files(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """(source patches, test patches) from a PR file list."""
    src, tst = [], []
    for f in files:
        if not f.get("patch"):
            continue
        if TEST_FILE_RE.search(f["filename"]):
            src.append(f) if f["filename"].endswith(".py") else src.append(f)
    return src, src  # refined below


def mine_repo(repo: str, license: str, limit: int, seen: set[str]) -> list[dict]:
    """Mine up to `limit` case dicts from one repo."""
    out: list[dict] = []
    q = f"repo:{repo} is:pr is:merged linked:issue sort:updated-desc"
    items = gh(f"search/issues?q={q}&per_page=40").get("items", [])
    for item in items:
        if len(out) >= limit:
            break
        num = item.get("number")
        key = f"{repo}#{num}"
        if key in seen:
            continue
        try:
            pr = gh(f"repos/{repo}/pulls/{num}")
            files = gh(f"repos/{repo}/pulls/{num}/files?per_page=100")
        except RuntimeError as e:
            print(f"  ! {repo}#{num}: {e}", file=sys.stderr)
            continue
        issue_no = is_bugfix_candidate(pr, files, pr.get("body") or "")
        if not issue_no:
            continue
        try:
            issue = gh(f"repos/{repo}/issues/{issue_no}")
        except RuntimeError:
            continue
        if issue.get("state") != "closed" or bool(issue.get("pull_request")):
            continue
        test_files = [f for f in files if TEST_FILE_RE.search(f["filename"])]
        fix_files = [f["filename"] for f in files
                     if not TEST_FILE_RE.search(f["filename"])]
        slug = re.sub(r"[^a-z0-9]+", "-", (pr.get("title") or item.get("title", "pr")).lower())[:40].strip("-")
        cid = f"{repo.split('/')[1].lower()}/{num}_{slug or 'pr'}"
        if cid in seen:
            continue
        seen.add(key); seen.add(cid)
        out.append({
            "id": cid,
            "repo": repo,
            "license": license,
            "issue_url": f"https://github.com/{repo}/issues/{issue_no}",
            "issue_number": int(issue_no),
            "issue_title": issue.get("title", "")[:200],
            "issue_body_excerpt": (issue.get("body") or "")[:1200],
            "pr_url": f"https://github.com/{repo}/pull/{num}",
            "base_sha": pr["base"]["sha"],
            "fix_sha": pr.get("merge_commit_sha"),
            "fix_files": fix_files,
            "test_patch": {f["filename"]: f["patch"] for f in files
                           if TEST_FILE_RE.search(f["filename"])},
            "stats": {"files": len(files),
                      "lines": sum(f.get("changes", 0) for f in files)},
            "pr_title": pr.get("title", "")[:200],
            "status": "unverified",
            "validated": {"fails_before": None, "passes_after": None},
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-per-repo", type=int, default=12)
    ap.add_argument("--repos", nargs="*", help="subset of TARGET_REPOS")
    ap.add_argument("--out", default=str(CASES))
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    repos = args.repos or list(TARGET_REPOS)
    all_cases: list[dict] = []
    for repo in repos:
        lic = TARGET_REPOS.get(repo, "UNKNOWN")
        print(f"== {repo} ({lic})", file=sys.stderr)
        try:
            cases = mine_repo(repo, lic, args.limit_per_repo, seen)
        except RuntimeError as e:
            print(f"  ! repo failed: {e}", file=sys.stderr)
            continue
        (outdir / f"{repo.replace('/', '__')}.json").write_text(
            "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) or "")
        print(f"  {len(cases)} cases", file=sys.stderr)
        all_cases.extend(cases)

    index = {"count": len(all_cases),
             "by_repo": {r: sum(1 for c in all_cases if c["repo"] == r)
                         for r in repos},
             "cases": [{"id": c["id"], "issue_url": c["issue_url"],
                        "pr_url": c["pr_url"], "stats": c["stats"],
                        "status": c["status"]} for c in all_cases]}
    (outdir / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False))
    print(f"total: {len(all_cases)} cases -> {outdir}/index.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())