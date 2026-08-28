from atomic_forge.agent import parse_action, run_agent
from atomic_forge.tools import LocalToolBackend
from atomic_forge.trajectory import Trajectory

from _helpers import ScriptedChatLLM


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

    def check(patch):
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

    def check(patch):
        attempts.append(patch)
        if "good" in (patch or ""):
            return True, ""
        return False, "not good enough"

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=check, max_turns=10)
    assert result.success
    assert len(attempts) == 2


def test_run_agent_aborts_on_turn_budget(tmp_path):
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = ScriptedChatLLM(["PATCH\n```python\nx\n```"])  # never submits

    def check(patch):
        return False, "never good enough"

    result = run_agent(llm, tools, tmp_path, "system", "task", traj, submit_check=check, max_turns=3)
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
