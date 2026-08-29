"""
`atomic-forge fix <github_issue_url>` — one-shot issue → PR.

Pipeline (CIE required, not optional):
  1. preflight: LLM env present + CIE importable, else bail with a clear message.
  2. parse the issue URL; fetch the issue title + body (the bug description).
  3. get a runnable checkout: shallow-clone the repo (or use --project-dir),
     gitignore tool artifacts (.cie/.venv/.forge), stand up a venv + install.
  4. populate CIE: `cie index` the checkout, serve CIE as an MCP server.
  5. CIE generates a regression test from the issue (grounded in the real
     signatures via graph tools); validate it reproduces the bug (fails on
     the buggy code on an assertion). Abort if it can't reproduce — no PR.
  6. forge's repair loop (CIE-backed) fixes the bug against that test,
     re-running it each round until green or --max-rounds.
  7. on green: fork the repo, push the fix branch to the FORK only (never to
     origin), open a PR `fork → upstream`. --dry-run skips the push/PR.

CIE is mandatory here. The repair loop itself is unchanged; this orchestrator
just wires CIE-MCP + test-gen + the fork-only PR around it.

`run_fix_from_comment` is the same pipeline (via the shared
`_run_fix_pipeline`) for a review-comment-driven fix (R8): the bug
description comes from a comment anchored to a specific file/line instead
of a GitHub issue — see that function's docstring for scope notes.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .agent import render_tool_manifest
from .cie_backend import MCPBridge, MCPToolBackend, cie_index, require_cie
from .issue import (clone_repo, fetch_issue, issue_to_bug_description,
                    make_test_cmd, parse_issue_url, setup_python_env, upstream_slug)
from .llm import OpenAICompatLLM
from .pr import default_branch_for, forge_footer, prepare_pr_branch, raise_pr_via_fork
from .repair_agent import repair_loop_agentic
from .testgen import generate_regression_test, oracle_fails_on_buggy
from .trajectory import Trajectory

def _ground_truth_green(project_dir: Path, test_cmd: str, timeout: int = 300) -> bool:
    """Re-run the generated test ourselves and trust that, not the repair
    loop's self-report."""
    chk = subprocess.run(test_cmd, shell=True, cwd=str(project_dir),
                         capture_output=True, text=True, timeout=timeout)
    return chk.returncode == 0


_IGNORE_ARTIFACTS = [".cie/", ".venv/", ".forge/"]


def _gitignore_artifacts(project_dir: Path) -> None:
    """Append .cie/ .venv/ .forge/ to the checkout's .gitignore so the
    repair loop's `git add -A` (in sandbox.commit) doesn't stage the CIE
    graph DB / venv / forge state into the PR."""
    gi = project_dir / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    add = [line for line in _IGNORE_ARTIFACTS if line not in existing]
    if add:
        with gi.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(add) + "\n")


def _pr_title(issue: dict, override: Optional[str]) -> str:
    if override:
        return override
    title = (issue.get("title") or "bug").strip().splitlines()[0][:80]
    if not issue.get("number"):  # comment-driven fix (run_fix_from_comment) — no issue number
        return f"fix: {title}"
    return f"fix(issue #{issue['number']}): {title}"


def _pr_body(issue: dict, report: dict, test_rel: str) -> str:
    repo_bit = f"{issue['owner']}/{issue['repo']}" + (f"#{issue['number']}" if issue.get("number") else "")
    source_label = "Review comment" if not issue.get("number") else "Issue"
    return (
        f"Fixes {issue.get('url') or repo_bit}\n\n"
        "## What\n"
        f"autonomous fix via `atomic-forge fix` — CIE (code graph over MCP) "
        "localized the bug, generated a failing regression test, and forge's "
        "repair loop fixed the source against it.\n\n"
        f"## {source_label}\n> **{issue.get('title','').strip()}**\n\n"
        f"{(issue.get('body','') or '').strip()[:2000]}\n\n"
        "## How it was verified\n"
        f"- regression test: `{test_rel}` (CIE-generated; fails on the pre-fix "
        "code, passes on the fix)\n"
        f"- repair rounds: {report.get('rounds')}  "
        f"failures: {report.get('initial_failures')} -> {report.get('final_failures')}\n"
        f"- repaired file(s): {', '.join(report.get('repaired_files', [])) or 'n/a'}\n"
        + forge_footer()
    )


def run_fix(url: str, llm: OpenAICompatLLM, *,
            project_dir: Optional[Path] = None,
            install_cmd: Optional[str] = None,
            max_rounds: int = 5, max_turns: int = 10,
            dry_run: bool = False,
            pr_base: Optional[str] = None, pr_branch: Optional[str] = None,
            pr_title: Optional[str] = None,
            issue_body_file: Optional[Path] = None,
            work_root: Optional[Path] = None,
            samples: int = 2, max_turns_per_attempt: int = 25,
            architect_mode: bool = False,
            skip_bootstrap: bool = False, bootstrap_timeout: int = 600) -> dict:
    """Run the full fix pipeline. Returns a report dict (always; includes
    `success`, `stage`, and either `pr_url` or `reason` on failure).

    `skip_bootstrap`: only meaningful for the cold-clone path — a
    `--project-dir` checkout is a user-vouched "already runnable" tree
    (same trust level as `--install-cmd ""`) and never gates. Pass True
    to skip the R16 test-probe on a fresh clone whose suite you already
    know runs (or takes too long to probe)."""
    require_cie()
    owner, repo, number = parse_issue_url(url)
    upstream = upstream_slug(owner, repo)

    # 2. issue
    if issue_body_file:
        # "-" reads the bug description from stdin instead of a file — the
        # lowest-friction intake path: `echo "bug text" | atomic-forge fix
        # <url> --issue-body-file -`, or pipe from any other tool (a Slack
        # export, a ticket-system dump) with no filesystem step in between.
        # The URL is still required (fix targets a real repo to clone/PR
        # against — see this module's own docstring), so this narrows
        # "supply the bug text yourself" down to its cheapest form rather
        # than replacing the URL requirement.
        if str(issue_body_file) == "-":
            body_text = sys.stdin.read()
            title = body_text.strip().splitlines()[0][:80] if body_text.strip() else "issue"
        else:
            body_text = Path(issue_body_file).read_text()
            title = Path(issue_body_file).stem
        issue = {"title": title, "body": body_text,
                 "url": url, "number": number, "owner": owner, "repo": repo}
        print(f"[forge fix] using issue body from "
              f"{'stdin' if str(issue_body_file) == '-' else issue_body_file}")
    else:
        print(f"[forge fix] fetching issue {upstream}#{number}")
        issue = fetch_issue(owner, repo, number)
    bug = issue_to_bug_description(issue)

    return _run_fix_pipeline(
        owner, repo, test_id=str(number), issue=issue, bug=bug, llm=llm, url=url,
        pr_branch_default=f"forge/fix-issue-{number}", result_extra={"issue_number": number},
        project_dir=project_dir, install_cmd=install_cmd, max_rounds=max_rounds,
        max_turns=max_turns, dry_run=dry_run, pr_base=pr_base, pr_branch=pr_branch,
        pr_title=pr_title, work_root=work_root, samples=samples,
        max_turns_per_attempt=max_turns_per_attempt, architect_mode=architect_mode,
        skip_bootstrap=skip_bootstrap, bootstrap_timeout=bootstrap_timeout,
    )


def run_fix_from_comment(owner: str, repo: str, comment_body: str, file_path: str,
                         llm: OpenAICompatLLM, *,
                         line: Optional[int] = None, source_url: Optional[str] = None,
                         project_dir: Optional[Path] = None,
                         install_cmd: Optional[str] = None,
                         max_rounds: int = 5, max_turns: int = 10,
                         dry_run: bool = False,
                         pr_base: Optional[str] = None, pr_branch: Optional[str] = None,
                         pr_title: Optional[str] = None,
                         work_root: Optional[Path] = None,
                         samples: int = 2, max_turns_per_attempt: int = 25,
                         architect_mode: bool = False,
                         skip_bootstrap: bool = False, bootstrap_timeout: int = 600) -> dict:
    """Scoped variant of `run_fix` for a code-review-comment-driven fix
    (R8): the bug description comes from a review comment already
    anchored to `file_path` (+ optional `line`) instead of a GitHub issue,
    so there's no `gh issue view` fetch step — everything else (CIE index,
    regression-test generation, the repair loop, ground-truth re-check,
    fork-only PR) is the exact same pipeline `run_fix` uses, via
    `_run_fix_pipeline`.

    Scope note: this targets the repo's own default branch to clone and
    fork-PR against (same as `run_fix`), NOT the specific PR branch the
    comment was left on — pushing a fix onto an arbitrary contributor's
    own PR branch is a separate, unvalidated permissions problem (whose
    fork, does forge have write access to THAT branch) this function
    deliberately does not attempt. It answers "turn this review comment
    into a fix PR against upstream," not "amend the PR the comment is on."

    `comment_body` doubles as the bug description fed to test generation;
    prefixing it with the file (and line, if known) gives testgen a
    localization hint an issue-derived bug description wouldn't have, so
    fault-localization search starts scoped instead of cold."""
    require_cie()
    location = f"{file_path}" + (f" near line {line}" if line else "")
    bug = f"{comment_body.strip()}\n\n(Focus on {location} — that's where this comment was left.)"
    issue = {"title": f"review comment on {file_path}", "body": comment_body,
             "url": source_url or "", "number": 0, "owner": owner, "repo": repo}
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", file_path).strip("_") or "comment"
    return _run_fix_pipeline(
        owner, repo, test_id=slug, issue=issue, bug=bug, llm=llm, url=source_url or "",
        pr_branch_default=f"forge/fix-comment-{slug}", result_extra={"file_path": file_path, "line": line},
        project_dir=project_dir, install_cmd=install_cmd, max_rounds=max_rounds,
        max_turns=max_turns, dry_run=dry_run, pr_base=pr_base, pr_branch=pr_branch,
        pr_title=pr_title, work_root=work_root, samples=samples,
        max_turns_per_attempt=max_turns_per_attempt, architect_mode=architect_mode,
        skip_bootstrap=skip_bootstrap, bootstrap_timeout=bootstrap_timeout,
    )


def _run_fix_pipeline(owner: str, repo: str, *, test_id: str, issue: dict, bug: str,
                      llm: OpenAICompatLLM, url: str, pr_branch_default: str,
                      result_extra: dict,
                      project_dir: Optional[Path], install_cmd: Optional[str],
                      max_rounds: int, max_turns: int, dry_run: bool,
                      pr_base: Optional[str], pr_branch: Optional[str],
                      pr_title: Optional[str], work_root: Optional[Path],
                      samples: int, max_turns_per_attempt: int, architect_mode: bool,
                      skip_bootstrap: bool = False, bootstrap_timeout: int = 600) -> dict:
    """Shared body of `run_fix`/`run_fix_from_comment` from "get a runnable
    checkout" onward — everything upstream of this (parsing the intake
    source into an `issue` dict + `bug` description) is the only part
    that differs between an issue-driven and a comment-driven fix.
    `test_id`: a filesystem/branch-safe identifier for this run (an issue
    number, or a file-path slug) — used for the clone dir, branch name,
    and generated test filename so the two call sites never collide."""
    upstream = upstream_slug(owner, repo)

    # 3. checkout
    supplied_project_dir = project_dir
    if project_dir is None:
        work_root = Path(work_root or (Path(tempfile.gettempdir()) / "forge_fix"))
        project_dir = work_root / f"{repo}-{test_id}"
        print(f"[forge fix] cloning {upstream} -> {project_dir}")
        clone_repo(owner, repo, project_dir)
    project_dir = Path(project_dir)
    _gitignore_artifacts(project_dir)

    # 3b. R16 bootstrap gate (see bootstrap.py): a cold clone must prove
    #     "at least one test in this repo is discoverable and executable"
    #     before CIE indexing / testgen / repair run against it — a clear
    #     "could not bootstrap this repo" beats a confusing downstream
    #     failure. A --project-dir checkout is user-vouched and skips the
    #     gate (same trust level as --install-cmd "").
    _gate = None
    if supplied_project_dir is None and not skip_bootstrap:
        from . import bootstrap as bootstrap_mod
        print("[forge fix] bootstrap gate: detect stack + probe tests ...")
        # llm + allow_agentic: the R16c Repo2Run-style fallback runs only when
        # FORGE_ENABLE_AGENTIC_BOOTSTRAP=1 (it spends real tokens in a Docker
        # sandbox — never enabled merely by passing an llm).
        _gate = bootstrap_mod.run_bootstrap_gate(
            project_dir, timeout=bootstrap_timeout, llm=llm, allow_agentic=True)
        if not _gate["ok"]:
            print(f"[forge fix] abort at bootstrap gate: {_gate['verdict']} — {_gate['detail']}")
            return {"url": url, "upstream": upstream, "branch": pr_branch or pr_branch_default,
                    "test_file": f"tests/test_forge_{test_id}.py", "success": False,
                    "stage": "bootstrap", "bootstrap": _gate["verdict"],
                    "bootstrap_detail": _gate["detail"],
                    "checkpoint_run_id": _gate["checkpoint_run_id"],
                    **result_extra}
        print(f"[forge fix] bootstrap gate passed: {_gate['evidence']}")

    print(f"[forge fix] setting up venv + installing project (this can take a bit)")
    venv_py = setup_python_env(project_dir, install_cmd=install_cmd)

    # branch for the fix (commits from the repair loop land here, not on default)
    branch = pr_branch or pr_branch_default
    prepare_pr_branch(project_dir, branch)
    print(f"[forge fix] on branch {branch}")

    # 4. CIE index + MCP server
    db = project_dir / ".cie" / "graph.db"
    print(f"[forge fix] indexing code graph with CIE ...")
    cie_index(project_dir, db)
    bridge = MCPBridge(project_dir, db)
    backend = MCPToolBackend(bridge, project_dir)
    manifest = backend.describe()["results"]
    manifest_text = render_tool_manifest(manifest)

    traj = Trajectory(project_dir)
    test_rel = f"tests/test_forge_{test_id}.py"
    (project_dir / "tests").mkdir(exist_ok=True)

    result = {"url": url, "upstream": upstream, "branch": branch,
              "test_file": test_rel, "success": False,
              "bootstrap": _gate["verdict"] if _gate else "skipped",
              "checkpoint_run_id": _gate["checkpoint_run_id"] if _gate else None,
              **result_extra}
    try:
        # 5. CIE generates a regression test that reproduces the bug
        print(f"[forge fix] CIE generating regression test at {test_rel} ...")
        gen = generate_regression_test(llm, bridge, project_dir, test_rel, bug, max_turns=max_turns)
        result["testgen_turns"] = gen["turns"]
        if not gen["generated"].strip():
            result.update(stage="testgen", reason="CIE produced no test file")
            print("[forge fix] abort: no regression test generated; no PR raised.")
            return result
        fails, out = oracle_fails_on_buggy(project_dir, test_rel, venv_py)
        result["test_reproduces_bug"] = fails
        if not fails:
            result.update(stage="validate", reason="generated test does not reproduce the bug "
                          "(passes on buggy code or has a collection error)")
            print("[forge fix] abort: the generated test does not reproduce the bug; no PR raised.")
            print(f"  pytest output tail:\n" + "\n".join(out.splitlines()[-8:]))
            return result
        print("[forge fix] generated test reproduces the bug — entering repair loop.")

        # 6. forge repair loop (CIE-backed) against the generated test
        test_cmd = make_test_cmd(venv_py, test_rel)
        print(f"[forge fix] repair loop (max_rounds={max_rounds}, test={test_rel}) ...")
        report = repair_loop_agentic(
            project_dir, llm, backend, traj, test_cmd=test_cmd,
            max_rounds=max_rounds, samples=samples,
            max_turns_per_attempt=max_turns_per_attempt,
            timeout=300, tool_manifest=manifest, tool_manifest_text=manifest_text,
            tasks_by_file={}, architect_mode=architect_mode)
        result["repair"] = {
            "success": report.get("success"), "rounds": report.get("rounds"),
            "initial_failures": report.get("initial_failures"),
            "final_failures": report.get("final_failures"),
            "repaired_files": report.get("repaired_files", []),
        }

        # ground-truth re-check of the generated test (don't trust self-report)
        green = _ground_truth_green(project_dir, test_cmd)
        result["ground_truth_green"] = green
        if not green:
            result.update(stage="repair", reason="repair did not make the generated test pass")
            print("[forge fix] repair exhausted; the generated test still fails; no PR raised.")
            return result
        result["success"] = True
        result["stage"] = "fixed"

        # 7. fork-only PR (never push to origin)
        title = _pr_title(issue, pr_title)
        body = _pr_body(issue, result["repair"], test_rel)
        base = pr_base or default_branch_for(upstream)
        if dry_run:
            r = raise_pr_via_fork(project_dir, upstream=upstream, title=title, body=body,
                                  base=base, dry_run=True)
            result.update(stage="dry_run", pr_url=None, dry_run=r)
            print(f"[forge fix] dry-run: fix ready on branch {branch} (base {base}); "
                  f"NOT pushing / NOT opening a PR.")
        else:
            print(f"[forge fix] bug fixed — forking {upstream} and opening a PR ...")
            r = raise_pr_via_fork(project_dir, upstream=upstream, title=title, body=body, base=base)
            result["pr_url"] = r.get("pr_url")
            result["fork"] = r.get("fork")
            print(f"[forge fix] PR opened: {r.get('pr_url')}  (fork -> {upstream}, base {base})")
        return result
    finally:
        bridge.stop()
        usage = getattr(llm, "usage", None)
        if usage:
            print(f"[forge fix] {usage.summary()}")
        print(f"[forge fix] trajectory: {traj.path}")