#!/usr/bin/env python3
"""Shared per-issue cleanup for the real-issue sweep drivers (sweep.py,
run_round2.py, run_round3.py, and whatever runs campaign50).

`atomic-forge fix` clones each issue's repo to `<tempdir>/forge_fix/<repo>-
<number>` (see fix.py's default `work_root`) and never removes it — every
run leaves behind a full clone + venv + CIE index, and forensic artifacts
under `.forge/` (learning.json, exit_audit.jsonl, trajectory.jsonl) that
are lost the moment the clone is deleted. This IS what happened mid-pilot
(see rca_pilot_runs_1_3.md, finding F5: "all /tmp/forge_fix workdirs
vanished mid-session") — the fix prescribed there is exactly this module:
copy the forensic artifacts into a durable, git-tracked location, THEN
delete the ephemeral clone. Nothing else in the clone (source checkout,
venv, CIE cache) has any use once the result is logged — successes are
already pushed to a fork branch on GitHub, so nothing here is otherwise
recoverable-only-here.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: Contribution-policy files worth checking, in likelihood order. Only the
#: first one found is read — a repo has one canonical contribution doc, not
#: several competing ones.
_POLICY_PATHS = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md",
                  "CONTRIBUTING.rst", "docs/contributing.rst")

#: Phrases confirmed live, 2026-08-31, in real projects' own contribution
#: docs that specifically ban what atomic-forge's `--raise-pr` does: open a
#: PR with no human having reviewed it first. NOT a blanket "mentions AI"
#: filter — pytest-dev/pytest and networkx/networkx both explicitly WELCOME
#: disclosed, human-reviewed AI-assisted contributions; only the unattended/
#: unreviewed/purely-agentic case is disqualifying, matched by these exact
#: phrases: Rapptz/discord.py ("blanket banned... instantly closed", hit
#: live — the PR was closed by the maintainer within seconds, citing this),
#: jazzband/pip-tools ("Autonomous Code Submissions: ... without human
#: review is not permitted"), pytest-dev/pytest ("Purely agentic
#: contributions are not accepted... we ban with prejudice" — a threat
#: against the submitting account, not just the PR), networkx/networkx
#: ("Review all code... before submitting them under your name" — a
#: pre-submission human-review requirement, softer phrasing but the same
#: disqualifying condition: no such review happens before `--raise-pr`).
_HARD_BAN_MARKERS = (
    "blanket banned",
    "instantly closed",
    "does not accept any ai contributions",
    "without human review is not permitted",
    "purely agentic contributions are not accepted",
    "ban with prejudice",
    "unattended automation",
    "before submitting them under your name",
)


def check_ai_policy(repo: str, timeout: int = 20) -> str | None:
    """Fetch `repo`'s own contribution policy (first of `_POLICY_PATHS`
    found) and check it for language that bans an unattended/autonomous-
    agent PR or requires human review before submission — a category
    atomic-forge's `--raise-pr` cannot satisfy, since it opens the PR with
    no human having reviewed the diff first. Returns a short reason string
    if the repo should be skipped entirely (never even attempted, not just
    the PR withheld — spending a real repair effort on a repo that will
    reject the result on principle wastes the same budget as any other
    dead end), or None if no such policy was found.

    Deliberately narrow: keyed to exact phrases confirmed against real
    repos (see `_HARD_BAN_MARKERS`), not a broad "AI" keyword match — a
    broad filter would incorrectly exclude projects like pytest that
    explicitly welcome disclosed, reviewed AI-assisted work; forge's own
    PR footer already discloses tool provenance honestly, which is exactly
    what those policies ask for. This can still miss a real policy phrased
    differently, or one living somewhere other than `_POLICY_PATHS` — it's
    a good-faith check, not a guarantee; a human curating `campaign50_
    targets.json`-style repo lists remains the real backstop."""
    for path in _POLICY_PATHS:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{path}", "--jq", ".content"],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        try:
            raw = base64.b64decode(r.stdout.strip()).decode("utf-8", errors="replace").lower()
        except Exception:
            continue
        # Prose in these docs line-wraps at ~80-100 chars — a marker phrase
        # can straddle a newline ("...without human review\nis not
        # permitted...") and silently fail a literal substring check.
        # Confirmed live: this exact bug produced false "clear" verdicts for
        # jazzband/pip-tools and networkx/networkx on first implementation.
        # Collapse all whitespace runs to one space before matching.
        content = re.sub(r"\s+", " ", raw)
        for marker in _HARD_BAN_MARKERS:
            if marker in content:
                return f"{path}: policy contains {marker!r} — conflicts with unreviewed autonomous PR"
        return None  # found and read the canonical policy file, it's clear — don't check the others
    return None

_COST_RE = re.compile(
    r"llm_calls=(\d+) prompt_tokens=(\d+) completion_tokens=(\d+)")


def parse_cost(stdout: str) -> dict:
    """Extract forge's own `llm_calls=N prompt_tokens=N completion_tokens=N`
    line (llm.py's Usage.__str__) so campaign result records carry a real
    per-run cost, per rca_pilot_runs_1_3.md's F8 finding — the data was
    already being printed by forge, just never captured by the driver.
    Returns {} if the line isn't present (e.g. the run aborted before any
    LLM call, as with a bootstrap_fail)."""
    m = _COST_RE.search(stdout)
    if not m:
        return {}
    return {"llm_calls": int(m[1]), "prompt_tokens": int(m[2]),
            "completion_tokens": int(m[3])}

#: The three per-run forensic artifacts named in rca_pilot_runs_1_3.md's F5
#: fix. All three live under `<project_dir>/.forge/` (see learning.py,
#: exit_audit.py, trajectory.py) and are the ONLY things in a clone worth
#: keeping once a run is done.
_FORGE_ARTIFACTS = ("learning.json", "exit_audit.jsonl", "trajectory.jsonl")


def harvest_and_clean(tmp_root: Path, logs_dir: Path, repo: str, number: int,
                       result: dict | None = None) -> str | None:
    """Copy `.forge/{learning.json,exit_audit.jsonl,trajectory.jsonl}` (each,
    if present) into `logs_dir/<repo-slug>-<number>/`, alongside `result`
    (the driver's own result record, if given, as `result.json` — this is
    the "+ PR body" half of F5's fix: everything about one run in one
    place), then rmtree the issue's ephemeral clone.

    Returns the reclaimed size (human-readable) or None if the clone was
    already gone.
    """
    slug = f"{repo.split('/')[-1]}-{number}"
    d = tmp_root / slug
    if not d.is_dir():
        return None

    run_log_dir = logs_dir / slug
    forge_dir = d / ".forge"
    copied = []
    if forge_dir.is_dir():
        run_log_dir.mkdir(parents=True, exist_ok=True)
        for name in _FORGE_ARTIFACTS:
            src = forge_dir / name
            if src.is_file():
                shutil.copy2(src, run_log_dir / name)
                copied.append(name)
    if result is not None:
        run_log_dir.mkdir(parents=True, exist_ok=True)
        rec = {"harvested_at": time.strftime("%F %T"), **result}
        (run_log_dir / "result.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2))
        copied.append("result.json")
    if copied:
        print(f"  harvested {', '.join(copied)} for {repo}#{number} -> {run_log_dir}",
              file=sys.stderr)

    du = subprocess.run(["du", "-sh", str(d)], capture_output=True, text=True)
    size = du.stdout.split()[0] if du.returncode == 0 and du.stdout.strip() else None
    shutil.rmtree(d, ignore_errors=True)
    print(f"  cleaned {d}" + (f" ({size})" if size else ""), file=sys.stderr)
    return size


def prune_docker() -> None:
    for cmd in (["docker", "container", "prune", "-f"],
                ["docker", "volume", "prune", "-f"]):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        print(f"  $ {' '.join(cmd)} -> {(r.stdout or r.stderr).strip()[:120]}",
              file=sys.stderr)
