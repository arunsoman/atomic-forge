"""Full pipeline smoke test: generate -> qa -> repair, against a scripted
LLM and a real (but trivial) Python project — no network, no mocking of
forge's own internals. Proves the pieces actually compose, not just that
each one works in isolation."""
from pathlib import Path

from atomic_forge.generate_agent import generate_batch_agentic
from atomic_forge.models import AtomicTask, AtomicTaskBatch, TestTriad
from atomic_forge.qa import qa_phase
from atomic_forge.repair_agent import repair_loop_agentic
from atomic_forge.sandbox import ensure_repo
from atomic_forge.tools import LocalToolBackend
from atomic_forge.trajectory import Trajectory

from _helpers import ScriptedChatLLM

GOOD_SOURCE = "PATCH\n```python\ndef add(a, b):\n    return a + b\n```\nSUBMIT"
GOOD_TEST = (
    "PATCH\n```python\n"
    "from adder import add\n\n\n"
    "def test_add_positive():\n"
    "    assert add(2, 3) == 5\n\n\n"
    "def test_add_negative():\n"
    "    assert add(-1, 1) == 0\n"
    "```\nSUBMIT"
)


def test_generate_then_qa_then_repair_all_green(tmp_path):
    ensure_repo(tmp_path)
    task = AtomicTask(
        name="create adder", task_type="dev", action="create", file_path="adder.py",
        description="a two-argument add function",
        function_signatures=["def add(a, b)"],
        test_triad=TestTriad(positive="add(2,3)==5", negative="n/a", negative_to_positive="n/a"),
    )
    batch = AtomicTaskBatch(tasks=[task])
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)

    # A lone "create" task with no dependencies qualifies for the
    # batch-direct fast path (generate_agent._generate_batch_direct) —
    # ONE completion in the <<<FILE name>>>...<<<END>>> format, not the
    # per-task PATCH/SUBMIT agentic loop.
    gen_llm = ScriptedChatLLM([
        "<<<FILE create adder>>>\ndef add(a, b):\n    return a + b\n<<<END>>>",
    ])
    gen_result = generate_batch_agentic(tmp_path, batch, gen_llm, tools, traj)
    assert gen_result.ok
    assert (tmp_path / "adder.py").read_text().strip() == "def add(a, b):\n    return a + b"

    qa_llm = ScriptedChatLLM([
        "PATCH\n```python\n"
        "from adder import add\n\n\n"
        "def test_add_positive():\n"
        "    assert add(2, 3) == 5\n\n\n"
        "def test_add_negative():\n"
        "    assert add(-1, 1) == 0\n"
        "```",
        "SUBMIT",
    ])
    written = qa_phase(tmp_path, batch, qa_llm, tools, traj)
    assert len(written) == 1
    test_file = written[0]
    assert test_file.exists()
    assert "def test_add_positive" in test_file.read_text()

    # repair: the suite should already be green (no repair needed at all) —
    # exercises the "tests already passing" fast path.
    repair_llm = ScriptedChatLLM([])  # must never be called
    report = repair_loop_agentic(
        tmp_path, repair_llm, tools, traj,
        test_cmd="python -m pytest -q --continue-on-collection-errors",
        tasks_by_file={task.file_path: task.name},
    )
    assert report["success"]
    assert report["initial_failures"] == 0
    assert report["final_failures"] == 0
