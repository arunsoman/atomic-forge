from atomic_forge.generate_agent import (
    _contract_check, _regression_check, generate_file_agentic,
)
from atomic_forge.models import AtomicTask, TestTriad
from atomic_forge.tools import LocalToolBackend
from atomic_forge.trajectory import Trajectory

from _helpers import ScriptedChatLLM


def _dev_task(**overrides):
    fields = dict(
        name="create foo", task_type="dev", action="create", file_path="foo.py",
        description="a foo function", function_signatures=["def foo() -> int"],
        test_triad=TestTriad(positive="p", negative="n", negative_to_positive="r"),
    )
    fields.update(overrides)
    return AtomicTask(**fields)


def test_contract_check_catches_missing_signature():
    task = _dev_task()
    ok, why = _contract_check(task, "def bar():\n    pass\n")
    assert not ok
    assert "foo" in why


def test_contract_check_passes_when_signature_present():
    task = _dev_task()
    ok, why = _contract_check(task, "def foo() -> int:\n    return 1\n")
    assert ok


def test_regression_check_flags_dropped_symbol():
    original = "def foo():\n    pass\n\n\ndef bar():\n    pass\n"
    new = "def foo():\n    pass\n"
    ok, why = _regression_check(original, new)
    assert not ok
    assert "bar" in why


def test_regression_check_passes_when_nothing_dropped():
    original = "def foo():\n    pass\n"
    new = "def foo():\n    return 1\n"
    ok, _ = _regression_check(original, new)
    assert ok


def test_generate_file_agentic_writes_new_file(tmp_path):
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    task = _dev_task()
    llm = ScriptedChatLLM([
        "PATCH\n```python\ndef foo() -> int:\n    return 1\n```",
        "SUBMIT",
    ])
    target = generate_file_agentic(tmp_path, task, llm, tools, traj)
    assert target.exists()
    assert "def foo" in target.read_text()


def test_generate_file_agentic_delete_action(tmp_path):
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    (tmp_path / "gone.py").write_text("x = 1\n")
    task = AtomicTask(name="remove gone", task_type="dev", action="delete", file_path="gone.py",
                      description="remove it")
    llm = ScriptedChatLLM([])  # delete tasks never call the LLM
    target = generate_file_agentic(tmp_path, task, llm, tools, traj)
    assert not target.exists()


def test_generate_file_agentic_raises_when_contract_never_satisfied(tmp_path):
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    task = _dev_task()
    # Never emits a valid `def foo` — every submit gets rejected until the
    # turn budget runs out.
    llm = ScriptedChatLLM(["PATCH\n```python\ndef wrong_name():\n    pass\n```", "SUBMIT"] * 5)
    import pytest
    with pytest.raises(RuntimeError):
        generate_file_agentic(tmp_path, task, llm, tools, traj, max_turns=6)
