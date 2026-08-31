"""Tests for the post-mortem learning engine (learning.py) — runs after a
repair_exhausted failure to study the trajectory for untried paths and
whether a new MCP/CIE tool would plausibly have changed the outcome."""
from __future__ import annotations

import json

from atomic_forge.learning import run_postmortem


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.messages_seen = None

    def chat(self, messages, **kw):
        self.messages_seen = messages
        return self.reply


def _write_trajectory(project_dir, records):
    d = project_dir / ".forge"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "trajectory.jsonl"
    with p.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


def test_run_postmortem_parses_well_formed_json(tmp_path):
    traj = _write_trajectory(tmp_path, [
        {"event": "test_run", "round": 0, "ok": False, "failures": 1},
        {"event": "round", "round": 1, "file": "mod.py", "ok": False},
    ])
    reply = json.dumps({
        "paths_tried": ["patched mod.py's off-by-one"],
        "untried_paths": ["never inspected the caller in helpers.py"],
        "new_tool_would_help": True,
        "proposed_tool": {"name": "trace_call_args", "purpose": "show real call-site args",
                          "why_it_would_have_changed_the_outcome": "would've surfaced the caller bug"},
    })
    llm = _FakeLLM(reply)
    rec = run_postmortem(llm, tmp_path, traj, bug_description="add() returns wrong value")
    assert rec["exit_reason"] == "repair_exhausted"
    assert rec["paths_tried"] == ["patched mod.py's off-by-one"]
    assert rec["new_tool_would_help"] is True
    assert rec["proposed_tool"]["name"] == "trace_call_args"
    # persisted to both the latest snapshot and the cross-run log
    assert (tmp_path / ".forge" / "learning.json").exists()
    assert (tmp_path / ".forge" / "learning.jsonl").exists()
    logged = json.loads((tmp_path / ".forge" / "learning.jsonl").read_text().strip())
    assert logged["exit_reason"] == "repair_exhausted"


def test_run_postmortem_tolerates_markdown_fence(tmp_path):
    traj = _write_trajectory(tmp_path, [{"event": "test_run", "round": 0, "ok": False}])
    reply = "```json\n" + json.dumps({
        "paths_tried": [], "untried_paths": [], "new_tool_would_help": False,
        "proposed_tool": None}) + "\n```"
    rec = run_postmortem(_FakeLLM(reply), tmp_path, traj, bug_description="x")
    assert rec["new_tool_would_help"] is False
    assert rec["proposed_tool"] is None


def test_run_postmortem_never_raises_on_malformed_reply(tmp_path):
    traj = _write_trajectory(tmp_path, [{"event": "test_run", "round": 0, "ok": False}])
    rec = run_postmortem(_FakeLLM("not json at all"), tmp_path, traj, bug_description="x")
    assert rec["paths_tried"] == []
    assert "parse_error" in rec
    assert (tmp_path / ".forge" / "learning.json").exists()


def test_run_postmortem_never_raises_when_llm_call_itself_fails(tmp_path):
    """`new_tool_would_help` must be None here, not False — found live
    (2026-08-31, simonw/sqlite-utils#841 and pypa/pip#14269): False used
    to read as a confident "the postmortem looked and found nothing"
    when what actually happened is the postmortem never ran at all.
    False is a specific, unearned claim when there was no real analysis
    to base it on; None (unknown) is honest either way."""
    traj = _write_trajectory(tmp_path, [{"event": "test_run", "round": 0, "ok": False}])
    class _Boom:
        def chat(self, *a, **kw): raise RuntimeError("rate limited")
    rec = run_postmortem(_Boom(), tmp_path, traj, bug_description="x")
    assert rec["new_tool_would_help"] is None
    assert rec["postmortem_failed"] is True
    assert "parse_error" in rec


def test_run_postmortem_empty_llm_reply_is_flagged_not_silently_false(tmp_path):
    """The other real failure shape seen live: llm.chat() returns
    successfully with EMPTY content (no exception at all) rather than
    raising - _parse_analysis("") then fails to json.loads(), landing in
    the same except branch. Must be flagged the same way as an outright
    LLM-call exception, not treated as a valid empty analysis."""
    traj = _write_trajectory(tmp_path, [{"event": "test_run", "round": 0, "ok": False}])
    rec = run_postmortem(_FakeLLM(""), tmp_path, traj, bug_description="x")
    assert rec["new_tool_would_help"] is None
    assert rec["postmortem_failed"] is True


def test_run_postmortem_handles_missing_trajectory(tmp_path):
    missing = tmp_path / ".forge" / "trajectory.jsonl"
    rec = run_postmortem(_FakeLLM(json.dumps(
        {"paths_tried": [], "untried_paths": [], "new_tool_would_help": False,
         "proposed_tool": None})), tmp_path, missing, bug_description="x")
    assert rec["paths_tried"] == []
