import json

from atomic_forge.decompose import decompose_spec, write_draft_json

from _helpers import ScriptedChatLLM

VALID_TASK = {
    "name": "create slugify util",
    "task_type": "dev",
    "action": "create",
    "file_path": "string_utils.py",
    "layer": "Backend",
    "description": "A slugify(text) function.",
    "exact_imports": [],
    "function_signatures": ["def slugify(text: str) -> str"],
    "step_by_step_implementation": ["lowercase", "strip punctuation", "hyphenate"],
    "dependencies": [],
    "test_triad": {
        "positive": "slugify('Hello, World!') == 'hello-world'",
        "negative": "slugify(None) raises TypeError",
        "negative_to_positive": "slugify('Hello') after that still returns 'hello'",
    },
}

INVALID_TASK_MISSING_TRIAD = {
    "name": "create counter",
    "task_type": "dev",
    "action": "create",
    "file_path": "counter.py",
    "layer": "Backend",
    "description": "A counter class.",
    "exact_imports": [],
    "function_signatures": ["class Counter"],
    "step_by_step_implementation": [],
    "dependencies": [],
    # no test_triad -> AtomicTask's own contract validator must reject this
}


def test_decompose_valid_output_parses_into_real_atomic_tasks():
    llm = ScriptedChatLLM([json.dumps([VALID_TASK])])
    result = decompose_spec("Add a slugify util.", llm)
    assert len(result.tasks) == 1
    assert not result.rejected
    task = result.tasks[0]
    assert task.file_path == "string_utils.py"
    assert task.test_triad.positive == VALID_TASK["test_triad"]["positive"]
    # It's a real AtomicTask, contract-enforced, not a loose dict:
    assert task.__class__.__name__ == "AtomicTask"


def test_decompose_fenced_json_output_also_parses():
    fenced = "Here you go:\n```json\n" + json.dumps([VALID_TASK]) + "\n```\n"
    llm = ScriptedChatLLM([fenced])
    result = decompose_spec("Add a slugify util.", llm)
    assert len(result.tasks) == 1


def test_decompose_contract_violation_is_rejected_not_silently_dropped():
    llm = ScriptedChatLLM([json.dumps([VALID_TASK, INVALID_TASK_MISSING_TRIAD])])
    result = decompose_spec("Add a slugify util and a counter.", llm)
    assert len(result.tasks) == 1
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.raw["file_path"] == "counter.py"
    assert "test_triad" in rejected.error


def test_decompose_non_json_output_raises_clearly():
    llm = ScriptedChatLLM(["not json at all"])
    try:
        decompose_spec("Add a slugify util.", llm)
        assert False, "expected a ValueError"
    except ValueError as e:
        assert "not valid JSON" in str(e)


def test_write_draft_json_writes_batch_and_rejected_sidecar(tmp_path):
    llm = ScriptedChatLLM([json.dumps([VALID_TASK, INVALID_TASK_MISSING_TRIAD])])
    result = decompose_spec("spec", llm)
    out = write_draft_json(result, tmp_path / "tasks.draft.json")

    from atomic_forge.batch_io import load_batch_json
    batch = load_batch_json(out)
    assert len(batch.tasks) == 1
    assert batch.tasks[0].file_path == "string_utils.py"

    rejected_path = tmp_path / "tasks.draft.json.rejected.json"
    assert rejected_path.exists()
    rejected = json.loads(rejected_path.read_text())
    assert rejected[0]["raw"]["file_path"] == "counter.py"
