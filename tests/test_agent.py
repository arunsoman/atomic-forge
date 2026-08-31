import json

from atomic_forge.agent import parse_action, run_agent
from atomic_forge.llm import ChatTurn, ToolCall
from atomic_forge.tools import LocalToolBackend
from atomic_forge.trajectory import Trajectory

from _helpers import ScriptedChatLLM, ScriptedToolCallLLM


def test_parse_action_variants():
    assert parse_action("SUBMIT") == ("submit", "")
    assert parse_action("PATCH\nsome content")[0] == "patch"
    kind, payload = parse_action('TOOL view_file {"path": "a.py"}')
    assert kind == "tool"
    assert parse_action("RUN pytest -q") == ("run", "pytest -q")
    assert parse_action("nothing recognizable")[0] == "unknown"


def test_run_agent_success_on_first_patch(tmp_path):
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM([
        "PATCH\n```python\nprint('hi')\n```",
        "SUBMIT",
    ])

    holder = {}

    def check(patch, path=None):
        holder["patch"] = patch
        return True, ""

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=check, max_turns=5)
    assert result.success
    assert "print" in result.final_patch


def test_run_agent_retries_after_submit_rejection(tmp_path):
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM([
        "PATCH\n```python\nbad\n```",
        "SUBMIT",
        "PATCH\n```python\ngood\n```",
        "SUBMIT",
    ])

    attempts = []

    def check(patch, path=None):
        attempts.append(patch)
        if "good" in (patch or ""):
            return True, ""
        return False, "not good enough"

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=check, max_turns=10)
    assert result.success
    assert len(attempts) == 2


def test_run_agent_aborts_early_after_repeated_rejected_submits(tmp_path):
    """Regression test for a real cost finding (2026-08-29 benchmark run):
    a session whose SUBMIT keeps getting rejected but never
    aborts runs to the full turn budget anyway, dominating that run's
    token cost. 3 rejected SUBMITs (never an identical patch twice, so
    the "same action 5x" stuck detector never fires) must stop the
    session well short of a generous max_turns."""
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM([
        "PATCH\n```python\nattempt 1\n```", "SUBMIT",
        "PATCH\n```python\nattempt 2\n```", "SUBMIT",
        "PATCH\n```python\nattempt 3\n```", "SUBMIT",
        # Never reached if early-abort works — max_turns=20 would happily
        # run this far if nothing stopped it sooner.
        "PATCH\n```python\nattempt 4\n```", "SUBMIT",
    ])

    def check(patch, path=None):
        return False, "still wrong"  # never accepts

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=check, max_turns=20)
    assert not result.success
    assert "rejected SUBMIT" in result.abort_reason
    assert result.turns < 20  # stopped well short of the generous budget


def test_run_agent_aborts_on_turn_budget(tmp_path):
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM(["PATCH\n```python\nx\n```"])  # never submits

    def check(patch, path=None):
        return False, "never good enough"

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=check, max_turns=3)
    assert not result.success
    assert "turn budget" in result.abort_reason


def test_run_agent_forces_patch_with_two_turns_remaining(tmp_path):
    """A soft NOTE folded into tool observations at turns_since_patch==6/12
    wasn't enough to stop a session from exploring right up to the turn
    cap with no patch ever recorded — confirmed live on repair_agent's
    astroid-769 campaign run (2026-08-30): ~145 repair turns across two
    rounds, most view_file/search_symbol, most samples died with zero
    patches produced. Mirrors testgen's own proven F4b fix (see git log
    a31d73b): at 2 turns remaining with still no patch, inject an
    unambiguous forcing directive rather than just another soft nudge."""
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM([
        "RUN echo one",
        "RUN echo two",
        "RUN echo three",
        "RUN echo four",  # still no patch — this is the turn the force must fire on
        "PATCH\n```python\nx\n```",
        "SUBMIT",
    ])

    result = run_agent(llm, tools, tmp_path, "system", "task", traj,
                       submit_check=lambda patch, path=None: (True, ""), max_turns=6)
    assert result.success
    forced = [m["content"] for m in result.messages
              if m.get("role") == "user" and "no PATCH has been recorded yet" in m.get("content", "")]
    assert forced, "expected the hard-forcing directive at 2 turns remaining"


class IgnoresNudgeToolCallLLM:
    """Function-calling-path counterpart of testgen's proven
    IgnoresTheNudgeLLM (round-3 RCA: sphinx#14656, sphinx#14625,
    urllib3#5164, confirmed live against glm-5.2:cloud): a model that
    reads the soft "NOTE: only N turn(s) left"/"2 turn(s) remain" nudges
    folded into tool observations and keeps calling read-only tools
    anyway, right through the true final turn — the text nudge alone is a
    request, not a guarantee. Only complies once tool_choice forces
    `patch` specifically."""

    def __init__(self):
        self.tool_choices_seen: list = []

    def chat_with_tools(self, messages, tools, temperature=0.0, tool_choice="auto"):
        self.tool_choices_seen.append(tool_choice)
        if isinstance(tool_choice, dict) and tool_choice["function"]["name"] == "patch":
            return ChatTurn(content="", tool_calls=[ToolCall(
                id="p1", name="patch",
                arguments=json.dumps({"content": "```python\nx = 1\n```"}))])
        return ChatTurn(content="", tool_calls=[ToolCall(
            id="v1", name="view_file", arguments=json.dumps({"path": "a.py"}))])


def test_run_agent_fc_forces_patch_tool_choice_on_final_turn_when_nudge_is_ignored(tmp_path):
    """The function-calling path had the exact same weakness testgen.py's
    write_file forcing fix addressed: soft "NOTE: only N turn(s) left"
    nudges folded into tool observations are a request the model is free
    to ignore. A model that ignores them entirely (0 patches, right
    through the true final turn) must still get a patch attempt forced on
    that final turn via tool_choice, structurally unable to call anything
    else — and the deterministic auto-submit fallback must then still
    turn that into a successful, validated result even though the model
    never explicitly called `submit` itself."""
    llm = IgnoresNudgeToolCallLLM()
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    manifest = [{"name": "view_file", "signature": "(path)", "doc": "view a file"}]

    result = run_agent(llm, tools, tmp_path, "system", "task", traj,
                       submit_check=lambda patch, path=None: (True, ""),
                       max_turns=5, tool_manifest_text="", tool_manifest=manifest)

    assert result.success
    assert "x = 1" in result.final_patch
    # first 4 turns unforced (auto), only the true final turn is forced
    assert llm.tool_choices_seen[:4] == ["auto"] * 4
    assert llm.tool_choices_seen[4] == {"type": "function", "function": {"name": "patch"}}


class IgnoresForcedToolChoiceEntirelyLLM:
    """Round-3 RCA (celery/celery#10102, confirmed live 2026-08-31 via a
    direct API probe against Ollama Cloud's glm-5.2:cloud): forcing
    tool_choice to a specific function is NOT honored by every provider —
    the live probe called a DIFFERENT tool anyway, and tool_choice=
    "required" with only one tool offered returned ZERO tool calls,
    finish_reason "stop". This mock reproduces that: even the forced
    tool_choice from the previous fix is ignored, keeps calling view_file
    (or, to mirror the "required" case exactly, could return no tool_calls
    at all — either way `last_patch` stays None after the loop). Only the
    plain chat() fallback (no tools at all) succeeds."""

    def __init__(self):
        self.chat_called_with = None

    def chat_with_tools(self, messages, tools, temperature=0.0, tool_choice="auto"):
        return ChatTurn(content="", tool_calls=[ToolCall(
            id="v1", name="view_file", arguments=json.dumps({"path": "a.py"}))])

    def chat(self, messages, temperature=0.0, max_tokens=8192):
        self.chat_called_with = messages
        return ("Here is my fix:\nPATCH\n<<<<<<< SEARCH\nx = 0\n=======\n"
                "x = 1\n>>>>>>> REPLACE\n")


def test_run_agent_plain_text_fallback_when_forced_tool_choice_also_ignored(tmp_path):
    """Even the forced tool_choice from the previous fix can't be relied
    on against every backend — confirmed live. The true last resort is a
    plain chat() call (no tools at all) asking for the same PATCH/SEARCH-
    REPLACE text format the non-fc path already understands, parsed via
    the same `parse_action` — and that plain-text ask reliably produced
    exactly the requested content on every live probe, where a tool call
    did not."""
    llm = IgnoresForcedToolChoiceEntirelyLLM()
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    manifest = [{"name": "view_file", "signature": "(path)", "doc": "view a file"}]

    result = run_agent(llm, tools, tmp_path, "system", "task", traj,
                       submit_check=lambda patch, path=None: (True, ""),
                       max_turns=3, tool_manifest_text="", tool_manifest=manifest)

    assert result.success
    assert "SEARCH" in result.final_patch
    assert llm.chat_called_with is not None  # the fallback path actually ran


def test_run_agent_auto_submits_a_recorded_patch_when_budget_runs_out(tmp_path):
    """Forcing tool_choice on the final turn only guarantees a patch
    ATTEMPT gets made — it doesn't guarantee the model then also calls
    `submit` in that same forced turn (most tool_choice-forced APIs allow
    exactly the one forced call). Before this fix, a patch recorded on
    ANY turn that was never explicitly submitted before the budget ran
    out was silently discarded — `_attempt_patch` (repair_agent.py) and
    `generate_file_agentic` (generate_agent.py) both require
    `result.success`, so a perfectly good last_patch produced nothing.
    The deterministic auto-submit fallback must give it one last
    submit_check pass instead of throwing it away."""
    llm = ScriptedToolCallLLM([
        [("patch", {"content": "```python\ngood_fix = 1\n```"})],
        [("view_file", {"path": "a.py"})],
        [("view_file", {"path": "a.py"})],
    ])
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    manifest = [{"name": "view_file", "signature": "(path)", "doc": "view a file"}]

    result = run_agent(llm, tools, tmp_path, "system", "task", traj,
                       submit_check=lambda patch, path=None: (True, ""),
                       max_turns=3, tool_manifest_text="", tool_manifest=manifest)

    assert result.success
    assert "good_fix" in result.final_patch


def test_run_agent_auto_submit_fallback_still_rejects_a_bad_patch(tmp_path):
    """The auto-submit fallback must run through the real submit_check
    gate, not bypass it — a never-submitted patch that submit_check would
    genuinely reject must still fail the whole attempt, exactly as an
    explicit rejected SUBMIT would."""
    llm = ScriptedToolCallLLM([
        [("patch", {"content": "```python\nbad_fix = 1\n```"})],
        [("view_file", {"path": "a.py"})],
    ])
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    manifest = [{"name": "view_file", "signature": "(path)", "doc": "view a file"}]

    result = run_agent(llm, tools, tmp_path, "system", "task", traj,
                       submit_check=lambda patch, path=None: (False, "never good enough"),
                       max_turns=2, tool_manifest_text="", tool_manifest=manifest)

    assert not result.success
    assert "turn budget" in result.abort_reason


def test_run_agent_aborts_on_repeated_invalid_action(tmp_path):
    """An invalid action is also an "ERROR:" observation, so 4 in a row
    trips the consecutive-error abort before the (5x) stuck-detection
    threshold is even reached."""
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM(["not a valid action"])

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=None, max_turns=10)
    assert not result.success
    assert "consecutive" in result.abort_reason


def test_run_agent_stuck_detection(tmp_path):
    """A valid but never-accepted TOOL call repeated 5x trips stuck
    detection specifically (not the consecutive-error abort, since a
    successful tool call is never an "ERROR:" observation)."""
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM(['TOOL view_file {"path": "nope.py"}'])

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=None, max_turns=10)
    assert not result.success
    assert "stuck" in result.abort_reason
