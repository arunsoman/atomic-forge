# atomic-forge

[![tests](https://github.com/arunsoman/atomic-forge/actions/workflows/test.yml/badge.svg)](https://github.com/arunsoman/atomic-forge/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue.svg)](https://www.python.org/downloads/)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-atomic--forge-blue.svg)](./action.yml)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/arunsoman/atomic-forge/compare)
[![discussions](https://img.shields.io/github/discussions/arunsoman/atomic-forge)](https://github.com/arunsoman/atomic-forge/discussions)
[![stars](https://img.shields.io/github/stars/arunsoman/atomic-forge?style=social)](https://github.com/arunsoman/atomic-forge/stargazers)

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

See [`benchmarks/`](benchmarks/) for the harness and methodology behind
these three claims — real merged GitHub bug fixes, run live against a
real LLM endpoint, scored by actually running each repo's own test
suite, not by asking a model which patch looks right.

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

## What this doesn't try to be

- Not a language server or an embeddings-based semantic search engine.
  `tools.py` ships two bundled `ToolBackend` implementations:
  `LocalToolBackend` (an in-memory symbol index — exact for Python via
  `ast`, regex-heuristic for JS/TS/Java) and `GraphToolBackend` (the same
  parsing, persisted to a SQLite call graph at `.forge/codegraph.db` with
  precomputed edges, so multi-hop `callers`/`callees`/`affected_by` are
  indexed lookups instead of a regex scan repeated per call — see
  [codegraph.py](src/atomic_forge/codegraph.py)). A worked third
  implementation —
  [`examples/ripgrep_tool_backend.py`](examples/ripgrep_tool_backend.py),
  a live-`rg` backend with no index/build step — is included as a
  reference for repos where even that up-front build is the bottleneck.
  Bring your own richer backend (a real language server, cross-repo
  analysis) by implementing the same protocol.
- Not a general production-infrastructure platform. `watchdog.py`'s
  `WatchdogLoop` does implement the detect → repair → canary → promote/
  rollback loop end to end (see below) — but its bundled `DeployTarget`
  is a real local-process/subprocess-and-reverse-proxy reference
  implementation, not Kubernetes or a real load balancer. Implement the
  same `DeployTarget` protocol against your own infra for that.

## Install

```bash
pip install git+https://github.com/arunsoman/atomic-forge.git   # one line, no checkout needed
atomic-forge --help                                            # sanity check the CLI
```

Or from a checkout: `pip install -e ".[dev]"` (also installs `pytest` for the suite).
Requires Python ≥3.10. At runtime forge needs an **OpenAI-compatible LLM
endpoint** — point it at OpenAI, or a local
[Ollama](https://ollama.com) model that supports tool-calling. See
[LLM configuration](#llm-configuration).

### Code-graph backend (optional, recommended): CIE

forge's repair loop can use
**[CIE — the Code Insight Engine](https://github.com/arunsoman/cie)** as its
code-graph backend, served as a real **MCP server** over stdio (the same
surface Claude Code / Cursor consume). CIE does localization + blast-radius
(`callers`, `affected_by`, `failing_context`, `file_skeleton`, …); forge does
the sample → select → gate → commit loop — **forge itself stays unchanged**.
Install it alongside and try a real open-source bug fix end-to-end in one line:

```bash
pip install git+https://github.com/arunsoman/cie.git pytest
python benchmarks/cie_forge_realbugs/forge_cie_bench.py boltons_bits_offbyone
```

Needs a tool-calling model at `http://localhost:11434/v1` (Ollama default);
override with `FORGE_MODEL` / `FORGE_BASE_URL` / `FORGE_API_KEY`. Full
methodology + 4/4 results: [`docs/cie-forge-realbug-benchmark.md`](docs/cie-forge-realbug-benchmark.md).
Reproducible seeds + harness: [`benchmarks/cie_forge_realbugs/`](benchmarks/cie_forge_realbugs/).

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

**Don't want to hand-author the JSON?** `atomic-forge decompose --spec
spec.md --out tasks.draft.json` asks the model to draft it for you from a
loose natural-language spec/issue — including a proposed `test_triad`.
This is scaffolding, not a shortcut around the contract: the exact same
`AtomicTask` validation runs on the draft, anything that fails is written
to `tasks.draft.json.rejected.json` with the real reason instead of being
silently dropped, and the draft is meant to be reviewed/edited by a human
before it's ever passed to `run`. See [decompose.py](src/atomic_forge/decompose.py).

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

## Production watchdog

`watchdog.py`'s `WatchdogLoop` closes the loop past your local working
copy: detect a live failure, repair it with the exact same evidence-based
localization + agentic sampling `repair_agent` uses, land the fix as a
canary, and promote or roll back on a real health check — no local test
suite required, since the canary's own health check is the pass/fail
oracle.

```bash
atomic-forge watch --project-dir ./out --log-file /var/log/app.log \
    --deploy-cmd "python app.py {port}" --canary-percent 10
```

- **Detect** — `LogFailureDetector` tails a log file for Python
  tracebacks, reuses `repair_agent.extract_signals` to parse them (no
  second parser to keep in sync), and dedupes by fingerprint so a
  steadily-repeating crash surfaces once, not once per poll.
- **Repair** — the traceback is localized and patched the same way a
  failing local test would be (`repair_agent.localize` +
  `_attempt_patch`), then committed.
- **Canary** — `LocalProcessCanaryDeployer` runs the pre-patch and
  post-patch code as two real subprocesses on two real ports, splits
  real HTTP traffic between them through a small stdlib reverse proxy,
  and health-checks the canary directly (not through the proxy, so a bad
  canary at 10% traffic still fails its own check immediately).
- **Promote / rollback** — N consecutive healthy checks promotes the
  canary (stable process torn down); any unhealthy check rolls back
  (canary torn down, the patch reverted and committed).

Both `FailureDetector` and `DeployTarget` are protocols with one real
reference implementation each — bring your own (a real error tracker, a
real load balancer/Kubernetes) by implementing the same protocol; nothing
else about `WatchdogLoop` changes.

## CIE + forge: real open-source bug fixes (and CIE-generated tests)

forge's repair loop can run with **[CIE](https://github.com/arunsoman/cie)**
(the Code Insight Engine) as its code-graph backend — served as a real
**MCP server** over stdio, the same way Claude Code/Cursor consume it.
CIE does localization + blast-radius (`callers`, `affected_by`,
`failing_context`, `file_skeleton`, …); forge does the sample →
execution-select → blast-radius-gate → commit loop. And now
`atomic-forge repair --raise-pr` pushes the fix on a fresh branch and
opens the GitHub PR, closing the loop end to end.

Two measured benchmarks (real runs against a live tool-calling model, not
simulated):

- **CIE-vs-no-CIE token cost** on one mathematically-subtle planted bug —
  the CIE-backed agent fixed it correctly in ~63% fewer tokens; the same
  agent without the graph broke the suite and did not converge. →
  [`docs/cie-graph-bugfix-benchmark.md`](docs/cie-graph-bugfix-benchmark.md)
- **Real bugs from real open-source repos** (more-itertools, boltons —
  MIT, many open issues): forge+CIE fixed **4/4** from the real PR's
  regression test, each in 1 round, every fix matching the merged PR.
  Then CIE **generated a valid regression test from just the bug
  description** for **4/4** (validated: fails on the buggy code, passes on
  the real fix — measured, not asserted), and forge+CIE fixed **4/4**
  against those generated tests. →
  [`docs/cie-forge-realbug-benchmark.md`](docs/cie-forge-realbug-benchmark.md)

Reproducible harness + seeds: [`benchmarks/cie_forge_realbugs/`](benchmarks/cie_forge_realbugs/)
and [`benchmarks/measure_cie_graph_benefit.py`](benchmarks/measure_cie_graph_benefit.py).
Raw results: [`benchmarks/results/`](benchmarks/results/). Short companion
notes on the *why* behind these designs: [`docs/aside.md`](docs/aside.md).

See also [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, the `AtomicTask`
contract, and how to add a benchmark case.

```bash
# CIE + forge on a bundled real bug (CIE served as an MCP server over stdio):
pip install git+https://github.com/arunsoman/cie.git pytest
python benchmarks/cie_forge_realbugs/forge_cie_bench.py boltons_bits_offbyone

# land a forge fix as a GitHub PR (forge's own repair CLI + --raise-pr):
atomic-forge repair --tasks bug.json --project-dir ./checkout \
  --test-cmd "pytest -q" --raise-pr
```

## Architecture

| Module | What it does |
|---|---|
| `models.py` | `AtomicTask` / `AtomicTaskBatch` — the task contract |
| `decompose.py` | Optional LLM-assisted draft of `AtomicTask` JSON from a loose spec — same contract enforced, human review still required |
| `planner.py` | Dependency-ordered execution planning (Kahn topological sort) |
| `agent.py` | The agentic session loop (TOOL / RUN / PATCH / SUBMIT grammar, or real function-calling) |
| `llm.py` | `ChatLLM` protocol + `OpenAICompatLLM` + provider resolution |
| `tools.py` | `ToolBackend` protocol + `LocalToolBackend` + `GraphToolBackend` (bring your own richer backend) |
| `codegraph.py` | Persisted SQLite call graph (precomputed edges, incremental rebuild) behind `GraphToolBackend` |
| `symbols.py` | The dependency-free symbol index behind `LocalToolBackend` and `codegraph.py`'s parsing |
| `patch.py` | The one canonical SEARCH/REPLACE parser |
| `generator.py` / `generate_agent.py` | Prompt building + the agentic/batch generation pipeline (including the direct multi-file-in-one-completion fast path for independent, dependency-free tasks) |
| `qa.py` | Synthesizes a test file per `test_triad`, gap-filling coverage |
| `repair_agent.py` / `repair.py` | The SOTA repair loop: signals → localize → sample → select → gate |
| `watchdog.py` | Production loop: detect a live failure → repair → canary → promote/rollback |
| `pr.py` | Raise a GitHub PR for a landed fix (`atomic-forge repair --raise-pr`, via `gh`) |
| `sandbox.py` / `docker_env.py` / `stacks.py` | Command execution, git, lint gate, test-stack detection, optional Docker sandboxing |
| `concurrency.py` | The adaptive rate-limit-aware worker pool |
| `checkpoint.py` / `checkpoint_store.py` | Crash-safe, resumable run state (SQLite) |
| `reporter.py` | Write-back protocol for task status/artifacts (bring your own backend) |
| `trajectory.py` | Append-only JSONL audit trail of every action taken |

## GitHub Action

This repo is also a GitHub Action (`action.yml` + `Dockerfile` at the
root) — a thin containerized wrapper around the CLI above, the one
integration surface this project ships. See
[`docs/github-action.md`](docs/github-action.md) for inputs/outputs and a
worked workflow.

## License

MIT — see [LICENSE](LICENSE).
