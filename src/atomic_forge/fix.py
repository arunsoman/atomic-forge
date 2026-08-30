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

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .agent import render_tool_manifest
from .cie_backend import MCPBridge, MCPToolBackend, cie_index, require_cie
from .exit_audit import record_exit
from .issue import (clone_repo, fetch_issue, issue_to_bug_description,
                    make_test_cmd, parse_issue_url, setup_python_env, upstream_slug)
from .learning import run_postmortem
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


def run_repro_probe(project_dir: Path, venv_py: str, repro: Path,
                    timeout: int = 600, out_tail: int = 2000) -> tuple[bool, int, str]:
    """Run the caller's repro probe against the checkout and interpret it
    as a ground-truth contract:

      exit 0        -> the issue's behavior is NOT present on HEAD: the
                       issue is stale/already fixed upstream. Abort before
                       any LLM spend — no index, no testgen, no repair, no
                       PR (the pilot measured 2/4 targets exactly here).
      exit non-zero -> the bug reproduces (expected on a live issue); the
                       output tail is kept so a human can tell a true
                       repro apart from an environment crash, and so the
                       abort reason is legible in learning.json/trajectory.

    .py scripts run under the project venv's python (the same interpreter
    the repair loop's tests use); anything else runs under bash, so a
    one-line shell repro works too. The exit code alone decides — output
    only decorates. On timeout the probe is treated as bug-present (the
    pessimistic reading: a hang is at least not evidence of a fix)."""
    repro = Path(repro)
    if repro.suffix == ".py":
        cmd = [venv_py, str(repro)]
    else:
        cmd = ["bash", str(repro)]
    env = dict(os.environ)
    venv_bin = str(Path(venv_py).resolve().parent)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    if "/bin/python" in venv_py or "/bin/python3" in venv_py:
        env.setdefault("VIRTUAL_ENV", str(Path(venv_bin).parent))
    try:
        r = subprocess.run(cmd, cwd=str(project_dir), capture_output=True,
                           text=True, timeout=timeout, env=env)
        out = f"{r.stdout}\n{r.stderr}".strip()
        return r.returncode != 0, r.returncode, out[-out_tail:]
    except subprocess.TimeoutExpired as e:
        def _s(x):
            return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        out = _s(e.stdout) + _s(e.stderr).strip()
        return True, -1, (f"(repro probe timed out after {timeout}s — counted as "
                          f"bug-present)\n{out[-out_tail:]}").strip()


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
            skip_bootstrap: bool = False, bootstrap_timeout: int = 600,
            repro: Optional[Path] = None, test_file: Optional[Path] = None) -> dict:
    """Run the full fix pipeline. Returns a report dict (always; includes
    `success`, `stage`, and either `pr_url` or `reason` on failure). Returns a report dict (always; includes
    `success`, `stage`, and either `pr_url` or `reason` on failure).

    `skip_bootstrap`: only meaningful for the cold-clone path — a
    `--project-dir` checkout is a user-vouched "already runnable" tree
    (same trust level as `--install-cmd ""`) and never gates. Pass True
    to skip the R16 test-probe on a fresh clone whose suite you already
    know runs (or takes too long to probe).

    `repro` (F1 pre-flight gate): path to a repro script executed against
    the untouched checkout right after the venv exists and BEFORE any
    LLM spend. Contract: non-zero exit while the bug is present, exit 0
    once fixed. A probe that exits 0 on HEAD aborts the run as
    `issue_already_fixed` (stale issue); after repair the same probe
    must flip to exit 0 before a PR is raised (`repro_still_failing`
    aborts) — the generated regression test stays the primary oracle,
    this is the independent second witness.

    `test_file` (F4 operator-supplied test): path to a caller-authored
    regression test copied to `tests/test_forge_<id>.py`, skipping testgen
    entirely. The oracle still gates it — it must fail on HEAD (a test
    that passes is the issue_already_fixed abort, "test_not_reproducing"
    if it can't reproduce). Use when the testgen agent can't express a bug
    (render-order classes, fixture-heavy asserts) — the campaign pilot's
    #1 residual loss bucket. Everything downstream (repair loop, ground
    truth, blast radius, PR) is unchanged."""
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
        repro=repro, test_file=test_file,
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
                      skip_bootstrap: bool = False, bootstrap_timeout: int = 600,
                      repro: Optional[Path] = None,
                      test_file: Optional[Path] = None) -> dict:
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
            record_exit(project_dir, reason="bootstrap_fail", detail=_gate["detail"],
                       extra={"issue": url, "verdict": _gate["verdict"]})
            return {"url": url, "upstream": upstream, "branch": pr_branch or pr_branch_default,
                    "test_file": f"tests/test_forge_{test_id}.py", "success": False,
                    "stage": "bootstrap", "bootstrap": _gate["verdict"],
                    "bootstrap_detail": _gate["detail"],
                    "checkpoint_run_id": _gate["checkpoint_run_id"],
                    **result_extra}
        print(f"[forge fix] bootstrap gate passed: {_gate['evidence']}")

    print(f"[forge fix] setting up venv + installing project (this can take a bit)")
    venv_py = setup_python_env(project_dir, install_cmd=install_cmd)

    # 3c. F1 pre-flight repro gate — cheap ground truth before any expensive
    #     work. The caller's probe must reproduce the bug (non-zero exit) on
    #     this untouched checkout; exit 0 means the issue is stale upstrean
    #     and the whole CIE/testgen/repair/PR pipeline is skipped for free.
    repro_confirmed = False
    if repro is not None:
        print(f"[forge fix] repro probe: {Path(repro).name} on HEAD "
              f"(a non-zero exit is expected while the bug is present) ...")
        fails_on_head, _code, out_tail = run_repro_probe(project_dir, venv_py, repro)
        if not fails_on_head:
            reason = ("repro probe exits 0 on HEAD — issue looks already fixed "
                      "upstream; aborting before CIE/testgen/repair (no LLM tokens spent)")
            print(f"[forge fix] abort: {reason}\n"
                  + "\n".join("  " + ln for ln in out_tail.splitlines()[-10:]))
            record_exit(project_dir, reason="issue_already_fixed", detail=out_tail[-500:],
                       extra={"issue": url})
            return {"url": url, "upstream": upstream, "branch": pr_branch or pr_branch_default,
                    "test_file": f"tests/test_forge_{test_id}.py", "success": False,
                    "stage": "repro", "reason": reason, "repro_exit_code": 0,
                    **result_extra}
        repro_confirmed = True
        print("[forge fix] repro probe failed as expected — bug is present on HEAD")

    # branch for the fix (commits from the repair loop land here, not on default)
    branch = pr_branch or pr_branch_default
    prepare_pr_branch(project_dir, branch)
    print(f"[forge fix] on branch {branch}")

    test_rel = f"tests/test_forge_{test_id}.py"
    result = {"url": url, "upstream": upstream, "branch": branch,
              "test_file": test_rel, "success": False,
              "bootstrap": _gate["verdict"] if _gate else "skipped",
              "checkpoint_run_id": _gate["checkpoint_run_id"] if _gate else None,
              **result_extra}
    if repro_confirmed:
        result["repro_fails_on_head"] = True

    # 4. CIE index + MCP server. CIE is the ONLY code-understanding backend
    # forge's testgen/repair agents use — there is no silent degraded mode
    # at this layer (unlike CIE's own internal graph-query-vs-heuristic-index
    # fallback, which lives in the `cie` package itself and is out of forge's
    # control). If indexing, the MCP bridge, or the tool manifest can't be
    # stood up at all, fail fast here with a clear reason instead of letting
    # testgen/repair spend rounds against a broken or absent backend, or
    # crash later with a raw traceback the way an unhandled exception here
    # used to (this whole block was previously outside any try/except).
    db = project_dir / ".cie" / "graph.db"
    print(f"[forge fix] indexing code graph with CIE ...")
    bridge = None
    try:
        cie_index(project_dir, db)
        bridge = MCPBridge(project_dir, db)
        backend = MCPToolBackend(bridge, project_dir)
        manifest = backend.describe()["results"]
    except Exception as e:
        if bridge is not None:
            bridge.stop()  # bridge came up but describe()/manifest failed after — don't leak its thread
        record_exit(project_dir, reason="cie_unavailable", detail=str(e)[:500],
                   extra={"issue": url})
        result.update(stage="cie_setup", reason=f"CIE unavailable: {e}")
        print(f"[forge fix] abort: CIE unavailable as the MCP backend ({e}); no PR raised.")
        return result
    manifest_text = render_tool_manifest(manifest)

    traj = Trajectory(project_dir)
    (project_dir / "tests").mkdir(exist_ok=True)

    try:
        # 5. regression test that reproduces the bug: operator-authored via
        # --test-file (testgen skipped — its oracle still gates), or CIE-
        # generated, fed the FULL bug description, comments included (see
        # issue_to_bug_description): a thin original report plus a
        # maintainer's repro steps in a follow-up comment is common, and
        # testgen used to only ever see the original report.
        if test_file is not None:
            src = Path(test_file)
            if not src.is_file():
                raise FileNotFoundError(f"--test-file not found: {src}")
            dest = project_dir / test_rel
            if src.resolve() != dest.resolve():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            result["test_source"] = "operator"
            print(f"[forge fix] operator-supplied test at {test_rel} (testgen skipped)")
            gen = {"generated": dest.read_text(), "turns": 0}
        else:
            print(f"[forge fix] CIE generating regression test at {test_rel} ...")
            gen = generate_regression_test(llm, bridge, project_dir, test_rel, bug, max_turns=max_turns)
        result["testgen_turns"] = gen["turns"]
        if not gen["generated"].strip():
            result.update(stage="testgen", reason="no test file ("
                          + ("operator-supplied file is empty" if test_file is not None
                             else "CIE produced nothing") + ")")
            record_exit(project_dir, reason="no_test_generated",
                       detail=(f"operator-supplied test file was empty" if test_file is not None else
                               f"testgen agent used its full {max_turns}-turn budget "
                               "(comments included in the bug description) without writing a test"),
                       extra={"issue": url})
            print("[forge fix] abort: no regression test generated; no PR raised.")
            return result
        fails, out = oracle_fails_on_buggy(project_dir, test_rel, venv_py)
        result["test_reproduces_bug"] = fails
        if not fails:
            result.update(stage="validate", reason=("operator-supplied" if test_file is not None
                          else "generated") + " test does not reproduce the bug "
                          "(passes on buggy code or has a collection error)")
            record_exit(project_dir, reason="test_not_reproducing",
                       detail=out[-500:], extra={"issue": url})
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

        # The repair loop's OWN initial test run just found the test green
        # at round 0 — despite oracle_fails_on_buggy() confirming moments
        # ago that it fails. That's a contradiction (test flakiness, or an
        # environment difference between the two runs), not a fix: zero
        # rounds ran, nothing was patched, nothing was committed. Proceeding
        # from here used to silently PR an unchanged branch — `gh pr create`
        # would fail minutes later with an opaque "No commits between X and
        # Y", and 3 real attempts (psf/black#5214, psf/black#4420,
        # Delgan/loguru#1502) were lost exactly this way in the round-2
        # sweep. Fail fast here instead, with the actual reason on record.
        if report.get("rounds") == 0:
            result.update(stage="repair",
                          reason="repair loop's own initial check found the test already "
                                 "passing, contradicting the oracle check moments earlier "
                                 "(flake or environment drift) — no patch was made")
            record_exit(project_dir, reason="test_already_passing",
                       detail="oracle_fails_on_buggy() said the test fails; repair_loop_agentic's "
                              "own round-0 check said it passes",
                       extra={"issue": url})
            print("[forge fix] abort: repair loop found the test already passing at round 0 "
                  "(contradicts the oracle check) — no patch made, no PR raised.")
            return result

        # ground-truth re-check of the generated test (don't trust self-report)
        green = _ground_truth_green(project_dir, test_cmd)
        result["ground_truth_green"] = green
        if not green:
            result.update(stage="repair", reason="repair did not make the generated test pass")
            record_exit(project_dir, reason="repair_exhausted",
                       detail=f"rounds={report.get('rounds')} "
                              f"final_failures={report.get('final_failures')}",
                       extra={"issue": url})
            print("[forge fix] repair exhausted; the generated test still fails; no PR raised.")
            # Post-mortem learning engine (best-effort, never fatal): study
            # the full trajectory for what was tried, what wasn't, and
            # whether a new MCP/CIE tool function would plausibly have
            # changed the outcome. Written to .forge/learning.json(l) for
            # later study — never re-enters this (already-terminal) attempt.
            try:
                pm = run_postmortem(llm, project_dir, traj.path, bug_description=bug,
                                    exit_reason="repair_exhausted")
                print(f"[forge fix] postmortem written to {project_dir / '.forge' / 'learning.json'} "
                      f"({len(pm.get('untried_paths', []))} untried path(s) identified, "
                      f"new_tool_would_help={pm.get('new_tool_would_help')})")
            except Exception as e:
                print(f"[forge fix] postmortem failed (non-fatal): {e}")
            return result
        result["success"] = True
        result["stage"] = "fixed"

        # F1 second witness: the caller's independent repro probe must flip
        # to exit 0 before anything is raised. The generated regression test
        # remains the primary oracle; this is the independent cross-check.
        if repro is not None:
            print("[forge fix] re-running repro probe against the repaired tree ...")
            _now_green, code2, out2 = run_repro_probe(project_dir, venv_py, repro)
            result["repro_fixed_after_repair"] = code2 == 0
            if code2 != 0:
                result.update(success=False, stage="repair",
                              reason="repair made the generated test pass but the caller's "
                                     f"repro probe still exits non-zero (exit {code2})")
                record_exit(project_dir, reason="repro_still_failing", detail=out2[-500:],
                           extra={"issue": url})
                print("[forge fix] abort: repro probe still fails after repair; "
                      "no PR raised (the generated test passing alone is not "
                      "independently convincing).")
                return result

        # 7. fork-only PR (never push to origin)
        title = _pr_title(issue, pr_title)
        body = _pr_body(issue, result["repair"], test_rel)
        base = pr_base or default_branch_for(upstream, project_dir=project_dir)
        try:
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
        except Exception as e:
            result.update(success=False, stage="pr_create", reason=str(e))
            record_exit(project_dir, reason="pr_create_failed", detail=str(e)[:500],
                       extra={"issue": url})
            print(f"[forge fix] abort: PR creation failed after a validated fix ({e}). "
                  f"The fix itself is real and committed on branch {branch}.")
            return result
        record_exit(project_dir, reason="success",
                   detail=result.get("pr_url") or "dry_run", extra={"issue": url})
        return result
    finally:
        if bridge is not None:
            bridge.stop()
        usage = getattr(llm, "usage", None)
        if usage:
            print(f"[forge fix] {usage.summary()}")
        print(f"[forge fix] trajectory: {traj.path}")