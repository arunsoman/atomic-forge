"""R16b — environment-bootstrap gate tests.

Uses real `make`-fixture repos (no Docker, no network) so the probe path
itself is exercised end-to-end: detect_test_stack -> run command -> verdict
-> checkpoint row.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atomic_forge.bootstrap import run_bootstrap_gate
from atomic_forge.checkpoint import load_run


@pytest.fixture
def ck_db(tmp_path, monkeypatch):
    db = tmp_path / "ck" / "checkpoints.db"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(db))
    return db


def _make_repo(tmp_path: Path, target: str, recipe: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "Makefile").write_text(f"{target}:\n\t{recipe}\n")
    return repo


def test_unsupported_ecosystem_on_empty_dir(tmp_path, ck_db):
    report = run_bootstrap_gate(tmp_path / "empty", db_path=ck_db)
    assert report["ok"] is False
    assert report["verdict"] == "unsupported_ecosystem"
    rec = load_run(report["checkpoint_run_id"], db_path=ck_db)
    assert rec.phase == "bootstrap"
    assert rec.bootstrap_verdict == "unsupported_ecosystem"
    assert rec.status == "failed"


def test_bootstrapped_when_test_target_runs(tmp_path, ck_db):
    repo = _make_repo(tmp_path, "test", '@echo "ran 2 tests"')
    report = run_bootstrap_gate(repo, db_path=ck_db)
    assert report["ok"] is True
    assert report["verdict"] == "bootstrapped"
    assert "2 tests" in report["evidence"]
    rec = load_run(report["checkpoint_run_id"], db_path=ck_db)
    assert rec.status == "passed"
    assert rec.bootstrap_verdict == "bootstrapped"
    assert "make -j test" in rec.bootstrap_detail


def test_failed_deterministic_on_runner_crash(tmp_path, ck_db):
    # exit 2 = runner crash/misuse, NOT a completed test run — a failing
    # assertion would exit 1 and pass the gate; this is the difference
    # the whole gate exists to draw.
    repo = _make_repo(tmp_path, "test", "@echo 'cmake: command not found' >&2; exit 2")
    report = run_bootstrap_gate(repo, db_path=ck_db)
    assert report["ok"] is False
    assert report["verdict"] == "failed_deterministic"
    assert "exited 2" in report["detail"]
    rec = load_run(report["checkpoint_run_id"], db_path=ck_db)
    assert rec.bootstrap_verdict == "failed_deterministic"


def test_failed_deterministic_on_timeout(tmp_path, ck_db):
    repo = _make_repo(tmp_path, "test", "@sleep 5")
    report = run_bootstrap_gate(repo, timeout=2, db_path=ck_db)
    assert report["ok"] is False
    assert report["verdict"] == "failed_deterministic"


def test_passing_tests_count_as_bootstrapped(tmp_path, ck_db):
    # exit 0 with real pytest evidence — the happy path
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("")  # no deps -> falls to bare pytest
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    report = run_bootstrap_gate(repo, db_path=ck_db)
    assert report["ok"] is True
    rec = load_run(report["checkpoint_run_id"], db_path=ck_db)
    assert rec.bootstrap_detail  # evidence recorded on the checkpoint row


def test_checkpoint_record_survives_as_durable_state(tmp_path, ck_db):
    """The gate's whole point vs a print(): the verdict is queryable later."""
    repo = _make_repo(tmp_path, "test", "@exit 2")
    run_bootstrap_gate(repo, db_path=ck_db)
    from atomic_forge.checkpoint_store import list_latest_per_owner
    rows = list_latest_per_owner("forge_run", db_path=ck_db)
    assert len(rows) == 1
    data = rows[0].data
    assert data["phase"] == "bootstrap"
    assert data["bootstrap_verdict"] in ("failed_deterministic", "bootstrapped")