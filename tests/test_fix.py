"""Tests for `atomic-forge fix <url>`.

Covers the deterministic plumbing (URL parsing, oracle validation, gitignore
artifacts, test-cmd shape) and the orchestration control flow (CIE-required
gate, the "abort if the test doesn't reproduce the bug" honesty gate, and the
fork-only dry-run path that never pushes to origin). The live CIE/LLM/clone
path is exercised manually, not here."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from atomic_forge.issue import (issue_to_bug_description, make_test_cmd,
                                 parse_issue_url)
from atomic_forge.testgen import oracle_fails_on_buggy


# --------------------------------------------------------------- URL parsing
def test_parse_issue_url_ok():
    assert parse_issue_url("https://github.com/mahmoud/boltons/issues/42") == ("mahmoud", "boltons", 42)
    assert parse_issue_url("https://github.com/A/B/issues/7/") == ("A", "B", 7)
    assert parse_issue_url("http://github.com/A/B/issues/7?foo=bar") == ("A", "B", 7)


@pytest.mark.parametrize("bad", [
    "", "not a url", "https://github.com/A/B", "https://gitlab.com/A/B/issues/1",
    "https://github.com/A/B/pull/1", "https://github.com/A/B/issues/",
])
def test_parse_issue_url_bad(bad):
    with pytest.raises(ValueError):
        parse_issue_url(bad)


def test_issue_to_bug_description():
    assert issue_to_bug_description({"title": "T", "body": "B"}) == "T\n\nB"
    assert issue_to_bug_description({"title": "T", "body": ""}) == "T"
    assert issue_to_bug_description({"title": "", "body": ""}) == "(no issue body)"


def test_issue_to_bug_description_includes_comments():
    issue = {"title": "T", "body": "B", "comments": [
        {"author": {"login": "maintainer"}, "body": "actually repro with x=1"},
        {"author": "userbot", "body": "same here on 3.12"},
    ]}
    out = issue_to_bug_description(issue)
    assert out.startswith("T\n\nB")
    assert "## Comments" in out
    assert "@maintainer: actually repro with x=1" in out
    assert "@userbot: same here on 3.12" in out


def test_issue_to_bug_description_no_comments_key_is_unaffected():
    # backward compatible: an issue dict with no "comments" at all (the old
    # shape, or a comment-driven fix's synthetic issue dict) behaves exactly
    # as before this feature was added.
    assert issue_to_bug_description({"title": "T", "body": "B"}) == "T\n\nB"


def test_issue_to_bug_description_skips_empty_comments():
    issue = {"title": "T", "body": "B", "comments": [{"author": "x", "body": "   "}]}
    assert "## Comments" not in issue_to_bug_description(issue)


def test_issue_to_bug_description_truncates_long_comment_threads():
    big_comment = "x" * 5000
    issue = {"title": "T", "body": "B", "comments": [
        {"author": "a", "body": big_comment},
        {"author": "b", "body": big_comment},  # would exceed _MAX_COMMENT_CHARS combined
        {"author": "c", "body": "this one should be dropped"},
    ]}
    out = issue_to_bug_description(issue)
    assert "@a:" in out
    assert "this one should be dropped" not in out  # cut off once the char budget is hit


def test_make_test_cmd_uses_venv_python():
    cmd = make_test_cmd("/p/.venv/bin/python", "tests/test_forge_issue_1.py")
    assert cmd.startswith("/p/.venv/bin/python -m pytest ")
    assert "tests/test_forge_issue_1.py" in cmd
    assert "-p no:cacheprovider" in cmd


# ------------------------------------------------------- oracle validation
@pytest.fixture
def buggy_project(tmp_path):
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a - b  # bug: subtracts\n")
    return tmp_path


def _write_test(project, body):
    (project / "test_mod.py").write_text(body)


def test_oracle_fails_on_assertion_is_a_valid_reproduction(buggy_project):
    _write_test(buggy_project, "from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    fails, out = oracle_fails_on_buggy(buggy_project, "test_mod.py", py="python")
    assert fails is True


def test_oracle_passing_on_buggy_does_not_reproduce(buggy_project):
    _write_test(buggy_project, "from mod import add\n\ndef test_add():\n    assert add(2, 3) == -1  # wrong oracle\n")
    fails, _ = oracle_fails_on_buggy(buggy_project, "test_mod.py", py="python")
    assert fails is False  # passes on buggy -> not a reproduction


def test_oracle_collection_error_does_not_count(buggy_project):
    _write_test(buggy_project, "from nope_does_not_exist import x  # import error\n\ndef test_x():\n    assert x\n")
    fails, out = oracle_fails_on_buggy(buggy_project, "test_mod.py", py="python")
    assert fails is False  # collection/import error -> test itself is broken
    assert "error" in out.lower()


# ------------------------------------------------------- gitignore artifacts
def test_gitignore_artifacts(tmp_path):
    from atomic_forge.fix import _gitignore_artifacts
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    _gitignore_artifacts(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    assert ".cie/" in text and ".venv/" in text and ".forge/" in text
    assert text.startswith("*.pyc\n")  # original content preserved


# ------------------------------------------------------- orchestration flow
def _git_init(repo):
    for args in (["init", "-b", "main", str(repo)], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["add", "."], ["commit", "-m", "init"]):
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("x = 1\n")
    _git_init(repo)
    return repo


class _DummyLLM:
    class usage:
        prompt_tokens = 0
        completion_tokens = 0
        @staticmethod
        def summary():
            return "llm_calls=0"


def _stub_chain(monkeypatch, *, oracle_fails, repair_success, green=True):
    """Stub everything run_fix() touches below the orchestration layer."""
    import atomic_forge.fix as F
    monkeypatch.setattr(F, "require_cie", lambda: None)
    monkeypatch.setattr(F, "setup_python_env", lambda pd, install_cmd=None: "python")
    monkeypatch.setattr(F, "cie_index", lambda pd, db: "indexed")
    monkeypatch.setattr(F, "render_tool_manifest", lambda m: "")

    class _Bridge:
        def __init__(self, *a, **k): pass
        def call(self, name, **kw): return {"ok": True, "results": []}
        def stop(self): pass
    monkeypatch.setattr(F, "MCPBridge", _Bridge)

    class _Backend:
        def __init__(self, *a, **k): pass
        def describe(self): return {"results": []}
    monkeypatch.setattr(F, "MCPToolBackend", _Backend)

    class _Traj:
        path = "/tmp/traj.jsonl"
    monkeypatch.setattr(F, "Trajectory", lambda pd: _Traj())

    monkeypatch.setattr(F, "generate_regression_test",
                        lambda llm, br, pd, tr, bug, max_turns=10: {"generated": "test", "turns": 3})
    monkeypatch.setattr(F, "oracle_fails_on_buggy", lambda pd, tr, py: (oracle_fails, "out"))

    def _repair(pd, llm, tools, traj, **kw):
        return {"success": repair_success, "rounds": 1, "initial_failures": 1,
                "final_failures": 0 if repair_success else 1, "repaired_files": ["mod.py"]}
    monkeypatch.setattr(F, "repair_loop_agentic", _repair)

    if green:
        monkeypatch.setattr(F, "_ground_truth_green", lambda pd, cmd, timeout=300: True)
    else:
        monkeypatch.setattr(F, "_ground_truth_green", lambda pd, cmd, timeout=300: False)
    monkeypatch.setattr(F, "default_branch_for", lambda upstream, project_dir=None: "main")
    raised = {"called": False}
    def _raise_pr(pd, *, upstream, title, body, base, dry_run=False):
        raised["called"] = True
        raised["dry_run"] = dry_run
        return {"dry_run": True, "branch": "forge/fix-issue-1", "base": base, "title": title} \
            if dry_run else {"pr_url": "https://github.com/up/repo/pull/1", "branch": "b", "base": base}
    monkeypatch.setattr(F, "raise_pr_via_fork", _raise_pr)
    return raised


def test_run_fix_requires_cie(monkeypatch):
    import atomic_forge.fix as F
    def _no(): raise RuntimeError("CIE not installed")
    monkeypatch.setattr(F, "require_cie", _no)
    with pytest.raises(RuntimeError, match="CIE"):
        F.run_fix("https://github.com/o/r/issues/1", _DummyLLM())


def test_run_fix_bad_url(monkeypatch):
    import atomic_forge.fix as F
    monkeypatch.setattr(F, "require_cie", lambda: None)
    with pytest.raises(ValueError):
        F.run_fix("not a url", _DummyLLM())


def test_run_fix_aborts_when_test_does_not_reproduce(monkeypatch, fake_repo):
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=False, repair_success=True, green=False)
    # issue body from a file so no gh fetch; project_dir so no clone
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    assert "does not reproduce" in r["reason"]
    assert raised["called"] is False  # no PR raised when the test can't reproduce the bug


def test_run_fix_dry_run_never_pushes(monkeypatch, fake_repo):
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is True
    assert r["stage"] == "dry_run"
    assert r.get("pr_url") is None
    assert raised["called"] is True and raised["dry_run"] is True  # fork PR helper called in dry_run mode


def test_run_fix_real_pr_uses_fork_only(monkeypatch, fake_repo):
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=False)
    assert r["success"] is True
    assert r["pr_url"] == "https://github.com/up/repo/pull/1"
    assert raised["called"] is True and raised["dry_run"] is False  # went through raise_pr_via_fork (fork-only, never origin)


def test_run_fix_issue_body_from_stdin(monkeypatch, fake_repo):
    """--issue-body-file - (a literal dash) reads the bug text from stdin
    instead of a file — the lowest-friction intake path (R7): `echo "bug
    text" | atomic-forge fix <url> --issue-body-file -`, no filesystem
    step, no `gh` fetch. The URL is still required (fix targets a real
    repo to clone/PR against)."""
    import io
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    monkeypatch.setattr(F.sys, "stdin", io.StringIO("add(2,3) should be 5 but returns -1\nmore detail here"))
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=Path("-"), dry_run=True)
    assert r["success"] is True
    assert raised["called"] is True


# ------------------------------------------------------- run_fix_from_comment (R8)
def test_run_fix_from_comment_scopes_bug_to_file(monkeypatch, fake_repo):
    """The comment-driven path (R8) builds a bug description that names
    the file (and line, if given) the comment was anchored to, and skips
    the gh issue fetch entirely — no `number` needed."""
    import atomic_forge.fix as F
    captured = {}
    monkeypatch.setattr(F, "require_cie", lambda: None)
    monkeypatch.setattr(F, "setup_python_env", lambda pd, install_cmd=None: "python")
    monkeypatch.setattr(F, "cie_index", lambda pd, db: "indexed")
    monkeypatch.setattr(F, "render_tool_manifest", lambda m: "")

    class _Bridge:
        def __init__(self, *a, **k): pass
        def call(self, name, **kw): return {"ok": True, "results": []}
        def stop(self): pass
    monkeypatch.setattr(F, "MCPBridge", _Bridge)

    class _Backend:
        def __init__(self, *a, **k): pass
        def describe(self): return {"results": []}
    monkeypatch.setattr(F, "MCPToolBackend", _Backend)

    class _Traj:
        path = "/tmp/traj.jsonl"
    monkeypatch.setattr(F, "Trajectory", lambda pd: _Traj())

    def _gen(llm, br, pd, tr, bug, max_turns=10):
        captured["bug"] = bug
        return {"generated": "test", "turns": 1}
    monkeypatch.setattr(F, "generate_regression_test", _gen)
    monkeypatch.setattr(F, "oracle_fails_on_buggy", lambda pd, tr, py: (True, "out"))

    def _repair(pd, llm, tools, traj, **kw):
        return {"success": True, "rounds": 1, "initial_failures": 1,
                "final_failures": 0, "repaired_files": ["mod.py"]}
    monkeypatch.setattr(F, "repair_loop_agentic", _repair)
    monkeypatch.setattr(F, "_ground_truth_green", lambda pd, cmd, timeout=300: True)
    monkeypatch.setattr(F, "default_branch_for", lambda upstream, project_dir=None: "main")

    raised = {"called": False}
    def _raise_pr(pd, *, upstream, title, body, base, dry_run=False):
        raised.update(called=True, title=title, body=body)
        return {"dry_run": True, "branch": "b", "base": base, "title": title}
    monkeypatch.setattr(F, "raise_pr_via_fork", _raise_pr)

    r = F.run_fix_from_comment(
        "o", "r", "this off-by-one looks wrong", "mod.py", _DummyLLM(),
        line=42, project_dir=fake_repo, dry_run=True,
    )
    assert r["success"] is True
    assert r["file_path"] == "mod.py"
    assert r["line"] == 42
    assert "mod.py" in captured["bug"]
    assert "line 42" in captured["bug"]
    assert "this off-by-one looks wrong" in captured["bug"]
    assert raised["called"] is True
    assert "issue #" not in raised["title"]  # no fake issue number in a comment-driven PR title


def test_run_fix_from_comment_uses_distinct_test_file_from_issue_fix(monkeypatch, fake_repo):
    """test_id (used for the generated test filename/branch) is derived
    from file_path, not an issue number — so a comment-driven fix and an
    issue-driven fix on the same repo never collide on test filename."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    r = F.run_fix_from_comment(
        "o", "r", "please fix this", "src/app/mod.py", _DummyLLM(),
        project_dir=fake_repo, dry_run=True,
    )
    assert r["success"] is True
    assert r["test_file"] == "tests/test_forge_src_app_mod_py.py"
    assert raised["called"] is True


# ------------------------------------------------------- fail-fast paths --
def test_run_fix_cie_unavailable_fails_fast(monkeypatch, fake_repo):
    """CIE is the only code-understanding backend testgen/repair use — if
    indexing/the MCP bridge/the tool manifest can't be stood up at all,
    abort immediately with a clear reason instead of a raw traceback."""
    import atomic_forge.fix as F
    monkeypatch.setattr(F, "require_cie", lambda: None)
    monkeypatch.setattr(F, "setup_python_env", lambda pd, install_cmd=None: "python")
    def _boom(pd, db):
        raise RuntimeError("cie index failed (exit 1): boom")
    monkeypatch.setattr(F, "cie_index", _boom)
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    assert r["stage"] == "cie_setup"
    assert "CIE unavailable" in r["reason"]
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "cie_unavailable"


def test_run_fix_aborts_when_repair_finds_test_already_passing(monkeypatch, fake_repo):
    """repair_loop_agentic's OWN round-0 check found the test green, despite
    oracle_fails_on_buggy() having just confirmed it fails — a flake/env
    contradiction, not a fix. Must fail fast rather than proceed to PR-raise
    an unchanged branch (the exact way 3 real attempts were lost in the
    round-2 sweep: psf/black#5214, psf/black#4420, Delgan/loguru#1502)."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    def _repair(pd, llm, tools, traj, **kw):
        return {"success": True, "rounds": 0, "initial_failures": 0,
                "final_failures": 0, "repaired_files": []}
    monkeypatch.setattr(F, "repair_loop_agentic", _repair)
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    assert "already passing" in r["reason"]
    assert raised["called"] is False  # never reached PR-raise
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "test_already_passing"


def test_run_fix_pr_create_failure_is_caught_cleanly(monkeypatch, fake_repo):
    """A validated fix whose PR creation fails (e.g. a genuine fork-sync
    race that outlasts the retry budget) must return a clean failed result,
    not propagate a raw exception out of run_fix()."""
    import atomic_forge.fix as F
    _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    def _raise_pr(pd, *, upstream, title, body, base, dry_run=False):
        raise RuntimeError("gh pr create failed: some transient error")
    monkeypatch.setattr(F, "raise_pr_via_fork", _raise_pr)
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=False)
    assert r["success"] is False
    assert r["stage"] == "pr_create"
    assert "transient error" in r["reason"]
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "pr_create_failed"


def test_run_fix_repair_exhausted_runs_postmortem(monkeypatch, fake_repo):
    """When repair genuinely exhausts its round budget, the learning engine
    runs (best-effort) and its result is on record for later study."""
    import atomic_forge.fix as F
    _stub_chain(monkeypatch, oracle_fails=True, repair_success=False, green=False)
    called = {}
    def _pm(llm, pd, traj_path, *, bug_description, exit_reason):
        called["hit"] = True
        called["exit_reason"] = exit_reason
        return {"untried_paths": ["x"], "new_tool_would_help": False}
    monkeypatch.setattr(F, "run_postmortem", _pm)
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    assert called.get("hit") is True
    assert called["exit_reason"] == "repair_exhausted"
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "repair_exhausted"


def test_run_fix_postmortem_failure_is_non_fatal(monkeypatch, fake_repo):
    """A postmortem that itself blows up must not take down the outer
    (already-failed) run's own clean result."""
    import atomic_forge.fix as F
    _stub_chain(monkeypatch, oracle_fails=True, repair_success=False, green=False)
    def _pm(*a, **kw):
        raise RuntimeError("llm exploded")
    monkeypatch.setattr(F, "run_postmortem", _pm)
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    assert r["stage"] == "repair"


def test_run_fix_success_records_exit_audit(monkeypatch, fake_repo):
    import atomic_forge.fix as F
    _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
             project_dir=fake_repo, issue_body_file=ib, dry_run=False)
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "success"


def test_run_fix_no_test_generated_records_exit_audit(monkeypatch, fake_repo):
    import atomic_forge.fix as F
    _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    monkeypatch.setattr(F, "generate_regression_test",
                        lambda llm, br, pd, tr, bug, max_turns=10: {"generated": "", "turns": 10})
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "no_test_generated"


def test_run_fix_test_not_reproducing_records_exit_audit(monkeypatch, fake_repo):
    import atomic_forge.fix as F
    _stub_chain(monkeypatch, oracle_fails=False, repair_success=True, green=False)
    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
             project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "test_not_reproducing"


# --------------------------------------------- LLMQuotaError (core-library gap)
#
# Confirmed live 2026-08-30/31: testgen's/repair's underlying LLM call
# raising a plain RuntimeError after exhausting its retries against an
# Ollama Cloud quota wall used to crash the WHOLE process uncaught — no
# exit_audit row at all — and, had it been caught anywhere generically,
# would have been silently recorded as a real repair/testgen failure. 32
# of 34 logged repair_fail/bootstrap_fail campaign attempts were this
# exact condition. These tests prove the fix at the CORE library level
# (llm.py's LLMQuotaError, caught in fix.py's `_run_fix_pipeline`), not
# just in the campaign script that classifies a subprocess's stdout.

def test_run_fix_testgen_llm_quota_error_is_caught_cleanly(monkeypatch, fake_repo):
    """The LLM failure happens inside testgen's own agent loop
    (generate_regression_test) — must not crash run_fix, must record the
    honest `llm_unavailable` exit reason (never `repair_exhausted` or an
    uncaught traceback), and must never raise a PR."""
    import atomic_forge.fix as F
    from atomic_forge.llm import LLMQuotaError
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)

    def _quota_boom(llm, br, pd, tr, bug, max_turns=10):
        raise LLMQuotaError("LLM call failed after 4 retries: Error code: 429 - session usage limit")
    monkeypatch.setattr(F, "generate_regression_test", _quota_boom)

    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)

    assert r["success"] is False
    assert r["stage"] == "llm"
    assert "quota" in r["reason"].lower() or "rate-limit" in r["reason"].lower()
    assert raised["called"] is False  # never got near raising a PR

    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "llm_unavailable"
    assert exits[-1]["reason"] != "repair_exhausted"


def test_run_fix_repair_loop_llm_quota_error_is_caught_cleanly(monkeypatch, fake_repo):
    """The LLM failure happens deep inside the repair loop's own agentic
    sampling (repair_loop_agentic) — the highest-stakes site, since it's
    the most expensive part of the pipeline. Must not be conflated with
    `repair_exhausted` (which implies a REAL repair attempt ran out of
    rounds using working LLM calls — false here: no real attempt was
    made)."""
    import atomic_forge.fix as F
    from atomic_forge.llm import LLMQuotaError
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)

    def _quota_boom(pd, llm, tools, traj, **kw):
        raise LLMQuotaError("LLM call failed after 4 retries: Error code: 429 - rate limit exceeded")
    monkeypatch.setattr(F, "repair_loop_agentic", _quota_boom)

    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)

    assert r["success"] is False
    assert r["stage"] == "llm"
    assert raised["called"] is False

    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == "llm_unavailable"
    assert exits[-1]["reason"] != "repair_exhausted"


def test_run_fix_bootstrap_gate_llm_quota_error_is_caught_cleanly(monkeypatch, tmp_path):
    """The R16c agentic bootstrap fallback's own configurator LLM call can
    hit the same quota wall, on a cold-clone path (BEFORE the main
    testgen/repair try-block even starts) — must be caught there too,
    with `stage: bootstrap` + `llm_unavailable`, never a raw crash and
    never `bootstrap_fail` (which would misleadingly imply the checkout
    genuinely couldn't be bootstrapped)."""
    import atomic_forge.fix as F
    import atomic_forge.bootstrap as B
    from atomic_forge.llm import LLMQuotaError

    def _fake_clone(owner, repo, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "mod.py").write_text("x = 1\n")
        _git_init(dest)
    monkeypatch.setattr(F, "clone_repo", _fake_clone)

    def _boom(project_dir, timeout=600, on_progress=None, db_path=None, llm=None, allow_agentic=False):
        raise LLMQuotaError("LLM call failed after 4 retries: Error code: 429")
    monkeypatch.setattr(B, "run_bootstrap_gate", _boom)

    ib = tmp_path / "issue.txt"; ib.write_text("bug")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  work_root=tmp_path / "work", issue_body_file=ib, dry_run=True)

    assert r["success"] is False
    assert r["stage"] == "bootstrap"
    assert "quota" in r["reason"].lower() or "rate-limit" in r["reason"].lower()

    from atomic_forge.exit_audit import read_exits
    project_dir = tmp_path / "work" / "r-1"
    exits = read_exits(project_dir)
    assert exits[-1]["reason"] == "llm_unavailable"
    assert exits[-1]["reason"] != "bootstrap_fail"


def test_run_fix_non_quota_runtime_error_still_propagates(monkeypatch, fake_repo):
    """A plain (non-quota) RuntimeError from anywhere in the pipeline must
    NOT be caught by the new LLMQuotaError handling — only the
    specifically-classified quota/rate-limit condition gets the honest
    llm_unavailable treatment; every other exception keeps its old,
    loud, uncaught behavior."""
    import atomic_forge.fix as F
    _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)

    def _boom(llm, br, pd, tr, bug, max_turns=10):
        raise RuntimeError("some unrelated real bug in testgen")
    monkeypatch.setattr(F, "generate_regression_test", _boom)

    ib = fake_repo / "issue.txt"; ib.write_text("bug")
    with pytest.raises(RuntimeError, match="unrelated real bug"):
        F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                 project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(fake_repo)
    assert not exits or exits[-1]["reason"] != "llm_unavailable"