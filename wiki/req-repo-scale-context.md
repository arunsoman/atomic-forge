# R4 — Repository-scale context (symbol/graph awareness)

**Requirement:** Maintain a whole-codebase symbol map (e.g. tree-sitter-derived)
so the model has structural awareness without loading every file into context.

**Sourced from:** Aider (repo-map).

**Status in atomic-forge:** **Met — verified against code 2026-08-29.**
`codegraph.py`'s `CodeGraph` persists a SQLite call graph
(`.forge/codegraph.db`) with precomputed, indexed `edges` table; `build()`
is incrementally hash-diffed (unchanged files cost one query, not a
re-parse); `GraphToolBackend` wraps it behind the same `ToolBackend`
protocol as `LocalToolBackend`.

## State of the art

- **Retrieval-Augmented Code Generation: A Survey with Focus on
  Repository-Level Approaches**
  ([arXiv:2510.04905](https://arxiv.org/abs/2510.04905)) — the unifying
  survey for this requirement. Frames repository-level code generation as a
  coupled process: context construction → retrieval optimization →
  generation → environment interaction. Useful as a structural checklist
  against `codegraph.py`/`tools.py`: forge covers context construction and
  generation solidly, but "retrieval optimization" (ranking/pruning what the
  graph surfaces) and "environment interaction" (using execution feedback to
  *refine* the retrieved context, not just the patch) are less developed.
- **Knowledge Graph Based Repository-Level Code Generation**
  ([arXiv:2505.14394](https://arxiv.org/abs/2505.14394)) — graph-structured
  repo representations outperform flat retrieval for cross-file coherence,
  directly supporting forge's choice of a call graph over embeddings.

## Implication for atomic-forge

This requirement is already substantively met; the research gap is less
"should forge have a graph" (yes, it does) and more granularity and use —
see [[Environment-Bootstrap]] for the statement-level granularity
upgrade path (ARISE) that's the natural next step past forge's current
function-level call graph.

## What needs to be done (to beat the competition)

1. **Add a retrieval-optimization/ranking layer on top of `codegraph.py`.**
   Currently `callers`/`callees`/`affected_by` return full edge sets; per
   the survey's context-construction → retrieval-optimization split, add
   relevance ranking (proximity to the failing test, recency of edit,
   symbol overlap with the issue text) so the model sees the highest-signal
   neighbors first, not every edge.
2. **Close the retrieval loop with execution feedback.** Per the survey's
   "environment interaction" stage — which forge doesn't yet have — re-query
   `codegraph.db` using the *failing test's* traceback frames after a rejected
   attempt, rather than only querying it once up front from the issue text.
3. **Validate against 2505.14394's claim directly.** That paper shows graph
   beats flat embeddings for cross-file coherence — forge already made this
   bet; worth confirming it still holds by A/B-testing `GraphToolBackend` vs.
   the unindexed `ripgrep_tool_backend.py` reference on multi-file
   `benchmarks/` cases specifically (not just single-file ones, where the
   difference should be largest).

## Implementation plan

**Phase 1 — ranked retrieval (~2 days)**
- Add a scoring function over `codegraph.py`'s `callers`/`callees`/`affected_by` results: proximity to the failing test's traceback frames, symbol-name overlap with the issue text, recency of last edit.
- Return top-N ranked edges instead of the full set by default, with an escape hatch to fetch the rest.

**Phase 2 — feedback-driven re-query (~2–3 days)**
- After a rejected K-sample attempt, extract the new failing test's traceback frames and issue a fresh `affected_by` query against `codegraph.db` scoped to those frames.
- Merge the new results into the next attempt's context instead of reusing the original issue-derived neighborhood unchanged.

**Phase 3 — A/B validation (~1 day)**
- Run `benchmarks/` multi-file cases with `GraphToolBackend` vs. `ripgrep_tool_backend.py`, before and after Phases 1–2, to confirm the graph's advantage is real and growing, not assumed.
- Record results in this file; if Phase 1/2 don't move the needle on forge's own cases, say so plainly rather than keeping unused ranking code.

## Related
- [[Environment-Bootstrap]] — same survey, scaling angle
- [[Environment-Bootstrap]] — the navigation/edit half of the same problem
