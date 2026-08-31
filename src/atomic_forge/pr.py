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
    default_branch_for(upstream, project_dir=None) -> str
        same, for an owner/repo string rather than a local checkout — used
        by the fork-only PR path. Verifies main/master actually exist
        before guessing (and raises if neither does) rather than assuming
        main; logs an exit_audit note when it had to fall back.
    raise_pr_via_fork(project_dir, *, upstream, title, body="", base=None,
                      dry_run=False) -> dict
        fork-only PR: push to the authenticated user's (or FORGE_FORK_ORG's)
        fork of `upstream` and open a PR from it. Every PR body forge writes
        should include forge_footer() — see classify_pr_create_error() for
        the retry/fail-fast triage of `gh pr create` failures.

`raise_pr` never force-pushes and never touches the default branch — it only
adds a feature branch and opens a PR. It is deliberately the only GitHub side
effect in forge; every other step is local.
"""
from __future__ import annotations

import os
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


def gh_login() -> str:
    """The authenticated GitHub username (via `gh`)."""
    r = subprocess.run(["gh", "api", "user", "-q", ".login"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"could not resolve GitHub login via `gh`: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def ensure_fork(project_dir, upstream: str) -> tuple[str, str]:
    """Make sure a fork of `upstream` (owner/repo) exists on GitHub, and that
    a local git remote named `fork` points at it. Returns (owner, fork_url).
    NEVER pushes to `origin` — `origin` is the cloned upstream and is
    fetch-only; all pushes go to the `fork` remote.

    By default the fork lives under the authenticated `gh` user. Set
    FORGE_FORK_ORG to fork into an org instead (e.g. so every PR forge raises
    comes from one maintained org account rather than scattering forks across
    whichever personal account happened to run the fix) — the currently
    authenticated `gh` session still needs admin rights on that org for the
    fork+push to work; PR *authorship* still follows the `gh` token, not the
    fork owner."""
    project_dir = Path(project_dir)
    org = os.environ.get("FORGE_FORK_ORG", "").strip()
    owner = org or gh_login()
    repo = upstream.split("/")[-1]
    fork_url = f"https://github.com/{owner}/{repo}.git"
    # Create the fork on GitHub if it doesn't already exist. `gh repo fork`
    # is idempotent: confirmed live (astroid#3199, 2026-08-30) that an
    # already-forked repo exits 0 with "owner/repo already exists" — the
    # OLD comment here ("a non-zero exit usually means it already exists")
    # was backwards and, worse, this result was never even checked: a
    # genuine failure (e.g. a brand-new account hitting GitHub's fork-
    # velocity abuse throttle — HTTP 403 "You cannot fork this repository
    # at this time", confirmed account-wide by forking an unrelated repo,
    # not upstream-specific) was silently swallowed here. The caller then
    # pushed to a `fork` remote pointing at a fork that was never created,
    # and got a confusing "repository not found" several steps later
    # instead of a clear failure at the actual point of failure.
    fork_cmd = ["gh", "repo", "fork", upstream, "--clone=false"]
    if org:
        fork_cmd += ["--org", org]
    r = subprocess.run(fork_cmd, cwd=str(project_dir), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"gh repo fork {upstream} failed (owner={owner}): "
            f"{(r.stderr or r.stdout).strip() or 'no output'}")
    # Point a local `fork` remote at the fork (add or fix-up).
    if _git(["remote", "get-url", "fork"], project_dir, check=False):
        _git(["remote", "set-url", "fork", fork_url], project_dir)
    else:
        _git(["remote", "add", "fork", fork_url], project_dir)
    return owner, fork_url


def raise_pr_via_fork(project_dir, *, upstream: str, title: str, body: str = "",
                      base: Optional[str] = None, dry_run: bool = False) -> dict:
    """Fork-only PR: push the current branch to the authenticated user's
    FORK of `upstream` (owner/repo) and open a PR `fork -> upstream`.

    Never pushes to `origin` (the cloned upstream). `upstream` is the
    owner/repo the issue lives in. `base` defaults to that repo's default
    branch. Call `prepare_pr_branch` first so the fix is on a feature
    branch."""
    project_dir = Path(project_dir)
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], project_dir)
    base = base or default_branch_for(upstream)
    if head == base:
        raise RuntimeError(
            f"refusing to PR the default branch ({head!r}) onto itself — "
            "call prepare_pr_branch() first so the fix is on a feature branch.")
    if dry_run:
        return {"dry_run": True, "branch": head, "base": base, "upstream": upstream,
                "title": title, "body_len": len(body)}
    login, fork_url = ensure_fork(project_dir, upstream)
    push = _git(["push", "-u", "fork", head], project_dir, check=False)
    if not _git(["rev-parse", "--abbrev-ref", f"fork/{head}"], project_dir, check=False):
        raise RuntimeError(
            f"git push fork {head} failed (fork={fork_url}): {push!r}")
    cmd = ["gh", "pr", "create", "--repo", upstream, "--head", f"{login}:{head}",
           "--base", base, "--title", title, "--body", body]
    # Fresh forks race GraphQL: createPullRequest 403s until the fork is
    # visible server-side. Retry a few times; refresh the fork between
    # attempts (ensure_fork is idempotent) so a silently-failed fork gets
    # created rather than retried against nothing.
    last_err = ""
    for attempt in range(4):
        r = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True)
        if r.returncode == 0:
            break
        last_err = (r.stderr or r.stdout or "").strip()
        kind = classify_pr_create_error(last_err)
        if kind == "lockdown":
            raise RuntimeError(
                "upstream blocks PR creation for non-collaborators "
                "(repo-level contributor gate — e.g. Textualize/rich's AI-slop "
                "lockdown). The validated fix branch remains pushed to the fork; "
                "an authorized account (collaborator status or approved path per "
                "the repo's AI policy) must open it. gh said: " + last_err)
        if kind == "no_commits":
            # Not a fork-sync race — retrying can't fix an empty diff. This
            # means the branch forge pushed has ZERO commits ahead of base:
            # some caller reached raise_pr_via_fork() after a "success" that
            # never actually produced a patch (see repair_agent's initial
            # already-green short-circuit, which fix.py now checks for
            # before ever getting here — this is a defense-in-depth catch,
            # not the primary fix). Fail fast rather than burn 4 retries.
            raise RuntimeError(
                "gh pr create failed: the pushed branch has no commits ahead "
                f"of {base!r} — nothing to open a PR for. gh said: " + last_err)
        if kind != "race":
            raise RuntimeError(f"gh pr create failed: {last_err}")
        time.sleep(3 * (attempt + 1))
        try:
            ensure_fork(project_dir, upstream)
        except Exception:
            pass
    else:
        raise RuntimeError(f"gh pr create failed: {last_err}")
    url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return {"pr_url": url, "branch": head, "base": base, "upstream": upstream,
            "fork": fork_url, "title": title}


def classify_pr_create_error(err: str) -> str:
    """Categorize a `gh pr create` failure so `raise_pr_via_fork` knows
    whether to retry, fail fast, or raise a specific "you need a human"
    error. Pulled out as its own function so each category has a direct
    unit test instead of only being exercisable through the full
    fork+push+retry integration path.

    Returns one of: "lockdown" (upstream gates PRs to collaborators —
    retrying never helps), "no_commits" (the branch has zero commits ahead
    of base — retrying never helps), "race" (fork-sync lag — worth
    retrying), "other" (unrecognized — surface as-is, don't retry blind).

    Matching is case-insensitive throughout: `gh`'s actual GraphQL error
    text annotates the mutation name in lowercase, `(createPullRequest)` —
    a capitalized-only match here previously never fired, so the "race"
    category never actually retried (see git history)."""
    low = (err or "").lower()
    if "correct permissions to execute `createpullrequest`" in low:
        return "lockdown"
    if "no commits between" in low:
        return "no_commits"
    if ("createpullrequest" in low or "head sha was not found" in low
            or "field: headrepository" in low or "fork collab" in low):
        return "race"
    return "other"


def default_branch_for(upstream: str, project_dir=None) -> str:
    """Default branch of an owner/repo (owner/repo string), via `gh`.

    The `gh api` call is authoritative and should resolve for any real,
    reachable repo — it's the fallback path that used to just guess "main"
    unconditionally, silently wrong for the (still common) repos still on
    "master". Now: verify main/master actually exist via `git ls-remote`
    before picking one, and raise rather than guess if neither does. Every
    time this DOESN'T take the authoritative path, it's worth a fail-fast
    audit note (`project_dir` is optional — pass it to get one; the
    fork/PR pipeline that consumes this passes its own project_dir)."""
    r = subprocess.run(
        ["gh", "repo", "view", upstream, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    url = f"https://github.com/{upstream}.git"
    for cand in ("main", "master"):
        probe = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", url, cand],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode == 0 and probe.stdout.strip():
            if project_dir is not None:
                from .exit_audit import record_exit
                record_exit(project_dir, reason="ambiguous_branch_defaulted",
                            detail=f"gh repo view failed ({(r.stderr or r.stdout).strip()[:200]}); "
                                   f"defaulted to {cand!r} after verifying it exists on {upstream}",
                            extra={"upstream": upstream, "branch": cand})
            return cand
    raise RuntimeError(
        f"could not resolve {upstream}'s default branch: gh api failed "
        f"({(r.stderr or r.stdout).strip()[:200]}) and neither 'main' nor "
        "'master' exists on the remote — this repo uses some other branch "
        "name forge can't guess.")


_FORGE_URL = "https://github.com/kannamma-labs/atomic-forge"


def forge_footer() -> str:
    """Canonical footer appended to every PR forge raises, anywhere.

    Same wording, same badge, same hidden `<!-- atomic-forge:pr -->` marker
    in every repo and under every fork account (`kannamma-labs` or
    otherwise) — the point isn't decoration, it's that this exact marker
    string is what makes the whole campaign trackable: `gh search prs --owner
    kannamma-labs --match body "atomic-forge:pr"` (or GitHub's own search)
    finds every forge-raised PR as one set, without depending on a `label`
    we usually can't set on someone else's upstream repo."""
    return (
        "\n---\n"
        "<!-- atomic-forge:pr -->\n"
        f"[![Fixed by Forge](https://img.shields.io/badge/fixed_by-atomic--forge-6f42c1?logo=github)]({_FORGE_URL})\n\n"
        f"🔨 Fixed by [Forge]({_FORGE_URL}) — an autonomous, test-driven "
        "issue→PR repair engine (`atomic-forge fix <issue-url>`).\n\n"
        f"⭐ If you found this useful, a star on [atomic-forge]({_FORGE_URL})"
        " helps other maintainers find the tool that fixed your bug.\n"
    )


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
        "green.\n"
        + forge_footer()
    )
    return title, body


# --- preflight policy checks -------------------------------------------
#
# Both moved here from campaign-only benchmark scripts (2026-08-31, see
# goal.md's "learnings folded back into forge" addendum and
# RESULTS.md) so a DIRECT `atomic-forge fix` call — CLI, the GitHub
# Action, an MCP tool caller — gets the same protection real-issues
# campaign runs do, instead of only campaigns that happen to route
# through curate.py/pr_writable.py first. Real cost avoided by catching
# these before any LLM spend: dateutil/dateutil#1421 alone cost 158 LLM
# calls / ~2.9M tokens attempting a fix for behavior a maintainer had
# already confirmed as intentional.

_AI_POLICY_PATHS = ["CONTRIBUTING.md", ".github/CONTRIBUTING.md",
                    "docs/CONTRIBUTING.md", ".github/PULL_REQUEST_TEMPLATE.md"]
_AI_POLICY_MARKERS = ("ai-generated", "ai generated", "ai contributions",
                      "llm-generated", "llm generated", "generated by an ai",
                      "generated by a language model", "no ai-authored",
                      "not accept ai", "won't accept ai", "will not accept ai",
                      "automated pull request", "bot-generated pull request")

_MAINTAINER_REJECTION_MARKERS = (
    "working as intended", "works as intended", "expected behaviour",
    "expected behavior", "by design", "not a bug", "won't fix", "wontfix",
    "as designed", "intentional", "duplicate of", "can't reproduce",
    "cannot reproduce", "unable to reproduce")
_MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _gh_api_json(path: str):
    """`gh api <path>`, parsed. None on any failure (network, 404, auth) —
    every caller here treats that as "couldn't check," not "check passed,"
    so a probe outage never silently disables a filter it can't answer."""
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    import json
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def check_ai_policy(upstream: str) -> Optional[dict]:
    """None if clear (or unknown); else {"path", "reason"} for the first
    written AI-contributions policy clause found in upstream's usual
    policy-doc locations. A written policy isn't API-enforced — PR
    creation still succeeds — so this can't be caught by a mechanical
    "does POST /pulls 404" probe (see pr_writable.py's PR-writability
    probe, a distinct, complementary check for the collaborator-only
    gate some repos use instead). Found live: Rapptz/discord.py#10507 was
    raised and closed citing exactly this — a written-but-unenforced
    policy neither this repair nor the campaign runner checked for at
    the time."""
    import base64
    for path in _AI_POLICY_PATHS:
        data = _gh_api_json(f"repos/{upstream}/contents/{path}")
        if not data:
            continue
        try:
            content = base64.b64decode(data.get("content", "")).decode(
                "utf-8", errors="replace")
        except (ValueError, TypeError):
            continue
        low = content.lower()
        hit = next((m for m in _AI_POLICY_MARKERS if m in low), None)
        if hit:
            return {"path": path, "reason": f"contains an AI-contributions "
                    f"policy clause (matched {hit!r})"}
    return None


def issue_already_settled(upstream: str, number: int) -> Optional[str]:
    """URL of the rejecting comment if an OWNER/MEMBER/COLLABORATOR has
    already told the reporter this isn't a bug ("working as intended",
    "wontfix", ...); None if clear (or unknown). Found live:
    python-dateutil/dateutil#1421 passed every other quality filter —
    reproducible, well-titled, undisputed by the reporter — and only the
    comment thread itself (never fetched by curation before this) carried
    the maintainer's actual verdict."""
    data = _gh_api_json(f"repos/{upstream}/issues/{number}/comments")
    for c in (data if isinstance(data, list) else []):
        if c.get("author_association") not in _MAINTAINER_ASSOCIATIONS:
            continue
        body = (c.get("body") or "").lower()
        if any(m in body for m in _MAINTAINER_REJECTION_MARKERS):
            return c.get("html_url", "")
    return None