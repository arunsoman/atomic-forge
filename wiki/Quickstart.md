## 1. Describe your work as `AtomicTask`s

```python
from atomic_forge import AtomicTask, AtomicTaskBatch, TestTriad

task = AtomicTask(
    name="create adder",
    task_type="dev",
    action="create",
    file_path="adder.py",
    description="A two-argument add function.",
    function_signatures=["def add(a, b)"],
    test_triad=TestTriad(
        positive="add(2, 3) == 5",
        negative="add('a', 1) raises TypeError",
        negative_to_positive="retry with valid ints succeeds",
    ),
)
batch = AtomicTaskBatch(tasks=[task])
```

The contract is machine-checked: exactly one file per task, and a required
positive / negative / recovery triad enforced by pydantic at construction
time — not negotiated with a prompt. Details in [[Repair-Loop]] and
[[Architecture]].

## 2. Or let the model draft them — with the same gate

**Don't want to hand-author the JSON?** Ask the model to draft it from a
loose natural-language spec/issue:

```bash
atomic-forge decompose --spec spec.md --out tasks.draft.json
```

The exact same `AtomicTask` validation runs on the draft; anything that
fails is written to `tasks.draft.json.rejected.json` with the real reason
instead of being silently dropped. The draft is scaffolding meant for
human review before it's ever passed to `run`. See
[`decompose.py`](https://github.com/kannamma-labs/atomic-forge/blob/main/src/atomic_forge/decompose.py).

## 3. Run the pipeline

```bash
export FORGE_API_KEY=sk-...      # or any OpenAI-compatible endpoint — [[Installation-and-LLM-Setup]]
atomic-forge run --tasks tasks.json --project-dir ./out
```

Three phases, in order:

1. **generate** — each task's file is written by an agentic session that
   reads its dependencies off disk first, then patches or writes the file,
   gated by a syntax check + a contract check (declared signatures actually
   present) before it's accepted.
2. **qa** — every dev task with a `test_triad` and no explicit QA task gets
   a real test file, synthesized straight from the triad through the same
   agentic path.
3. **repair** — the project's test suite is run (auto-detected);
   any failures go through the [[Repair-Loop]]: localize → sample →
   select-by-execution → blast-radius gate.

Every phase transition is checkpointed *before* work starts
([[Checkpointing-and-Resumability]]).

## 4. Or drive it from Python

```python
from atomic_forge.generate_agent import generate_batch_agentic
from atomic_forge.qa import qa_phase
from atomic_forge.repair_agent import repair_loop_agentic
from atomic_forge.llm import default_llm
from atomic_forge.tools import make_tools
from atomic_forge.trajectory import Trajectory

project_dir = "./out"
llm = default_llm()
tools = make_tools(project_dir)
traj = Trajectory(project_dir)

gen_result = generate_batch_agentic(project_dir, batch, llm, tools, traj)
qa_phase(project_dir, batch, llm, tools, traj)
report = repair_loop_agentic(
    project_dir, llm, tools, traj,
    tasks_by_file={t.file_path: t.name for t in batch.dev_tasks()},
)
print(report)  # {"success": True, "rounds": 1, "initial_failures": 2, "final_failures": 0, ...}
```

## 5. Resumable runs

```python
from atomic_forge.checkpoint import RunCheckpointer, new_run_id, diff_file_hashes, hash_files, load_run

run_id = new_run_id()
ckpt = RunCheckpointer(run_id=run_id, project="myproj", project_dir=str(project_dir))
ckpt.mark_phase("generate")
# ... do work, then:
ckpt.mark_written(hash_files(project_dir, [t.file_path for t in batch.tasks]))
ckpt.finish("passed")

# Later, resume:
record = load_run(run_id)
diff = diff_file_hashes(project_dir, record.file_hashes)
# diff.unchanged -> trust these, skip regenerating
# diff.changed   -> regenerate only these
```

Next: [[CLI-Reference]] · [[Issue-to-PR]] · [[Benchmarks]]