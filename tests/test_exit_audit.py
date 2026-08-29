"""Tests for the structured exit-audit trail (exit_audit.py) — the one-line-
per-run "why did this stop" record, separate from the full trajectory."""
from __future__ import annotations

import pytest

from atomic_forge.exit_audit import record_exit, read_exits


def test_record_exit_writes_jsonl(tmp_path):
    path = record_exit(tmp_path, reason="no_test_generated", detail="d",
                       extra={"issue": "https://x/y/issues/1"})
    assert path == tmp_path / ".forge" / "exit_audit.jsonl"
    exits = read_exits(tmp_path)
    assert len(exits) == 1
    assert exits[0]["reason"] == "no_test_generated"
    assert exits[0]["detail"] == "d"
    assert exits[0]["issue"] == "https://x/y/issues/1"
    assert "at" in exits[0]


def test_record_exit_appends_multiple(tmp_path):
    record_exit(tmp_path, reason="bootstrap_fail", detail="a")
    record_exit(tmp_path, reason="success", detail="b")
    exits = read_exits(tmp_path)
    assert [e["reason"] for e in exits] == ["bootstrap_fail", "success"]


def test_record_exit_rejects_unknown_reason(tmp_path):
    with pytest.raises(ValueError, match="unknown exit reason"):
        record_exit(tmp_path, reason="not_a_real_reason")


def test_read_exits_empty_when_never_written(tmp_path):
    assert read_exits(tmp_path) == []
