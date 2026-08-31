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
    # Preflight policy checks (added 2026-08-31, see pr.py) call `gh api`
    # for real — stub both to "clear" so tests never depend on network/gh
    # auth, matching the rest of this stub chain's philosophy of not
    # touching anything outside the process.
    monkeypatch.setattr(F, "check_ai_policy", lambda upstream: None)
    monkeypatch.setattr(F, "issue_already_settled", lambda upstream, number: None)
    monkeypatch.setattr(F, "already_has_open_pr", lambda upstream: None)

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


@pytest.mark.parametrize("err_text,expected_reason", [
    ("gh pr create failed: the pushed branch has no commits ahead of "
     "'main' — nothing to open a PR for. gh said: No commits between "
     "pylint-dev:main and arunsoman:forge/fix-issue-11361 (createPullRequest)",
     "pr_mechanics_fail"),
    ("you (amazing_williams) have reached your session usage limit, "
     "upgrade for higher limits", "quota_exceeded"),
    ("gh pr create failed: You don't have the correct permissions to "
     "execute `CreatePullRequest`", "pr_locked"),
    ("gh: some other unrecognized failure", "pr_create_failed"),
])
def test_run_fix_classifies_pr_creation_failures(monkeypatch, fake_repo, err_text, expected_reason):
    """A validated, ground-truth-green fix failing at the PR step is
    classified precisely (added 2026-08-31, see RESULTS.md /
    pr.py's classify_pr_create_error) instead of one generic
    pr_create_failed bucket that hid quota exhaustion and git-mechanics
    failures behind what looked like a forge-quality problem. Also
    regression coverage for exit_audit.EXIT_REASONS actually containing
    these — record_exit() raises ValueError on an unregistered reason,
    and this exact path had no test exercising it before."""
    import atomic_forge.fix as F
    from atomic_forge.exit_audit import read_exits
    _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)

    def _raise_pr(pd, *, upstream, title, body, base, dry_run=False):
        raise RuntimeError(err_text)
    monkeypatch.setattr(F, "raise_pr_via_fork", _raise_pr)

    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=False)
    assert r["success"] is False
    assert r["stage"] == "pr_create"
    exits = read_exits(fake_repo)
    assert exits[-1]["reason"] == expected_reason


def test_pr_body_embeds_the_actual_test_content():
    """A bare `test_rel` reference forces a reviewer to leave the PR
    description and dig through "Files changed" to see what's actually
    being asserted (user feedback, 2026-08-31: "the reference enough its
    not good enough") — the test source itself should be visible inline."""
    import atomic_forge.fix as F
    issue = {"owner": "o", "repo": "r", "number": 1, "url": "https://x/1",
             "title": "bug", "body": "desc"}
    report = {"rounds": 1, "initial_failures": 1, "final_failures": 0,
              "repaired_files": ["mod.py"]}
    body = F._pr_body(issue, report, "tests/test_forge_1.py",
                      "def test_add():\n    assert add(2, 3) == 5\n")
    assert "def test_add():" in body
    assert "assert add(2, 3) == 5" in body
    assert "```python" in body       # .py suffix -> python fence
    assert "<summary>" in body       # collapsed, doesn't dominate the description


def test_pr_body_truncates_a_very_large_test_and_says_so():
    import atomic_forge.fix as F
    issue = {"owner": "o", "repo": "r", "number": 1, "url": "https://x/1",
             "title": "bug", "body": "desc"}
    report = {"rounds": 1, "initial_failures": 1, "final_failures": 0, "repaired_files": []}
    huge = "x = 1\n" * 2000  # well over the 4000-char cap
    body = F._pr_body(issue, report, "tests/test_forge_1.py", huge)
    assert "truncated" in body.lower()
    assert len(body) < len(huge)  # didn't just dump the whole thing in


def test_pr_body_falls_back_to_reference_only_without_content():
    """No test_content (e.g. the file couldn't be read) -> no crash, no
    empty <details> block, just the existing reference line."""
    import atomic_forge.fix as F
    issue = {"owner": "o", "repo": "r", "number": 1, "url": "https://x/1",
             "title": "bug", "body": "desc"}
    report = {"rounds": 1, "initial_failures": 1, "final_failures": 0, "repaired_files": []}
    body = F._pr_body(issue, report, "tests/test_forge_1.py")
    assert "<details>" not in body
    assert "tests/test_forge_1.py" in body  # reference line still present


def test_run_fix_pr_body_includes_the_real_committed_test_file(monkeypatch, fake_repo):
    """End-to-end: the test file _stub_chain's fake generate_regression_test
    "wrote" (fix.py doesn't actually write it in this stubbed path — write
    it directly here, at the path run_fix will read from) ends up embedded
    in the body raise_pr_via_fork actually receives."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    captured = {}
    def _raise_pr(pd, *, upstream, title, body, base, dry_run=False):
        captured["body"] = body
        raised["called"] = True
        return {"pr_url": "https://github.com/up/repo/pull/1", "branch": "b", "base": base}
    monkeypatch.setattr(F, "raise_pr_via_fork", _raise_pr)

    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    tests_dir = fake_repo / "tests"; tests_dir.mkdir()
    (tests_dir / "test_forge_1.py").write_text(
        "def test_add_returns_five():\n    assert add(2, 3) == 5\n")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=False)
    assert r["success"] is True
    assert "def test_add_returns_five():" in captured["body"]


def test_run_fix_aborts_when_this_account_already_has_an_open_pr(monkeypatch, fake_repo):
    """Enforces campaign50_targets.json's own documented-but-never-enforced
    "max 1 open PR per repo at a time" rule. Real consequence found live
    (2026-08-31): 4 simultaneous astroid PRs from one batch violated it
    and the account was blocked from the pylint-dev org for it. Checked
    even under --dry-run, unlike the AI-policy warn-and-continue case —
    piling a second open PR onto a repo is the actual harm this guards
    against, not something a local preview needs to risk either."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    monkeypatch.setattr(F, "already_has_open_pr",
                        lambda upstream: "https://github.com/o/r/pull/1")
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    assert r["stage"] == "preflight"
    assert "already has an open PR" in r["reason"]
    assert raised["called"] is False


def test_run_fix_aborts_on_ai_contributions_policy(monkeypatch, fake_repo):
    """Preflight check added 2026-08-31 (see pr.py, RESULTS.md:
    Rapptz/discord.py#10507 was raised then closed citing exactly this) —
    a real, non-dry-run attempt against a repo with a written
    AI-contributions policy aborts before any LLM spend, not just at the
    PR-creation step."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    monkeypatch.setattr(F, "check_ai_policy",
                        lambda upstream: {"path": "CONTRIBUTING.md",
                                          "reason": "contains an AI-contributions "
                                                    "policy clause"})
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=False)
    assert r["success"] is False
    assert r["stage"] == "preflight"
    assert "CONTRIBUTING.md" in r["reason"]
    assert raised["called"] is False  # never even reached the repair loop


def test_run_fix_ai_policy_warns_but_continues_under_dry_run(monkeypatch, fake_repo):
    """--dry-run pushes nothing regardless, so an AI-contributions policy
    hit is a warning, not an abort — useful for local inspection."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    monkeypatch.setattr(F, "check_ai_policy",
                        lambda upstream: {"path": "CONTRIBUTING.md", "reason": "..."})
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is True
    assert r["stage"] == "dry_run"


def test_run_fix_aborts_on_maintainer_already_settled(monkeypatch, fake_repo):
    """dateutil/dateutil#1421 cost 158 LLM calls / ~2.9M tokens "fixing"
    behavior a maintainer had already confirmed as intended — this
    preflight check (added 2026-08-31) aborts before any of that spend,
    for both dry-run and real attempts (there's nothing useful to preview
    for a settled non-bug either way)."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    monkeypatch.setattr(F, "issue_already_settled",
                        lambda upstream, number:
                        "https://github.com/o/r/issues/1#issuecomment-1")
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=True)
    assert r["success"] is False
    assert r["stage"] == "preflight"
    assert "issuecomment-1" in r["reason"]
    assert raised["called"] is False


def test_run_fix_force_skips_both_preflight_checks(monkeypatch, fake_repo):
    """--force is the escape hatch for all three preflight checks at
    once — proceeds through the full pipeline even when every one of
    them would otherwise abort."""
    import atomic_forge.fix as F
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    monkeypatch.setattr(F, "check_ai_policy",
                        lambda upstream: {"path": "CONTRIBUTING.md", "reason": "..."})
    monkeypatch.setattr(F, "issue_already_settled",
                        lambda upstream, number: "https://example/settled")
    monkeypatch.setattr(F, "already_has_open_pr",
                        lambda upstream: "https://example/pull/1")
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, dry_run=False, force=True)
    assert r["success"] is True
    assert raised["called"] is True


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