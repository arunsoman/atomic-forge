# R14 — Execution-guided repair (patches selected by actually running the suite)

**Requirement:** Actually execute the test suite before proposing a fix,
rather than asking a model to judge correctness by inspection.

**Status in atomic-forge:** **Met, and more sophisticated than scoped —
verified against code 2026-08-29.** `repair_agent.py::repair_loop_agentic`
does apply→test→restore per candidate, picks the smallest-diff green
winner (or fewest-failures if none are green, only if strictly better),
auto-reverts any round that regresses, and additionally supports
`required_pass_count` (re-runs the suite N times before trusting a green
verdict, to absorb flaky tests) — a flake-tolerance mechanism beyond what
this requirement's research review anticipated.

**⚠️→✅ Correctness fix 2026-08-29 (found while validating [[Parallel-Execution]]):**
this whole requirement's guarantee — "the winner is picked by actually
running the suite" — depended on each subprocess test run actually
reflecting the CURRENT file content, which was sometimes false: a
write→test→rewrite→test sequence (exactly what candidate selection and
multi-round repair both do) could hit a stale pytest assertion-rewrite
`.pyc` cache and silently evaluate the PREVIOUS content instead. Fixed in
`sandbox.py::_purge_pycache`, called before every `run_test`/
`run_test_with_progress` invocation. See [[Parallel-Execution]] for
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

## Implementation plan

**Phase 1 — trace capture (~2 days)**
- Run failing tests with `pytest --tb=long` (or language-appropriate equivalent) and capture local variable state at the failure frame; store as a new field on the existing verdict object in `checkpoint.py`, alongside (not replacing) the current pass/failed/etc. label.

**Phase 2 — prompt integration (~1 day)**
- Update `repair_agent.py`'s retry-prompt construction to include the trace payload from Phase 1 for the next K-sample attempt, formatted concisely (per the ACI error-schema work in [[Agent-Computer-Interface]] if that's landed by then).

**Phase 3 — benchmark delta (~1–2 days)**
- Run `benchmarks/` with trace-augmented retries on vs. off; only keep it on by default if fix-rate improves or attempt-count-to-fix drops, since richer prompts cost more tokens per attempt.

**Phase 4 — guard the principle (ongoing, no code)**
- Whenever any other requirement's plan touches patch selection (R2, R10, R13), confirm in review that execution-based selection remains the final gate — trace richness and critique layers augment it, never bypass it.

## Related
- [[Critic-Verification-Gate]] — why execution-grounding beats self-judgment
- [[Self-Review-Issue-Resolution]] — depends on this requirement being met first
- [[Parallel-Execution]] — the selection step K-sampling relies on