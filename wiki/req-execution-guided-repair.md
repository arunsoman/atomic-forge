# R14 — Execution-guided repair (patches selected by actually running the suite)

**Requirement:** Actually execute the test suite before proposing a fix,
rather than asking a model to judge correctness by inspection.

**Sourced from:** Google Jules, OpenHands.

**Status in atomic-forge:** **Met, and more sophisticated than scoped —
verified against code 2026-08-29.** `repair_agent.py::repair_loop_agentic`
does apply→test→restore per candidate, picks the smallest-diff green
winner (or fewest-failures if none are green, only if strictly better),
auto-reverts any round that regresses, and additionally supports
`required_pass_count` (re-runs the suite N times before trusting a green
verdict, to absorb flaky tests) — a flake-tolerance mechanism beyond what
this requirement's research review anticipated.

**⚠️→✅ Correctness fix 2026-08-29 (found while validating [[req-parallel-execution]]):**
this whole requirement's guarantee — "the winner is picked by actually
running the suite" — depended on each subprocess test run actually
reflecting the CURRENT file content, which was sometimes false: a
write→test→rewrite→test sequence (exactly what candidate selection and
multi-round repair both do) could hit a stale pytest assertion-rewrite
`.pyc` cache and silently evaluate the PREVIOUS content instead. Fixed in
`sandbox.py::_purge_pycache`, called before every `run_test`/
`run_test_with_progress` invocation. See [[req-parallel-execution]] for
the full root-cause writeup and reproduction numbers. Worth flagging here
specifically: this bug meant R14's core claim was, before the fix,
occasionally *not actually true* in practice — the mechanism was right,
the implementation had a real gap.

## State of the art

- **SWE-bench: Can Language Models Resolve Real-World GitHub Issues?**
  (Jimenez et al., [arXiv:2310.06770](https://arxiv.org/abs/2310.06770)) —
  the field's foundational benchmark. Patches are only scored by running the
  repo's own tests — the same principle atomic-forge's own benchmarks claim
  to follow ("scored by actually running each repo's own test suite, not by
  asking a model which patch looks right").
- **DynaFix: Iterative Automated Program Repair Driven by Execution-Level
  Dynamic Information** ([arXiv:2512.24635](https://arxiv.org/abs/2512.24635))
  — argues coarse pass/fail signals under-inform repair; fine-grained
  execution traces (variable states, control-flow paths) outperform
  pass/fail alone. A candidate upgrade to forge's current
  pass/fail-plus-traceback verdict signal (the 7-way verdict taxonomy in
  `checkpoint.py`).
- **Runtime Execution Traces Guided APR with Multi-Agent Debate**
  ([arXiv:2604.02647](https://arxiv.org/abs/2604.02647)) — pairs execution
  traces with multi-agent debate for patch selection, an alternative
  selection mechanism to forge's single deterministic gate.

## Implication for atomic-forge

This requirement is already forge's strongest-evidenced claim — it's the
same principle underlying SWE-bench itself. The clearest research-backed
upgrade is DynaFix's finding: forge's 7-way verdict taxonomy
(`passed`/`failed`/`partial`/`timeout`/`lint_error`/`crashed`/`skipped`) is
already richer than plain pass/fail, but still coarser than variable-state/
control-flow-level traces. Worth prototyping whether feeding richer
execution traces into the K-sampled retry prompt (not just the verdict
label) improves fix rate on the existing `benchmarks/` harness.

## What needs to be done (to beat the competition)

1. **Capture execution traces, not just pass/fail, in the verdict.** Extend
   `checkpoint.py`'s 7-way taxonomy with a trace payload (variable state at
   failure point, control-flow path taken) per DynaFix (arXiv:2512.24635) —
   feasible via `pytest --tb=long` plus a local frame dump on failure, no
   new test infra required.
2. **Feed the trace into the next K-sample prompt, not just the verdict
   label.** This is the actual mechanism by which DynaFix outperforms
   coarse pass/fail — the taxonomy alone doesn't help unless the richer
   signal reaches the next attempt.
3. **Prototype on `benchmarks/` before rolling out.** Measure fix-rate delta
   with trace-augmented retries vs. current traceback-only retries on the
   existing harness — this is a benchmarkable, falsifiable change, not a
   speculative one.
4. **Keep this as the load-bearing gate.** Per SWE-bench's own founding
   principle (arXiv:2310.06770) and reinforced by every other requirement
   here that depends on grounding ([[req-critic-self-verification-gate]],
   [[req-self-review-issue-resolution]]) — every other improvement should
   compose with execution-based selection, never bypass it.

## Implementation plan

**Phase 1 — trace capture (~2 days)**
- Run failing tests with `pytest --tb=long` (or language-appropriate equivalent) and capture local variable state at the failure frame; store as a new field on the existing verdict object in `checkpoint.py`, alongside (not replacing) the current pass/failed/etc. label.

**Phase 2 — prompt integration (~1 day)**
- Update `repair_agent.py`'s retry-prompt construction to include the trace payload from Phase 1 for the next K-sample attempt, formatted concisely (per the ACI error-schema work in [[req-agent-computer-interface]] if that's landed by then).

**Phase 3 — benchmark delta (~1–2 days)**
- Run `benchmarks/` with trace-augmented retries on vs. off; only keep it on by default if fix-rate improves or attempt-count-to-fix drops, since richer prompts cost more tokens per attempt.

**Phase 4 — guard the principle (ongoing, no code)**
- Whenever any other requirement's plan touches patch selection (R2, R10, R13), confirm in review that execution-based selection remains the final gate — trace richness and critique layers augment it, never bypass it.

## Related
- [[req-critic-self-verification-gate]] — why execution-grounding beats self-judgment
- [[req-self-review-issue-resolution]] — depends on this requirement being met first
- [[req-parallel-execution]] — the selection step K-sampling relies on
