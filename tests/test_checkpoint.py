from pathlib import Path

from atomic_forge.checkpoint import RunCheckpointer, diff_file_hashes, hash_files, load_run, load_run_history, new_run_id


def test_checkpoint_round_trips(tmp_path):
    db = tmp_path / "checkpoints.db"
    run_id = new_run_id()
    ckpt = RunCheckpointer(run_id=run_id, project="p", project_dir=str(tmp_path), db_path=db)
    ckpt.mark_phase("generate")
    ckpt.mark_written({"a.py": "hash1"})
    ckpt.mark_tested("task-a", "passed")
    ckpt.finish("passed")

    loaded = load_run(run_id, db_path=db)
    assert loaded is not None
    assert loaded.phase == "finished"
    assert loaded.status == "passed"
    assert loaded.file_hashes == {"a.py": "hash1"}
    assert loaded.tested_verdicts == {"task-a": "passed"}


def test_history_keeps_every_snapshot(tmp_path):
    db = tmp_path / "checkpoints.db"
    run_id = new_run_id()
    ckpt = RunCheckpointer(run_id=run_id, project="p", project_dir=str(tmp_path), db_path=db)
    ckpt.mark_phase("generate")
    ckpt.mark_phase("qa")
    ckpt.mark_phase("repair")
    history = load_run_history(run_id, db_path=db)
    assert [r.phase for r in history] == ["generate", "generate", "qa", "repair"]


def test_resume_carries_forward_and_resets_status(tmp_path):
    db = tmp_path / "checkpoints.db"
    run_id = new_run_id()
    ckpt = RunCheckpointer(run_id=run_id, project="p", project_dir=str(tmp_path), db_path=db)
    ckpt.mark_phase("generate", status="crashed")

    resumed_record = load_run(run_id, db_path=db)
    resumed = RunCheckpointer(run_id=run_id, project="p", project_dir=str(tmp_path),
                              record=resumed_record, db_path=db)
    assert resumed.record.status == "running"  # reset, not stuck at "crashed"
    assert resumed.record.phase == "generate"  # phase carried forward


def test_diff_file_hashes_detects_drift(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("original")
    hashes = hash_files(tmp_path, ["a.py"])

    diff = diff_file_hashes(tmp_path, hashes)
    assert diff.unchanged == ["a.py"]
    assert diff.changed == []

    f.write_text("changed on disk")
    diff2 = diff_file_hashes(tmp_path, hashes)
    assert diff2.changed == ["a.py"]


def test_diff_file_hashes_missing_file_counts_as_changed(tmp_path):
    diff = diff_file_hashes(tmp_path, {"missing.py": "deadbeef"})
    assert diff.changed == ["missing.py"]
