# R5 — Auto-commit each accepted edit with a descriptive message

**Requirement:** Auto-commit each accepted edit with a descriptive,
attributed commit message.

**Sourced from:** Aider (git integration).

**Status in atomic-forge:** **Met (templated, not LLM-generated) — verified
against code 2026-08-29.** `sandbox.py::commit()` auto-commits every
accepted repair round with a descriptive, structured message (e.g. `"forge:
repair {file} (round N, green/best-effort, diff N)"`, or `"forge: revert
round N (failures A -> B)"` on auto-revert). Templated messages carry more
precise, verifiable detail (exact round/diff/failure counts) than an
LLM-authored summary would reliably include — deprioritizing the
LLM-generation upgrade below; the requirement's actual intent (auto-commit +
descriptive) is already satisfied.

## State of the art

Real but tangential literature — commit-message generation is a
well-studied NLG task, just not central to forge's repair-loop value
proposition:

- **Automated Commit Message Generation with Large Language Models: An
  Empirical Study and Beyond**
  ([arXiv:2404.14824](https://arxiv.org/abs/2404.14824)) — LLM-authored
  messages win human preference in ~78% of evaluated samples over prior
  state-of-the-art, despite mixed results on BLEU/ROUGE-L (i.e. n-gram
  metrics undersell how good these actually are to a human reviewer).
- **An Empirical Study on Commit Message Generation Using LLMs via
  In-Context Learning** ([arXiv:2502.18904](https://arxiv.org/abs/2502.18904))
  — in-context learning (no fine-tuning) already outperforms prior
  specialized commit-message models, meaning this requirement is cheap to
  satisfy well with the same model forge already calls, no dedicated
  training or tooling needed.
- **Brevity is the Soul of Wit: Condensing Code Changes to Improve Commit
  Message Generation** ([arXiv:2509.15567](https://arxiv.org/abs/2509.15567))
  — feeding a condensed diff (not the raw diff) improves message quality;
  relevant if forge generates the commit message from the same diff object
  `patch.py` already produces.

## Implication for atomic-forge

Low effort, low risk: this can likely be satisfied by prompting the same
model already in the loop with the accepted diff, no architecture change
required. Worth confirming current behavior before treating it as a gap.

## What needs to be done (to beat the competition)

1. **Confirm current state first.** Check whether forge already auto-commits
   accepted patches; if git integration exists but lacks generated messages,
   this is a small addition, not new plumbing.
2. **Condense the diff before prompting for a message**, per
   arXiv:2509.15567 — feed a summarized change description (files touched,
   symbols changed, verdict) rather than the raw unified diff, which that
   paper shows improves message quality.
3. **Use in-context examples, no fine-tuning**, per arXiv:2502.18904 — a
   handful of good example (diff-summary → message) pairs in the prompt is
   sufficient; don't over-invest here relative to the repair-loop work.
4. **Keep the `Co-authored-by` / attribution trailer** consistent with
   forge's existing commit conventions so generated commits are
   indistinguishable in provenance tracking from any other forge-made commit.

## Implementation plan

**Phase 1 — confirm current behavior (~0.5 day)**
- Grep the codebase for existing git-commit logic (likely near `checkpoint.py` or the CLI entrypoint) and confirm whether commits happen at all today, and if so, whether messages are already descriptive.

**Phase 2 — diff condenser (~1 day, skip if Phase 1 shows nothing to build on)**
- Add a small summarizer that turns an accepted patch's unified diff into a condensed change description (files touched, symbols added/changed/removed, verdict), per arXiv:2509.15567 — this is a pure-text transform, no model call needed for the condensing step itself.

**Phase 3 — message generation (~1 day)**
- Prompt the already-in-loop model with the condensed diff plus 2–3 in-context example (summary → message) pairs, per arXiv:2502.18904.
- Auto-commit with the generated message plus forge's standard attribution trailer.

**Phase 4 — validation (~0.5 day)**
- Spot-check generated messages on 10–15 `benchmarks/` runs for accuracy (does the message match what actually changed) before enabling by default.
