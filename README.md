# atomic-forge

An agentic **generate → test → repair** loop for LLM code generation, built
around three things most coding-agent libraries treat as an afterthought:

- **A strict, machine-checkable task contract.** Every unit of work is an
  `AtomicTask` — exactly one file, with a required `test_triad` (positive /
  negative / recovery), enforced at construction time by pydantic, not hoped
  for in a prompt. Decompose your PRD/issue/spec into `AtomicTask`s however
  you like; forge only cares that they satisfy the contract.
- **Crash-safe, resumable runs.** Every phase transition is durably
  checkpointed to SQLite *before* the work starts. Resume re-hashes every
  file on disk and regenerates only what actually changed or went missing —
  never an all-or-nothing restart.
- **A real repair loop, not a retry loop.** Failing tests are turned into
  ranked suspects (traceback paths, symbol resolution, blast-radius),
  patched via K sampled agentic attempts, selected by *actually running the
  suite* (not by asking the model which patch looks right), and gated by a
  static check that rejects a "passing" patch that silently breaks a caller
  elsewhere in the codebase.

## Why this exists

The generate → compile → test → repair loop itself isn't novel — aider,
SWE-agent, and OpenHands all do a version of it. What's here that's worth
a second look:

- **`patch.py`** — a SEARCH/REPLACE parser with a documented, tested
  normalization fallback chain (exact → whitespace-normalized →
  fully-normalized → line-number-stripped) and a disjointness preflight
  that rejects overlapping hunks *before* any text is rewritten, rather than
  letting two edits silently clobber each other depending on iteration
  order.
- **`checkpoint.py`** — per-file hash-diffing on resume, a 7-way verdict
  taxonomy (`passed`/`failed`/`partial`/`timeout`/`lint_error`/`crashed`/
  `skipped`) instead of a boolean, and the full phase-by-phase history of a
  run, not just its latest state.
- **`concurrency.py`** — an adaptive worker pool for parallel LLM calls:
  ramps up by 1 per success, steps down by 2 the instant a 429 is seen, with
  a monotonic counter so an in-flight success can't race a concurrent
  rate-limit step-down and cancel it out.
- **`repair_agent.py`** — localization by evidence (traceback frames,
  symbol resolution, failing-test distance), K independently-sampled patch
  attempts selected by *execution* (apply → run the real suite → restore),
  a blast-radius gate that statically rejects a winning patch if it changes
  or removes a function/method's signature while something outside the
  patched file still calls it, and auto-revert of any round that makes
  things worse.

## What's *not* here

This is the core engine, deliberately scoped down from a larger internal
project it was extracted from:

- No production "detect a live failure → patch → canary → roll out"
  watchdog loop. The repair loop here operates on your local working copy.
- No bundled code-intelligence backend (semantic search, a persisted code
  graph, cross-repo analysis). `tools.py` ships one real, working
  `LocalToolBackend` (an in-memory symbol index — exact for Python via
  `ast`, regex-heuristic for JS/TS/Java) behind a `ToolBackend` protocol;
  bring your own richer backend by implementing the same protocol.
- No story-batched multi-file-in-one-completion generation mode (a real
  optimization in the project this was extracted from, cut here to keep
  the surface area reviewable — `generate_batch_agentic`'s per-task /
  dependency-ordered path is what ships).

## Install

```bash
pip install -e ".[dev]"   # from a checkout
```

Requires Python ≥3.10. Needs an OpenAI-compatible LLM endpoint at runtime —
see [LLM configuration](#llm-configuration) below.

## Quickstart

### 1. Describe your work as `AtomicTask`s

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

Or write it as JSON (`examples/tasks.json` has a worked example) and use
the CLI.

### 2. Point forge at a real LLM endpoint

```bash
export FORGE_API_KEY=sk-...
export FORGE_BASE_URL=https://api.openai.com/v1   # or any OpenAI-compatible endpoint
export FORGE_MODEL=gpt-4o-mini
```

(`OPENAI_API_KEY` also works if that's already set for other tools — see
[LLM configuration](#llm-configuration).)

### 3. Run the pipeline

```bash
atomic-forge run --tasks examples/tasks.json --project-dir ./out
```

This runs three phases in order:

1. **generate** — each task's file is written by an agentic session that
   reads its dependencies off disk first, then patches or writes the file,
   gated by a syntax check + a contract check (declared signatures actually
   present) before it's accepted.
2. **qa** — every dev task with a `test_triad` and no explicit QA task gets
   a real test file, synthesized straight from the triad and generated
   through the same agentic path.
3. **repair** — the project's test suite is run (auto-detected: Python via
   `requirements.txt`/`pyproject.toml`, Node via `package.json`, or both);
   any failures go through the localize → sample → select → gate loop above.

### 4. Or drive it from Python

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

## LLM configuration

`default_llm()` resolves, in order:

1. `FORGE_MOCK=1` — use your own zero-network mock (register one via
   `atomic_forge.llm.set_mock_factory(...)`), useful for demos and CI.
2. `FORGE_API_KEY` / `FORGE_BASE_URL` / `FORGE_MODEL` — any OpenAI-compatible
   endpoint: OpenAI itself, a local vLLM/llama.cpp/Ollama proxy, OpenRouter,
   a corporate gateway.
3. `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`/`OPENAI_MODEL`) — the
   common case where you already have this set.
4. Otherwise: raises with a message naming exactly what to set. Never
   silently falls back to a fake key against real `api.openai.com`.

## Resumable runs

```python
from pathlib import Path
from atomic_forge.checkpoint import RunCheckpointer, new_run_id, diff_file_hashes, hash_files

run_id = new_run_id()
ckpt = RunCheckpointer(run_id=run_id, project="myproj", project_dir=str(project_dir))
ckpt.mark_phase("generate")
# ... do work, then:
ckpt.mark_written(hash_files(project_dir, [t.file_path for t in batch.tasks]))
ckpt.finish("passed")

# Later, resume:
from atomic_forge.checkpoint import load_run
record = load_run(run_id)
diff = diff_file_hashes(project_dir, record.file_hashes)
# diff.unchanged -> trust these, skip regenerating
# diff.changed   -> regenerate only these
```

## Architecture

| Module | What it does |
|---|---|
| `models.py` | `AtomicTask` / `AtomicTaskBatch` — the task contract |
| `planner.py` | Dependency-ordered execution planning (Kahn topological sort) |
| `agent.py` | The agentic session loop (TOOL / RUN / PATCH / SUBMIT grammar, or real function-calling) |
| `llm.py` | `ChatLLM` protocol + `OpenAICompatLLM` + provider resolution |
| `tools.py` | `ToolBackend` protocol + `LocalToolBackend` (bring your own richer backend) |
| `symbols.py` | The dependency-free symbol index behind `LocalToolBackend` |
| `patch.py` | The one canonical SEARCH/REPLACE parser |
| `generator.py` / `generate_agent.py` | Prompt building + the agentic/batch generation pipeline |
| `qa.py` | Synthesizes a test file per `test_triad`, gap-filling coverage |
| `repair_agent.py` / `repair.py` | The SOTA repair loop: signals → localize → sample → select → gate |
| `sandbox.py` / `docker_env.py` / `stacks.py` | Command execution, git, lint gate, test-stack detection, optional Docker sandboxing |
| `concurrency.py` | The adaptive rate-limit-aware worker pool |
| `checkpoint.py` / `checkpoint_store.py` | Crash-safe, resumable run state (SQLite) |
| `reporter.py` | Write-back protocol for task status/artifacts (bring your own backend) |
| `trajectory.py` | Append-only JSONL audit trail of every action taken |

## License

MIT — see [LICENSE](LICENSE).
