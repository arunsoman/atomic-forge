"""Tests for the PR-raising capability (pr.py). Uses dry_run + a throwaway
local git repo — never touches the network or a real GitHub repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atomic_forge.pr import (already_has_open_pr, classify_pr_create_error,
                             default_branch, default_branch_for, ensure_fork,
                             forge_footer, prepare_pr_branch, raise_pr,
                             summarize_repair_for_pr)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture
def tmp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], repo)
    _git(["config", "user.email", "forge@test"], repo)
    _git(["config", "user.name", "Forge Test"], repo)
    (repo / "mod.py").write_text("x = 1\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    return repo


def test_prepare_pr_branch_creates_feature_branch(tmp_repo):
    before = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=str(tmp_repo), capture_output=True, text=True).stdout.strip()
    assert before == "main"
    name = prepare_pr_branch(tmp_repo, "forge/fix-xyz")
    assert name == "forge/fix-xyz"
    after = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=str(tmp_repo), capture_output=True, text=True).stdout.strip()
    assert after == "forge/fix-xyz"
    # the default branch was not moved
    branches = subprocess.run(["git", "branch", "--list"], cwd=str(tmp_repo),
                               capture_output=True, text=True).stdout
    assert "main" in branches and "forge/fix-xyz" in branches


def test_prepare_pr_branch_default_name_has_prefix(tmp_repo):
    name = prepare_pr_branch(tmp_repo)
    assert name.startswith("forge/fix-")


def test_raise_pr_dry_run_does_not_push(tmp_repo):
    prepare_pr_branch(tmp_repo, "forge/fix-abc")
    (tmp_repo / "mod.py").write_text("x = 2\n")
    _git(["add", "."], tmp_repo)
    _git(["commit", "-m", "fix"], tmp_repo)
    out = raise_pr(tmp_repo, title="t", body="b", base="main", dry_run=True)
    assert out["dry_run"] is True
    assert out["branch"] == "forge/fix-abc"
    assert out["base"] == "main"
    assert out["title"] == "t"
    # nothing was pushed: no remote tracking ref exists
    rr = subprocess.run(["git", "rev-parse", "--abbrev-ref", "origin/forge/fix-abc"],
                        cwd=str(tmp_repo), capture_output=True, text=True)
    assert rr.returncode != 0  # no remote ref -> dry run did not push


def test_raise_pr_refuses_default_branch_onto_itself(tmp_repo):
    with pytest.raises(RuntimeError, match="default branch"):
        raise_pr(tmp_repo, title="t", body="b", base="main", dry_run=True)


def test_default_branch_falls_back_to_main(tmp_repo):
    assert default_branch(tmp_repo) == "main"


def test_summarize_repair_for_pr_shapes():
    title, body = summarize_repair_for_pr(
        {"success": True, "rounds": 1, "initial_failures": 1, "final_failures": 0,
         "repaired_files": ["mod.py"]}, "bits_offbyone")
    assert "bits_offbyone" in title
    assert "mod.py" in body


# -------------------------------------------------------- forge branding --
def test_forge_footer_has_marker_badge_and_link():
    footer = forge_footer()
    assert "<!-- atomic-forge:pr -->" in footer  # the tracker marker
    assert "kannamma-labs/atomic-forge" in footer
    assert "Fixed by" in footer
    assert "star" in footer  # the star-boost call to action


def test_summarize_repair_for_pr_includes_forge_footer():
    _, body = summarize_repair_for_pr(
        {"success": True, "rounds": 1, "initial_failures": 1, "final_failures": 0,
         "repaired_files": ["mod.py"]}, "x")
    assert "<!-- atomic-forge:pr -->" in body


# --------------------------------------------- gh pr create error triage --
@pytest.mark.parametrize("err,expected", [
    ("You must have the correct permissions to execute `CreatePullRequest`", "lockdown"),
    ("GraphQL: No commits between psf:main and arunsoman:forge/fix-issue-5214 "
     "(createPullRequest)", "no_commits"),
    ("GraphQL: Head sha was not found (createPullRequest)", "race"),
    ("GraphQL: Could not resolve to a Repository (field: headRepository)", "race"),
    ("GraphQL: Fork collab check pending (createPullRequest)", "race"),
    ("something totally unrelated blew up", "other"),
])
def test_classify_pr_create_error(err, expected):
    assert classify_pr_create_error(err) == expected


def test_classify_pr_create_error_is_case_insensitive():
    # the real captured gh error uses lowercase "createPullRequest" as the
    # GraphQL mutation-name annotation — this used to never match a
    # capital-C-only substring check, so the race path never retried.
    assert classify_pr_create_error(
        "graphql: head sha was not found (createpullrequest)") == "race"


# --------------------------------------------------- default branch fallback
def test_default_branch_for_uses_gh_when_it_resolves(monkeypatch):
    def _fake_run(cmd, **kw):
        class R: returncode = 0; stdout = "main\n"; stderr = ""
        return R()
    monkeypatch.setattr("atomic_forge.pr.subprocess.run", _fake_run)
    assert default_branch_for("o/r") == "main"


def test_default_branch_for_falls_back_to_master_when_gh_fails(monkeypatch, tmp_path):
    calls = []
    def _fake_run(cmd, **kw):
        calls.append(cmd)
        class R: pass
        r = R()
        if cmd[:2] == ["gh", "repo"]:
            r.returncode, r.stdout, r.stderr = 1, "", "gh: rate limited"
        elif cmd[:2] == ["git", "ls-remote"] and cmd[-1] == "main":
            r.returncode, r.stdout, r.stderr = 2, "", ""  # main doesn't exist
        elif cmd[:2] == ["git", "ls-remote"] and cmd[-1] == "master":
            r.returncode, r.stdout, r.stderr = 0, "abc123\trefs/heads/master\n", ""
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return r
    monkeypatch.setattr("atomic_forge.pr.subprocess.run", _fake_run)
    branch = default_branch_for("o/r", project_dir=tmp_path)
    assert branch == "master"
    # the fallback (not the authoritative gh path) is worth an audit note
    from atomic_forge.exit_audit import read_exits
    exits = read_exits(tmp_path)
    assert len(exits) == 1
    assert exits[0]["reason"] == "ambiguous_branch_defaulted"
    assert exits[0]["branch"] == "master"


def test_default_branch_for_raises_when_nothing_resolves(monkeypatch):
    def _fake_run(cmd, **kw):
        class R: returncode = 1; stdout = ""; stderr = "boom"
        return R()
    monkeypatch.setattr("atomic_forge.pr.subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="could not resolve"):
        default_branch_for("o/r")


def test_ensure_fork_raises_clearly_when_gh_repo_fork_fails(tmp_path, monkeypatch):
    """Confirmed live (astroid#3199, 2026-08-30): a brand-new GitHub account
    hit `gh repo fork`'s HTTP 403 abuse throttle ("You cannot fork this
    repository at this time"), account-wide, not repo-specific. The old
    code discarded `gh repo fork`'s result entirely, so this failure was
    silently swallowed — the caller went on to `git push` against a fork
    that was never created and got a confusing "repository not found"
    several steps later instead of a clear failure at the actual point of
    failure. ensure_fork must raise immediately, with the real gh output."""
    def _fake_run(cmd, **kw):
        class R: pass
        r = R()
        if cmd[:3] == ["gh", "repo", "fork"]:
            r.returncode, r.stdout, r.stderr = 1, "", (
                "failed to fork: HTTP 403: You cannot fork this repository "
                "at this time (https://api.github.com/repos/o/r/forks)")
        elif cmd == ["gh", "api", "user", "-q", ".login"]:
            r.returncode, r.stdout, r.stderr = 0, "someuser\n", ""
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return r
    monkeypatch.setattr("atomic_forge.pr.subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="cannot fork this repository"):
        ensure_fork(tmp_path, "o/r")


def test_ensure_fork_succeeds_when_fork_already_exists(tmp_repo, monkeypatch):
    """`gh repo fork` on an already-forked repo exits 0 with an
    "already exists" message (confirmed live) — this is success, not a
    failure to special-case; ensure_fork must proceed and set up the
    local `fork` remote normally."""
    real_run = subprocess.run

    def _fake_run(cmd, **kw):
        if cmd[:1] == ["git"]:
            return real_run(cmd, **kw)  # ensure_fork's own _git() calls — run for real
        class R: pass
        r = R()
        if cmd[:3] == ["gh", "repo", "fork"]:
            r.returncode, r.stdout, r.stderr = 0, "someuser/r already exists\n", ""
        elif cmd == ["gh", "api", "user", "-q", ".login"]:
            r.returncode, r.stdout, r.stderr = 0, "someuser\n", ""
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return r
    monkeypatch.setattr("atomic_forge.pr.subprocess.run", _fake_run)
    owner, fork_url = ensure_fork(tmp_repo, "o/r")
    assert owner == "someuser"
    assert fork_url == "https://github.com/someuser/r.git"
    remote = subprocess.run(["git", "remote", "get-url", "fork"], cwd=str(tmp_repo),
                            capture_output=True, text=True, check=True).stdout.strip()
    assert remote == fork_url

def test_already_has_open_pr_finds_the_existing_one(monkeypatch):
    """Enforces a rule that existed only on paper before this (see
    already_has_open_pr's docstring): 4 simultaneous astroid PRs from a
    single batch violated it and the account was blocked from the
    pylint-dev org for it — this makes "max 1 open PR per repo" a real
    precondition instead of something a human has to remember."""
    import atomic_forge.pr as PR

    def _fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
        r = R()
        if cmd[:3] == ["gh", "api", "user"]:
            r.stdout = "someuser\n"
        elif "search/issues" in cmd[-1]:
            r.stdout = ('{"items": [{"html_url": '
                        '"https://github.com/o/r/pull/42"}]}')
        return r
    monkeypatch.setattr(PR.subprocess, "run", _fake_run)
    assert already_has_open_pr("o/r") == "https://github.com/o/r/pull/42"


def test_already_has_open_pr_none_when_clear(monkeypatch):
    import atomic_forge.pr as PR

    def _fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
        r = R()
        if cmd[:3] == ["gh", "api", "user"]:
            r.stdout = "someuser\n"
        elif "search/issues" in cmd[-1]:
            r.stdout = '{"items": []}'
        return r
    monkeypatch.setattr(PR.subprocess, "run", _fake_run)
    assert already_has_open_pr("o/r") is None
