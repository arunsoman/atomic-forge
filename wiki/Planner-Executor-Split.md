# R3 — Planner/executor (architect/editor) model split

**Requirement:** Support a two-model split — a strong "planner" model that
reasons about the change, a cheap "editor" model that executes it — to cut
cost on multi-file work.

**Status in atomic-forge:** Not implemented — forge currently uses one model
per attempt (K sampled attempts), not a planner/editor role split.

**✅ PARTIALLY IMPLEMENTED 2026-08-29 — the plan-then-execute half, honestly
scoped:** `repair_agent.py::_plan_repair` adds an opt-in `architect_mode`
(new `repair_loop_agentic(..., architect_mode=False)` param, wired through
`fix.py::run_fix` and exposed as CLI `--architect` on both `repair` and
`fix`). When on, one extra LLM call asks for a structured
TARGET/CHANGE/CONSTRAINTS statement before each round's K-sampling, folded
into every attempt's prompt. Tests: `test_repair_loop_architect_mode_plans_then_fixes`,
`test_repair_loop_architect_mode_survives_planning_failure` (both green).

**What this is NOT**, deliberately: forge has no per-role model
configuration (`default_llm()` resolves exactly one endpoint), so this is a
same-model "plan, then execute" pass, not Aider's actual dual-model
cost-saving split (strong planner + cheap editor). Implementing a real
second, cheaper model would need new config plumbing (a `FORGE_PLANNER_*`
env var set, threading a second `ChatLLM` through the CLI) — real, scoped
follow-up work, not done here.

**Default is OFF, deliberately**, per this doc's own Phase 1 ("run the
SAFEdit test before building anything" — a live-LLM comparison against
plain K-sampling on forge's own `benchmarks/`). That comparison needs a
real LLM endpoint this environment doesn't have credentials for, so it
could not be run as part of this implementation pass — shipping the
feature default-on without it would be exactly the mistake SAFEdit warns
against. The flag exists, is fully wired and tested end-to-end with
scripted LLMs, and is safe to flip on a per-call-site basis once that
benchmark comparison is run.

## State of the art

- **Enhancing LLM-Based Agents via Global Planning and Hierarchical
  Execution (GoalAct)** ([arXiv:2504.16563](https://arxiv.org/abs/2504.16563))
  — a continuously-updated global plan plus hierarchical execution reduces
  per-step planning complexity and improves adaptability across task types.
- **SAFEdit: Does Multi-Agent Decomposition Resolve the Reliability
  Challenges of Instructed Code Editing?**
  ([arXiv:2604.25737](https://arxiv.org/abs/2604.25737)) — an important
  counter-signal: decomposing into planner/coder/tester agents does *not*
  automatically fix reliability. The paper interrogates when role-splitting
  actually helps versus just adding coordination overhead.
- **Architecting Resilient LLM Agents: A Guide to Secure Plan-then-Execute
  Implementations** ([arXiv:2509.08646](https://arxiv.org/abs/2509.08646))
  — plan-then-execute trades some adaptivity for predictability and lower
  cost, which maps directly onto Aider's stated motivation (cheap editor
  model after an expensive planner).

## Implication for atomic-forge

SAFEdit is the paper worth reading closely *before* committing to this: it's
specifically about whether decomposition helps or just adds overhead for
instructed code editing, which is forge's exact use case. If forge adopts a
planner/editor split, it should be validated against forge's own benchmark
harness (`benchmarks/`) rather than assumed from Aider's UX success — cost
savings and quality are separable claims, and SAFEdit suggests they don't
always move together.

## Implementation plan

**Phase 1 — SAFEdit-style reliability test (~2–3 days, gate for everything else)**
- Pick 10–15 multi-file cases from `benchmarks/`.
- Run each twice: current single-model K-sampling, and a throwaway planner→executor prototype (strong model emits a plain-text plan, same model executes it).
- Measure not just fix-rate but *variance* across repeated runs of the same case (SAFEdit's actual concern) — a split that's less consistent, even if average fix-rate ties, is a loss.
- **Decision point:** only proceed to Phase 2 if the split wins on both fix-rate and variance.

**Phase 2 — structured plan format (~2 days)**
- Define a `RepairPlan` schema (per-file: intent, target symbols, constraints) instead of prose, so the executor model's output can be validated against it before `patch.py` normalization runs.
- Planner model call happens once per `AtomicTask`, before K-sampling begins; the K-sampled executor attempts are all constrained to the same plan.

**Phase 3 — CLI surface (~1 day)**
- Add `--architect` flag (mirroring Aider's naming for user familiarity) to opt into Phase 2's behavior; default remains single-model K-sampling unless Phase 1 showed a clear win.

**Phase 4 — run-level plan (GoalAct-style, ~2 days, independent of Phases 1–3)**
- Add a run-level `RunPlan` object updated as each `AtomicTask` in a run completes (what changed, what broke, what's left).
- Feed it into later tasks' prompts in the same run for reprioritization — ship this regardless of the architect-mode decision, since it doesn't depend on the per-task split working out.

## Related
- [[Parallel-Execution]] — an orthogonal axis (breadth of K attempts vs. depth of plan/execute roles)