# R10 — Self-review: does the patch actually resolve the issue?

**Requirement:** Self-review before opening a PR — verify the change
actually resolves the stated issue, not just that tests pass.

**Sourced from:** Sweep.dev.

**Status in atomic-forge:** **Met for the `fix` pipeline — verified against
code 2026-08-29.** `testgen.py::oracle_fails_on_buggy` confirms the
generated regression test actually reproduces the reported bug (fails on
the pre-fix code) before repair starts, and `fix.py::_ground_truth_green`
independently re-runs that same test after repair — "not trusting the
repair loop's self-report" is the literal docstring. This is the grounded,
non-model-judged verification the requirement calls for. The `run`/`repair`
CLI phases (no originating issue text to check against) still have no
symptom-to-test mapping check — Phase 1 below is still relevant there, just
lower-priority than originally scoped.

## State of the art

- **Teaching Large Language Models to Self-Debug**
  ([arXiv:2304.05128](https://arxiv.org/abs/2304.05128)) — "rubber-duck"
  self-explanation improves correctness on suites *with* executable tests by
  up to 12 points, but shows near-zero gain without them. This is the key
  finding for this requirement: self-review only earns its keep when it's
  grounded in actual execution, which makes [[Environment-Bootstrap]]
  a prerequisite for this requirement rather than a parallel, independent
  feature.
- **Revisit Self-Debugging with Self-Generated Tests**
  ([arXiv:2501.12793](https://arxiv.org/abs/2501.12793)) — the sharp
  limitation: self-generated tests are unreliable oracles — a correct
  program can fail a generated test (false negative) and a flawed program
  can pass one (false positive).

## Implication for atomic-forge

This is direct, specific support for a real forge design choice: the
`test_triad` (positive/negative/recovery) is required and presumably
spec/human-derived, not model-invented at repair time. 2501.12793's finding
about unreliable self-generated-test oracles is exactly the failure mode
that a *fixed*, upfront test contract avoids. If forge ever adds a "does
this resolve the issue" semantic check, it should be layered on top of the
existing execution-based gate (per 2304.05128's finding), not as a
standalone LLM judgment call.

## What needs to be done (to beat the competition)

1. **Build the semantic check as a deterministic mapping, not an LLM
   opinion.** Per 2304.05128's finding that self-review only helps when
   grounded in execution: require that the `test_triad`'s positive test
   actually exercises the symptom described in the issue (e.g. the same
   function/traceback frame named in the issue text appears in the
   passing test's coverage) before marking a verdict "resolved" — a static
   check, not a second model call asking "does this look right?"
2. **Never let the model invent the verification test.** Per 2501.12793's
   finding that self-generated tests are unreliable oracles, this is
   already forge's structural advantage (`test_triad` is required upfront,
   not generated post-hoc at repair time) — the action item is to *keep* it
   that way rather than adding an LLM-judged "does this resolve it" step
   that reintroduces the same unreliable-oracle risk from a different angle.
3. **Layer, don't replace.** Any semantic check goes on top of the existing
   execution-based gate (per [[Environment-Bootstrap]]), never as a
   substitute for actually running the suite.

## Implementation plan

**Phase 1 — symptom-to-test mapping check (~2–3 days)**
- After a verdict is `passed`, statically confirm that the `test_triad`'s positive test's coverage touches the function/traceback frame the original issue text names (via the same symbol-resolution machinery `LocalToolBackend`/`GraphToolBackend` already provide).
- If the mapping can't be established (issue text too vague to name a symbol), fall back to today's behavior — don't block on this check, only strengthen it.

**Phase 2 — surface the check's result, don't gate on it initially (~1 day)**
- Add the mapping result (`confirmed` / `unmappable`) to the run's phase history in `checkpoint.py` as an informational field first, so its accuracy can be observed against real runs before it's allowed to reject a passing verdict.

**Phase 3 — promote to a gate once validated (~1 day, after enough data)**
- Once Phase 2's data shows the check reliably agrees with human judgment on a sample of `benchmarks/` cases, allow `unmappable`-but-suspicious results to trigger one extra repair attempt rather than shipping immediately.

## Related
- [[Environment-Bootstrap]] — same underlying grounding problem
- [[Environment-Bootstrap]] — the execution grounding this requirement depends on
