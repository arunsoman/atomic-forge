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
    monkeypatch.setattr(F, "default_branch_for", lambda upstream: "main")
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