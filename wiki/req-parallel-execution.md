# R13 — Parallel/isolated task execution & patch selection (K-sampling)

**Requirement:** Execute tasks fully asynchronously in an isolated per-task
environment, so many tasks can run in parallel and be reviewed only when
ready.

**Sourced from:** Google Jules.

**Status in atomic-forge:** Partial — `concurrency.py`'s adaptive worker
pool parallelizes LLM calls *within* a run (used by `generate_batch_agentic`);
there's no per-task isolated-VM model for running many independent tasks
concurrently.

**✅ IMPLEMENTED 2026-08-29 (within-task K-sampling half):** the K sampled
repair attempts in `repair_agent.py::repair_loop_agentic` were sequential
(`for k in range(samples): _attempt_patch(...)`) — now run concurrently via
`ThreadPoolExecutor` by default (new `parallel_samples: bool = True` param,
opt-out available). Verified safe: each attempt's `submit_check` only
validates a candidate in memory (no disk/tool-backend writes until the
sequential execution-based-selection step after all K finish), and
`CodeGraph`'s SQLite connection is now `check_same_thread=False` + guarded
by an `RLock` (see `codegraph.py`) since `GraphToolBackend` is shared
read-only across the K threads. Added `TurnByPositionScriptedLLM` to
`tests/_helpers.py` (indexes scripted responses by conversation position,
not a shared call counter — the only correct way to script concurrent
agent conversations) and two new tests in `test_repair_agent.py`
(`test_repair_loop_parallel_samples_fixes_real_bug`,
`test_repair_loop_sequential_samples_still_works`), each run 5x clean with
no flakes. Full suite green (164 passing).

Cross-task parallelism (the Jules-style "many independent tasks in
isolated sandboxes at once," Phase 1 of the plan below) is **not**
implemented — see the phased plan below for that remaining piece.

**Bonus find while validating this under load:** parallelizing K-sampling
surfaced a real, *pre-existing* correctness bug unrelated to concurrency
itself — see `sandbox.py::_purge_pycache`'s docstring. Rewriting the same
Python module and re-testing it via a fresh subprocess, twice in quick
succession (exactly what K-sampling — parallel OR sequential — and
multi-round repair both do), could intermittently evaluate STALE cached
bytecode from the previous content: reproduced directly and isolated from
all of forge's own code (a bare write→pytest→rewrite→pytest loop, ~30-40%
stale-result rate), root-caused to pytest's assertion-rewrite `.pyc` cache
trusting an (mtime, size) match that can coincide across two different-
content writes on coarse-mtime-resolution filesystems.
`PYTHONDONTWRITEBYTECODE=1` alone does NOT fix it (only suppresses
writing new cache entries, not trusting old ones already on disk) —
`sandbox.py::run_test`/`run_test_with_progress` now purge every
`__pycache__` under `project_dir` before each test run, closing the read
side too. Verified 15/15 clean over repeated full runs (was ~50% flaky on
`architect_mode` tests specifically, which write→test→write→test fastest)
before the fix, 0 failures across 191 passing tests + 3 repeated full-suite
runs after. This was silently corrupting the trustworthiness of *every*
repair loop's execution-based candidate selection (the R14 guarantee)
whenever a fix and a re-test landed close enough together — not a
parallelization-only bug, just one parallelization made easy to trigger
and see.

## State of the art

- **Trae Agent: An LLM-based Agent for Software Engineering with Test-time
  Scaling** ([arXiv:2507.23370](https://arxiv.org/abs/2507.23370)) — the
  closest published analogue to forge's existing design: high-temperature
  sampling across independent runs, with patches selected by actually
  executing them. Forge's "K sampled attempts, selected by running the real
  suite" is this same pattern.
- **Dissecting the SWE-Bench Leaderboards: Profiling Submitters and
  Architectures of LLM- and Agent-Based Repair Systems**
  ([arXiv:2506.17208](https://arxiv.org/abs/2506.17208)) — empirically
  surveys how top systems pick among candidate patches: LLM-as-judge vs.
  execution-based vs. multi-model diversity. Useful reference for whether
  forge's execution-only selection is leaving accuracy on the table versus a
  hybrid judge+execution approach.
- **When Parallelism Pays Off: Cohesion-Aware Task Partitioning for
  Multi-Agent Coding** ([arXiv:2606.00953](https://arxiv.org/abs/2606.00953))
  — parallelism helps only when subtasks are low-cohesion. Relevant if forge
  ever parallelizes *across* `AtomicTask`s (not just across K patch attempts
  for one task) — cohesive tasks (touching the same files/callers) may not
  benefit from, or may actively suffer from, naive parallel execution.

## Implication for atomic-forge

Forge's within-task K-sampling is already state-of-the-art in spirit
(Trae Agent validates the pattern). The gap versus Jules specifically is
cross-task isolation/parallelism (many independent `AtomicTask`s running in
separate sandboxes at once) — 2606.00953's cohesion-awareness finding
suggests this shouldn't be naive fan-out; tasks sharing call-graph neighbors
(see [[req-enterprise-scale-indexing]]) are exactly the ones blast-radius
gating exists to protect, and running them concurrently without coordination
risks the same class of cross-task collision the gate currently catches
within a single task.

## What needs to be done (to beat the competition)

1. **Add a task-level scheduling tier above `concurrency.py`'s worker pool.**
   Today the pool parallelizes LLM *calls*; add a layer that groups
   `AtomicTask`s by call-graph cohesion (using `codegraph.py`'s existing
   `affected_by` edges) before dispatch.
2. **Run cohesive groups serially, independent groups in parallel**, per
   arXiv:2606.00953's finding that naive fan-out hurts when subtasks share
   dependencies — tasks touching the same callers are exactly the ones the
   blast-radius gate exists to protect from cross-task collision, so they
   should never run concurrently against the same working tree.
3. **Match Trae Agent's validated pattern for within-task sampling** — keep
   K-sampling + execution-based selection as is (arXiv:2507.23370 confirms
   it's already state-of-the-art), and spend new engineering effort on the
   cross-task tier above, not on re-deriving within-task selection.
4. **Consider a hybrid judge+execution selector** per the SWE-Bench
   leaderboard analysis (arXiv:2506.17208) as a follow-up experiment once
   cross-task parallelism exists — only after confirming it beats
   execution-only selection on `benchmarks/`.

## Implementation plan

**Phase 1 — cohesion grouping (~2 days)**
- Before dispatching a batch of `AtomicTask`s, query `codegraph.py`'s `affected_by` edges to group tasks that share callers/callees into cohesion clusters.

**Phase 2 — two-tier scheduler (~3 days)**
- Extend `concurrency.py`'s adaptive worker pool with a task-dispatch layer: clusters run across separate workers in parallel; tasks *within* a cluster run serially against a shared working tree to avoid the cross-task collision the blast-radius gate already guards against within a single task.
- Reuse the existing adaptive ramp/step-down logic unchanged for the LLM-call layer underneath.

**Phase 3 — validation (~1–2 days)**
- Run a `benchmarks/` batch with deliberately cohesive and deliberately independent task sets; confirm naive full-parallel dispatch shows more collisions/gate-rejections on the cohesive set than the two-tier scheduler does, per arXiv:2606.00953's prediction.

**Phase 4 — hybrid selector experiment (~2 days, follow-up only)**
- Once Phases 1–3 are stable, prototype an LLM-as-judge pre-filter ahead of execution-based selection (per arXiv:2506.17208) and benchmark it against execution-only selection before adopting.

## Related
- [[req-planner-executor-split]] — orthogonal axis: breadth (K/parallel tasks) vs. depth (plan/execute roles)
- [[req-execution-guided-repair]] — the selection mechanism K-sampling depends on
