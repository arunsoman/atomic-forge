import json
from types import SimpleNamespace

from atomic_forge.llm import ChatTurn, ToolCall
from atomic_forge.testgen import generate_regression_test


class FakeBridge:
    def call(self, tool_name, **kwargs):
        return {"ok": True, "tool": tool_name}


class ExploreForeverUnlessNudgedLLM:
    """Simulates the failure mode confirmed live against sphinx#13180
    (rca_pilot_runs_1_3.md, finding F4): given no other signal, the model
    keeps calling exploration tools (search_symbol/view_file/file_skeleton)
    turn after turn and never calls write_file, exhausting the turn budget
    with nothing generated. Only responds with write_file once it sees the
    budget-aware nudge generate_regression_test injects with 2 turns left."""

    def __init__(self):
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        self.saw_nudge_on_turn = None

    def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192):
        nudged = any(m.get("role") == "user" and "Stop exploring" in (m.get("content") or "")
                     for m in messages)
        if nudged:
            return ChatTurn(content="", tool_calls=[ToolCall(
                id="w1", name="write_file",
                arguments=json.dumps({"path": "test_forge_gen.py",
                                      "content": "def test_x():\n    assert False\n"}))])
        return ChatTurn(content="", tool_calls=[ToolCall(
            id="s1", name="search_symbol", arguments=json.dumps({"name": "foo"}))])


def test_nudge_forces_a_write_before_budget_exhausts(tmp_path):
    """Regression test for rca_pilot_runs_1_3.md F4: a model that would
    otherwise explore forever must still produce a test file once the
    turn budget is nearly spent, instead of silently generating nothing
    (the sphinx#13180 outcome: 10 turns, 0 writes, 'no regression test
    generated')."""
    llm = ExploreForeverUnlessNudgedLLM()
    result = generate_regression_test(
        llm, FakeBridge(), tmp_path, "test_forge_gen.py",
        bug_description="something is broken", max_turns=10)

    assert (tmp_path / "test_forge_gen.py").exists()
    assert result["generated"].strip() != ""


class IgnoresFirstTwoNudgesLLM:
    """Simulates the failure mode confirmed live 2026-08-31
    (simonw/datasette#2805, simple-salesforce/simple-salesforce#758): the
    old single-nudge-at-2-turns-left mechanism fired exactly once, the
    model called view_file right through it anyway, and the actual final
    turn got no reminder at all. This fake explores through the first two
    nudges (3 and 2 turns remaining) and only writes on the third (1 turn
    remaining, the escalated "THIS IS THE LAST TURN" message) — proving
    the escalation across the whole tail, not a single warning, is what
    makes this reliable."""
    def __init__(self):
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        self.nudges_seen = 0

    def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192):
        last = messages[-1]
        nudged_this_turn = (last.get("role") == "user"
                            and "Stop exploring" in (last.get("content") or ""))
        if nudged_this_turn:
            self.nudges_seen += 1
        if nudged_this_turn and "THIS IS THE LAST TURN" in last["content"]:
            return ChatTurn(content="", tool_calls=[ToolCall(
                id="w1", name="write_file",
                arguments=json.dumps({"path": "test_forge_gen.py",
                                      "content": "def test_x():\n    assert False\n"}))])
        return ChatTurn(content="", tool_calls=[ToolCall(
            id="s1", name="view_file", arguments=json.dumps({"path": "mod.py"}))])


def test_escalating_nudges_survive_the_model_ignoring_the_first_ones(tmp_path):
    """Regression test for the live 2026-08-31 finding: a single nudge is
    not reliable enough — this model explicitly ignores the first two
    (3 and 2 turns remaining) and only complies with the third, most
    urgent one. Without the fix (nudge only at turns == max_turns - 1),
    this model would never see a second chance and the attempt would
    fail exactly like datasette#2805 and simple-salesforce#758 did."""
    llm = IgnoresFirstTwoNudgesLLM()
    result = generate_regression_test(
        llm, FakeBridge(), tmp_path, "test_forge_gen.py",
        bug_description="something is broken", max_turns=10)

    assert (tmp_path / "test_forge_gen.py").exists()
    assert result["generated"].strip() != ""
    assert llm.nudges_seen == 3  # saw all 3 escalating reminders, not just 1


def test_no_nudge_needed_if_model_writes_on_its_own(tmp_path):
    """The nudge must not interfere with a model that writes early —
    wrote_file short-circuits it (see the `not wrote_file` guard)."""
    class WritesImmediatelyLLM:
        def __init__(self):
            self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
            self.calls = 0

        def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192):
            self.calls += 1
            if self.calls == 1:
                return ChatTurn(content="", tool_calls=[ToolCall(
                    id="w1", name="write_file",
                    arguments=json.dumps({"path": "test_forge_gen.py",
                                          "content": "def test_x():\n    assert False\n"}))])
            return ChatTurn(content="done", tool_calls=[])

    result = generate_regression_test(
        WritesImmediatelyLLM(), FakeBridge(), tmp_path, "test_forge_gen.py",
        bug_description="something is broken", max_turns=10)

    assert (tmp_path / "test_forge_gen.py").exists()
    assert result["turns"] == 2
