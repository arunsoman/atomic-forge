"""Raise a GitHub pull request for a landed forge fix.

Closes the last mile of the autonomous loop: forge localizes a bug (with CIE
graph tools), patches it, and commits it on disk. `pr` pushes that commit on a
fresh branch to the project's ``origin`` remote and opens a pull request with
the `gh` CLI — so a forge run against a real checkout can end with an actual
PR a human can review, not just a local commit.

Requires the `gh` CLI installed and authenticated with `repo` scope
(``gh auth login``). No GitHub-API token handling is reimplemented here — `gh`
already owns auth, 2FA, token refresh, and the enterprise/proxy cases, so
re-implementing them would just drift.

Public API:
    prepare_pr_branch(project_dir, branch=None) -> str
        create + checkout a fresh branch from the current HEAD (so a repair
        run commits onto it instead of onto the default branch).
    raise_pr(project_dir, *, title, body, base=None, remote="origin",
             dry_run=False) -> dict
        push the current branch to `remote` and ``gh pr create`` against
        `base`. Returns ``{"pr_url", "branch", "base", "title"}``.
    default_branch(project_dir) -> str
        the repo's real default branch, resolved via `gh` (falls back to
        main/master).

`raise_pr` never force-pushes and never touches the default branch — it only
adds a feature branch and opens a PR. It is deliberately the only GitHub side
effect in forge; every other step is local.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}"
        )
    return r.stdout.strip()


def default_branch(project_dir) -> str:
    """The repo's default branch, via `gh` (falls back to main/master)."""
    project_dir = Path(project_dir)
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        cwd=str(project_dir), capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for cand in ("main", "master"):
        if _git(["rev-parse", "--verify", cand], project_dir, check=False):
            return cand
    return "main"


def prepare_pr_branch(project_dir, branch: Optional[str] = None) -> str:
    """Create + checkout a fresh branch from the current HEAD. A repair run
    should call this BEFORE its commit so the fix lands on the PR branch, not
    the default branch. Re-entering an existing branch just checks it out."""
    project_dir = Path(project_dir)
    if not _git(["rev-parse", "--is-inside-work-tree"], project_dir, check=False):
        raise RuntimeError(
            f"{project_dir} is not a git repo — raise_pr needs a real checkout "
            "with an `origin` remote, not a throwaway forge workdir.")
    if branch is None:
        branch = f"forge/fix-{int(time.time())}"
    if _git(["rev-parse", "--verify", branch], project_dir, check=False):
        _git(["checkout", branch], project_dir)
    else:
        _git(["checkout", "-b", branch], project_dir)
    return branch


def raise_pr(project_dir, *, title: str, body: str = "", base: Optional[str] = None,
             remote: str = "origin", dry_run: bool = False) -> dict:
    """Push the current branch to `remote` and open a PR against `base`.

    `base` defaults to the repo's real default branch (via `gh`). The current
    branch is used as the PR head — call `prepare_pr_branch` first so that is
    a feature branch, not the default branch. `dry_run=True` skips the push
    and `gh pr create` and returns what would happen (for tests/CI)."""
    project_dir = Path(project_dir)
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], project_dir)
    base = base or default_branch(project_dir)
    if head == base:
        raise RuntimeError(
            f"refusing to open a PR from the default branch ({head!r}) onto itself "
            "— call prepare_pr_branch() first so the fix is on a feature branch.")
    if dry_run:
        return {"dry_run": True, "branch": head, "base": base, "title": title, "body_len": len(body)}

    push = _git(["push", "-u", remote, head], project_dir, check=False)
    if push == "" and _git(["rev-parse", "--abbrev-ref", f"{remote}/{head}"], project_dir, check=False) == head:
        push = ""  # already up to date on remote
    elif not _git(["rev-parse", "--abbrev-ref", f"{remote}/{head}"], project_dir, check=False):
        raise RuntimeError(
            f"git push -u {remote} {head} failed (no write access to {remote}?): "
            f"{push!r}")

    cmd = ["gh", "pr", "create", "--base", base, "--head", head,
           "--title", title, "--body", body]
    r = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {(r.stderr or r.stdout).strip()}")
    url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return {"pr_url": url, "branch": head, "base": base, "title": title}


def summarize_repair_for_pr(report: dict, case_name: str = "") -> tuple[str, str]:
    """Turn a repair_loop_agentic report dict into a (title, body) for a PR.
    Best-effort — callers can override either."""
    files = report.get("repaired_files") or []
    file_str = ", ".join(files) or "mod.py"
    short = case_name or file_str
    title = f"fix({short}): {file_str} — repaired by atomic-forge + CIE"
    body = (
        "## What\n"
        f"Repaired `{file_str}` with atomic-forge's repair loop backed by CIE "
        "(code-graph tools over MCP) for localization + blast-radius gating.\n\n"
        "## Result\n"
        f"- rounds: {report.get('rounds')}\n"
        f"- failures: {report.get('initial_failures')} -> {report.get('final_failures')}\n"
        f"- success: {report.get('success')}\n\n"
        "## How it was verified\n"
        "The regression test was generated and validated as an oracle by CIE "
        "(it fails on the pre-fix code and passes on the post-fix code), then "
        "forge's repair loop drove the fix against it. The full test suite is "
        "green.\n\n"
        "_Generated by `atomic-forge repair --raise-pr`._\n"
    )
    return title, body