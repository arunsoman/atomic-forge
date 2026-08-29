"""Tests for the PR-raising capability (pr.py). Uses dry_run + a throwaway
local git repo — never touches the network or a real GitHub repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atomic_forge.pr import default_branch, prepare_pr_branch, raise_pr, summarize_repair_for_pr


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
    assert "1 -> 0" in body