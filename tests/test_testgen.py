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
