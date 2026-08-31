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

    def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192,
                        tool_choice="auto"):
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

        def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192,
                            tool_choice="auto"):
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


class IgnoresTheNudgeLLM:
    """Round-3 RCA (sphinx#14656, sphinx#14625, urllib3#5164, confirmed
    live against glm-5.2:cloud): a model that reads the turn-9 nudge and
    keeps exploring anyway, right through the final turn too — the text
    nudge alone doesn't guarantee anything. Only complies once tool_choice
    forces write_file specifically."""

    def __init__(self):
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        self.tool_choices_seen = []

    def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192,
                        tool_choice="auto"):
        self.tool_choices_seen.append(tool_choice)
        if isinstance(tool_choice, dict):
            name = tool_choice["function"]["name"]
            return ChatTurn(content="", tool_calls=[ToolCall(
                id="w1", name=name,
                arguments=json.dumps({"path": "test_forge_gen.py",
                                      "content": "def test_x():\n    assert False\n"}))])
        return ChatTurn(content="", tool_calls=[ToolCall(
            id="s1", name="search_symbol", arguments=json.dumps({"name": "foo"}))])


def test_forced_tool_choice_on_final_turn_when_nudge_is_ignored(tmp_path):
    """A model that ignores the soft nudge entirely (the actual sphinx/
    urllib3 failure mode — 10 turns, 0 writes) must still produce a test:
    the last turn forces tool_choice to write_file specifically, so the
    model has no way to keep exploring instead."""
    llm = IgnoresTheNudgeLLM()
    result = generate_regression_test(
        llm, FakeBridge(), tmp_path, "test_forge_gen.py",
        bug_description="something is broken", max_turns=10)

    assert (tmp_path / "test_forge_gen.py").exists()
    assert result["generated"].strip() != ""
    # first 9 turns unforced, only the last is forced
    assert llm.tool_choices_seen[:9] == ["auto"] * 9
    assert llm.tool_choices_seen[9] == {"type": "function", "function": {"name": "write_file"}}


class IgnoresForcedToolChoiceLLM:
    """Round-3 RCA (celery/celery#10102, confirmed live against Ollama
    Cloud's glm-5.2:cloud): a forced tool_choice is not honored by every
    provider — the live probe returned a `view_file` call anyway, and with
    tool_choice="required" and only write_file in the tools list, it
    returned ZERO tool calls (finish_reason "stop"). This double emulates
    that: forced tool_choice is ignored just like "auto" would be."""

    def __init__(self):
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        self.chat_called_with = None

    def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192,
                        tool_choice="auto"):
        return ChatTurn(content="", tool_calls=[ToolCall(
            id="s1", name="search_symbol", arguments=json.dumps({"name": "foo"}))])

    def chat(self, messages, temperature=0.0, max_tokens=8192):
        self.chat_called_with = messages
        return "def test_reproduces_bug():\n    assert False\n"


def test_plain_text_fallback_when_forced_tool_choice_is_also_ignored(tmp_path):
    """Even the forced tool_choice from the previous fix can't be relied
    on — confirmed live. The true last resort is a plain chat() call (no
    tools at all), which the model can't decline to answer in prose the
    same way, and which produced valid content on every live probe."""
    llm = IgnoresForcedToolChoiceLLM()
    result = generate_regression_test(
        llm, FakeBridge(), tmp_path, "test_forge_gen.py",
        bug_description="something is broken", max_turns=3)

    assert (tmp_path / "test_forge_gen.py").exists()
    assert "assert False" in result["generated"]
    assert llm.chat_called_with is not None  # the fallback path actually ran


def test_plain_text_fallback_rejects_non_python_reply(tmp_path):
    """A fallback reply that isn't valid Python must be discarded, not
    written as a broken 'generated' test — better to correctly report
    no_test_generated than to write garbage that fails for a confusing,
    unrelated reason three steps later."""
    class RepliesWithProseLLM(IgnoresForcedToolChoiceLLM):
        def chat(self, messages, temperature=0.0, max_tokens=8192):
            self.chat_called_with = messages
            return "I'll start by looking at the relevant files."

    llm = RepliesWithProseLLM()
    result = generate_regression_test(
        llm, FakeBridge(), tmp_path, "test_forge_gen.py",
        bug_description="something is broken", max_turns=3)

    assert not (tmp_path / "test_forge_gen.py").exists()
    assert result["generated"] == ""
