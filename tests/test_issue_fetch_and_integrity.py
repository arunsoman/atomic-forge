"""Tests for the F1 family of fix-pipeline reliability gates:

F1  --repro pre-flight probe: abort as issue_already_fixed when the probe
    passes on HEAD (stale issue); require it to flip to exit 0 after
    repair before a PR is raised (independent second witness);
F1b clone integrity: a clone that exits 0 with no resolvable HEAD is
    retried once and never returned (real case: sphinx's wrongenc.inc
    produced a zero-commit worktree that detonated at `git checkout -b`);
F1c fetch_issue channel fallback: GraphQL -> authenticated REST ->
    unauthenticated REST, so a zeroed GraphQL quota can't abort a run at
    the very first step (pilot: fresh-account GraphQL 0/0).

These are all deterministic — no network, no LLM, no CIE.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import atomic_forge.issue as issue_mod
from atomic_forge.fix import run_repro_probe
from atomic_forge.issue import _clone_head_ok
# ---------------------------------------------------------------- F1 probe
def _git_init(repo: Path):
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


def test_repro_probe_nonzero_exit_counts_as_bug_present(fake_repo):
    p = fake_repo / "repro.py"
    p.write_text("import sys\nsys.exit(3)\n")
    fails, code, out = run_repro_probe(fake_repo, "python", p)
    assert fails is True and code == 3


def test_repro_probe_zero_exit_means_already_fixed(fake_repo):
    p = fake_repo / "repro.py"
    p.write_text("print('no bug here')\n")
    fails, code, out = run_repro_probe(fake_repo, "python", p)
    assert fails is False and code == 0


def test_repro_probe_runs_py_under_venv_python(fake_repo):
    p = fake_repo / "repro.py"
    p.write_text("import sys\nprint(sys.executable)\nsys.exit(0)\n")
    fails, code, out = run_repro_probe(fake_repo, sys.executable, p)
    assert fails is False and code == 0
    assert out == sys.executable  # probe ran under the interpreter we supplied


def test_repro_probe_bash_script(fake_repo):
    p = fake_repo / "repro.sh"
    p.write_text("exit 7\n")
    fails, code, out = run_repro_probe(fake_repo, "python", p)
    assert fails is True and code == 7


def test_repro_probe_timeout_counts_as_bug_present(fake_repo, monkeypatch):
    p = fake_repo / "repro.py"
    p.write_text("import time; time.sleep(30)\n")
    fails, code, _ = run_repro_probe(fake_repo, "python", p, timeout=1)
    assert fails is True and code == -1


def test_run_fix_aborts_as_issue_already_fixed(monkeypatch, fake_repo):
    """The headline F1 behavior: probe passes on HEAD -> abort BEFORE any
    testgen/repair/PR (no LLM tokens spent, nothing raised)."""
    import atomic_forge.fix as F
    from test_fix import _DummyLLM, _stub_chain
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    repro = fake_repo / "repro.py"
    repro.write_text("import sys\nsys.exit(0)\n")  # "already fixed" on HEAD
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, repro=repro, dry_run=True)
    assert r["success"] is False
    assert r["stage"] == "repro"
    assert "already fixed" in r["reason"]
    assert r["repro_exit_code"] == 0
    assert raised["called"] is False  # never reached testgen/repair/PR


def test_run_fix_repro_confirmed_then_pr(monkeypatch, fake_repo):
    """Probe fails on HEAD (bug present) and flips to 0 after repair -> the
    run proceeds all the way to the (dry-run) PR, carrying both witnesses."""
    import atomic_forge.fix as F
    from test_fix import _DummyLLM, _stub_chain
    calls = {"n": 0}
    real = run_repro_probe
    def flaky_probe(pd, vp, rp, **kw):
        calls["n"] += 1
        return (True, 1, "bug reproduced") if calls["n"] == 1 else (False, 0, "fixed!")
    monkeypatch.setattr("atomic_forge.fix.run_repro_probe", flaky_probe)
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    repro = fake_repo / "repro.txt"  # any non-py path; actual script never runs
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib, repro=repro, dry_run=True)
    assert r["success"] is True
    assert r["stage"] == "dry_run"
    assert r.get("repro_fixed_after_repair") is True
    assert raised["called"] is True


def test_run_fix_blocks_pr_while_repro_still_fails(monkeypatch, fake_repo):
    """Generated test goes green but the independent probe still fails ->
    no PR (repro_still_failing), even though the pipeline 'fixed' the bug."""
    import atomic_forge.fix as F
    from test_fix import _DummyLLM, _stub_chain
    monkeypatch.setattr("atomic_forge.fix.run_repro_probe",
                        lambda pd, vp, rp, **kw: (True, 2, "still broken"))
    raised = _stub_chain(monkeypatch, oracle_fails=True, repair_success=True, green=True)
    ib = fake_repo / "issue.txt"; ib.write_text("add(2,3) should be 5 but returns -1")
    r = F.run_fix("https://github.com/o/r/issues/1", _DummyLLM(),
                  project_dir=fake_repo, issue_body_file=ib,
                  repro=fake_repo / "repro.txt", dry_run=True)
    assert r["success"] is False
    assert r["stage"] == "repair"
    assert "repro probe still exits non-zero" in r["reason"]
    assert r["repro_fixed_after_repair"] is False
    assert raised["called"] is False


# ------------------------------------------------------------- F1b clone
def _git_head(dest: Path):
    return subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                          capture_output=True, text=True).returncode == 0


def test_clone_head_ok_true_on_real_commit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "f.txt").write_text("hi\n")
    _git_init(repo)
    assert _clone_head_ok(repo) is True


def test_clone_head_ok_false_on_unborn_head(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    assert _clone_head_ok(repo) is False


def test_clone_repo_rejects_zero_commit_clone_result(monkeypatch, tmp_path):
    """A `git clone` that exits 0 but leaves an unborn HEAD must NOT be
    returned (the sphinx failure mode) — clone_repo retries once, then
    raises, and never hands a broken checkout to the pipeline."""
    import atomic_forge.issue as I
    real_run = subprocess.run
    def fake_clone_run(cmd, **kw):
        if cmd[:2] == ["git", "clone"]:
            dest = Path(cmd[-1])
            subprocess.run(["git", "init", "-q", str(dest)], capture_output=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")  # lies: "success"
        return real_run(cmd, **kw)
    monkeypatch.setattr(I.subprocess, "run", fake_clone_run)
    with pytest.raises(RuntimeError, match="no resolvable HEAD"):
        I.clone_repo("octocat", "hello-world", tmp_path / "hw")
    assert not (tmp_path / "hw" / ".git").exists() or _clone_head_ok(tmp_path / "hw") is False


# ------------------------------------------------------------- F1c fetch
class _Resp:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_fetch_issue_falls_back_to_ghapi_rest(monkeypatch):
    """GraphQL fetch fails (e.g. quota) -> gh api REST succeeds; the dict
    must carry gh's comment shape plus the state field."""
    issue = {"title": "T", "body": "B", "state": "open", "html_url": "https://x/7"}
    comments = [{"user": {"login": "bob"}, "body": "repro steps here"}]
    def fake_run(cmd, **kw):
        s = " ".join(cmd)
        if s.startswith("gh issue view"):
            return _Resp(1, "", "GraphQL: API rate limit already exceeded")
        if s.startswith("gh api") and "comments" in s:
            return _Resp(0, json.dumps(comments), "")
        if s.startswith("gh api"):
            return _Resp(0, json.dumps(issue), "")
        raise AssertionError(f"unexpected fetch command: {s}")
    monkeypatch.setattr(issue_mod.subprocess, "run", fake_run)
    d = issue_mod.fetch_issue("o", "r", 7)
    assert d["title"] == "T" and d["body"] == "B"
    assert d["state"] == "open" and d["url"].endswith("/7")
    assert d["comments"] == [{"author": {"login": "bob"}, "body": "repro steps here"}]


def test_fetch_issue_final_anon_channel(monkeypatch):
    """gh CLI entirely broken (invalid token) -> unauthenticated curl to the
    public REST API still fetches the issue (comments best-effort)."""
    issue = {"title": "T", "body": "B", "state": "open", "html_url": "https://x/8"}
    def fake_run(cmd, **kw):
        s = " ".join(cmd)
        if s.startswith("gh "):
            return _Resp(1, "", "HTTP 401: Requires authentication")
        if "curl" in s and "comments" in s:
            return _Resp(0, json.dumps([]), "")
        if "curl" in s:
            return _Resp(0, json.dumps(issue), "")
        raise AssertionError(s)
    monkeypatch.setattr(issue_mod.subprocess, "run", fake_run)
    d = issue_mod.fetch_issue("o", "r", 8)
    assert d["title"] == "T" and d["comments"] == []


def test_fetch_issue_aggregates_all_channel_errors(monkeypatch):
    def fake_run(cmd, **kw):
        s = " ".join(cmd)
        if s.startswith("gh "):
            return _Resp(1, "", "HTTP 401")
        return _Resp(1, "", "curl: (6) Could not resolve host")
    monkeypatch.setattr(issue_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="on any channel") as ei:
        issue_mod.fetch_issue("o", "r", 9)
    msg = str(ei.value)
    assert "HTTP 401" in msg and "Could not resolve host" in msg