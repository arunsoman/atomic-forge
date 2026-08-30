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
