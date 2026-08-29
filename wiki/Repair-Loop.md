*Signal, not vibes: each stage narrows the suspect set with evidence, and
selection is by execution, not by asking the model which patch looks
right.*

## The loop

```
failing tests → extract signals → localize → sample K patches
             → select by running the real suite → blast-radius gate → commit
```

1. **Signals** — traceback paths, exception types, failing-test identity,
   plus a flake-tolerance pass (a test that also fails on the *unpatched*
   baseline is evidence, not a verdict).
2. **Localization** — evidence-ranked suspects: traceback frames → symbol
   resolution → call-graph blast radius ([[CIE-Integration]] when CIE is
   present; the built-in `ToolBackend`s otherwise). Long functions get
   a second pass at statement granularity — [[Statement-Level-Graph]].
3. **K sampled attempts** — K independent patch attempts run in parallel
   (adaptive, rate-limit-aware worker pool).
4. **Selection by execution** — each candidate is applied, the *real*
   suite is run, the tree is restored. The winner is empirically better,
   not the model's favorite. (`sandbox.py::_purge_pycache` exists because
   a write→retest cycle could otherwise evaluate a stale `.pyc` and
   silently corrupt this selection — root-caused and fixed; see
   [[Parallel-Execution]].)
5. **Blast-radius gate** — a static check that **rejects** a winning patch
   if it changes or removes a function/method signature while a caller
   outside the patched file still depends on it — the rejection is fed
   back into the next round's prompt rather than dropped.
6. **Commit** — each accepted edit is auto-committed with a descriptive
   message ([[Auto-Commit-Messages]]); any round that makes things
   worse is reverted.

## Verdicts, not booleans

Every run's outcome is one of 7 checkpointed verdicts:
`passed` / `failed` / `partial` / `timeout` / `lint_error` / `crashed` /
`skipped` — plus the full phase-by-phase history
([[Checkpointing-and-Resumability]]).

## Where it runs

- **Local** — direct subprocess execution with per-command timeouts.
- **Docker** — per-project persistent container
  ([[CLI-CI-Native]]); forge degrades gracefully with
  `FORGE_DISABLE_DOCKER_TESTS=1`.
- **[[Bootstrap-Gate]]** — before any of this runs against a cold clone,
  the environment is proven capable of running at least one test.

## Research grounding

Execution-guided selection and critique-gated repairs trace to
SWE-agent's agent-computer interface work ([arXiv:2405.15793](https://arxiv.org/abs/2405.15793)),
Agentless's localize-repair decompose ([arXiv:2408.03310](https://arxiv.org/abs/2408.03310)),
and self-refine loops ([arXiv:2303.17651](https://arxiv.org/abs/2303.17651)) —
the full survey with what forge adopts and what it deliberately does not
is in [[Execution-Guided-Repair]] and [[Critic-Verification-Gate]].