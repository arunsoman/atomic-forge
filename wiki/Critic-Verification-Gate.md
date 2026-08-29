# R2 — Critic/self-verification gate before shipping a patch

**Requirement:** Run a second, independent model (or check) that scores the
agent's own patch and can withhold/iterate instead of shipping a
low-confidence result.

**Status in atomic-forge:** **Met — verified against code 2026-08-29.**
`repair_agent.py::_blast_radius_violations` is exactly the static gate
described below, AND `pending_violations` is already fed verbatim into the
next round's `task_prompt` under a "Previous patch REJECTED — fix these
before resubmitting" header — Phase 3 of the plan below ("feed the gate's
rejection reason back into the next attempt") was already implemented, not
a to-do.

## State of the art

- **CRITIC: Large Language Models Can Self-Correct with Tool-Interactive
  Critiquing** (Gou et al., [arXiv:2305.11738](https://arxiv.org/abs/2305.11738))
  — external tool-grounded critique beats free-form self-critique across
  QA, math, and toxicity-reduction tasks.
- **When Can LLMs Actually Correct Their Own Mistakes? A Critical Survey**
  ([arXiv:2406.01297](https://arxiv.org/abs/2406.01297)) — the load-bearing
  caveat for this whole requirement: LLMs are poor at *finding* their own
  errors unsupervised. They correct well only once an error's location or
  signal is externally supplied (a failing test, a linter, a static check).
- **How Many Tries Does It Take? Iterative Self-Repair Across Model Scales
  and Benchmarks** ([arXiv:2604.10508](https://arxiv.org/abs/2604.10508)) —
  self-repair helps universally but plateaus unevenly by error type: name
  errors are repaired at high rates, assertion/logic errors remain the
  hardest category — relevant to how many of forge's K sampled attempts are
  worth budgeting per verdict type.

## Implication for atomic-forge

This is direct evidence *for* forge's design choice of a static blast-radius
gate over a learned critic model: the 2406.01297 survey documents exactly
the failure mode (self-critique without external grounding) that a
deterministic, execution/analysis-derived signal sidesteps. OpenHands'
critic model is not obviously an upgrade — it's a different, less-grounded
bet. See [[Execution-Guided-Repair]] for the closely related evidence
that execution-based grounding is what actually earns correctness gains.

## Implementation plan

**Phase 1 — extend the deterministic gate (~2–3 days)**
- In `repair_agent.py`'s blast-radius check, add: exported-API diff (public symbol removed/renamed), type-signature diff (parameter/return type changed) for statically-typed or type-hinted code.
- Each new check is a pure static-analysis function, independently unit-testable, added alongside the existing blast-radius logic — not a model call.

**Phase 2 — failure-class taxonomy (~1–2 days)**
- Extend `checkpoint.py`'s verdict enum (or add a sub-field) to classify a `failed` verdict as `name_error` / `type_error` / `assertion_error` / `logic_error` / `other`, parsed from the exception type and traceback.
- Store this classification in the run's phase history alongside the existing 7-way verdict.

**Phase 3 — adaptive K budget by failure class (~2 days)**
- In `repair_agent.py`'s K-sampling loop, read the failure-class history for the current task and bias remaining attempts: fewer retries for `name_error` (fixed reliably fast, per arXiv:2604.10508), more for `assertion_error`/`logic_error`.
- Feed the specific rejection reason (blast-radius conflict, or the classified failure) into the next attempt's prompt verbatim, not a generic "try again."

**Phase 4 — one-time critic-model comparison (~2–3 days, optional)**
- Implement an OpenHands-style critic (a second model call scoring confidence) behind a benchmark-only flag, purely to get a number.
- Run both configurations (static gate alone vs. static gate + critic) on `benchmarks/`; record the delta in this file and stop maintaining the critic path unless it wins clearly.

## Related
- [[Execution-Guided-Repair]] — why execution >> self-judgment as a gate
- [[Self-Review-Issue-Resolution]] — self-review's dependency on this same grounding problem