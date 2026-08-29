# R11 — Scaling to large/enterprise repos

**Requirement:** Scale to large, messy enterprise monorepos without
prohibitive indexing cost.

**Sourced from:** Codegen.com.

**Status in atomic-forge:** Partial — `GraphToolBackend`'s precomputed
SQLite graph is aimed at this; the unindexed `ripgrep_tool_backend.py`
example is offered as a fallback for repos where even that build is the
bottleneck.

**Scope correction 2026-08-29 (re-audited against actual code):** the
"iterative query, not one-shot lookup" half of this requirement's plan is
**already substantially met** — `CodeGraph.callers`/`.callees` (and the
`GraphToolBackend` methods that wrap them) already accept a `depth`
parameter and do real BFS traversal over the precomputed edges table, not
a single-hop lookup; `describe()`'s introspected tool manifest surfaces
this real signature (`callers(symbol, depth=1)`) to the agent
dynamically, so an agent can already request `depth=3` in one call rather
than needing to chain three separate tool calls. **Shipped 2026-08-29 — statement-level def-use granularity (ARISE):**
`codegraph.db` gained additive `statements` + `def_use` tables built
alongside the function-level tables in the same indexing pass
(`graph_statements.py`: stdlib-`ast`-exact for Python — real shadowing
semantics, module-level fallback marked `heuristic`, honest non-Python
"block" rows without def_use; `FORGE_STATEMENT_GRAPH=0` kill switch).
New query surface: `CodeGraph.statements_near(file, line)` /
`.uses_of(name)`, exposed to the agent as a `statement_graph(file, line)`
tool on BOTH backends (auto-surfaced by `describe()`'s introspection,
like every other tool), with a REPAIR_SYSTEM prompt block teaching the
agent to prefer it over re-reading long functions. 17 new tests in
`test_graph_statements.py`. The ARISE-delta measurement (+4.7 pass@1
target, before/after benchmark on `benchmarks/cases/`) is the remaining
Phase 3 follow-up; C/C++ files get honest heuristic block rows (no
statement-level def_use) per the same regex-tier honesty as their
parsers.

## State of the art

- **Retrieval-Augmented Code Generation: A Survey with Focus on
  Repository-Level Approaches**
  ([arXiv:2510.04905](https://arxiv.org/abs/2510.04905)) — same survey as
  [[Environment-Bootstrap]]; the scaling-relevant dimension here is its
  treatment of retrieval-substrate tradeoffs (index-once-query-many vs.
  live-search) — directly analogous to forge's `GraphToolBackend` (indexed)
  vs. the `ripgrep` reference backend (live, no index).
- **ARISE: A Repository-level Graph Representation and Toolset for Agentic
  Fault Localization and Program Repair**
  ([arXiv:2605.03117](https://arxiv.org/abs/2605.03117)) — a
  statement-level def-use graph beat SWE-agent's flatter navigation by +4.7
  pass@1 points. Directly comparable to forge's `GraphToolBackend`
  precomputed call graph: the granularity gap (function-level in forge vs.
  statement-level in ARISE) is a concrete, testable upgrade path.
- **Issue Localization via LLM-Driven Iterative Code Graph Searching**
  ([arXiv:2503.22424](https://arxiv.org/abs/2503.22424)) — treats fault
  localization as *iterative search over* the call graph rather than a
  single retrieval pass — closer to how forge could use `codegraph.py`
  interactively (agent-driven graph traversal) instead of as a one-shot
  precomputed index lookup.

## Implication for atomic-forge

Two concrete upgrade paths surfaced by the literature: (1) statement-level
graph granularity (ARISE), (2) iterative/agent-driven graph search instead
of one-shot lookup (2503.22424). Both are incremental on top of the existing
`codegraph.py` schema rather than a rearchitecture.

## What needs to be done (to beat the competition)

1. **Extend `codegraph.db`'s schema to statement-level def-use edges**, per
   ARISE (arXiv:2605.03117) — currently forge's graph is function-level
   (`callers`/`callees`); adding intra-procedural def-use edges down to the
   statement is the concrete, cited +4.7 pass@1-point upgrade over
   SWE-agent-style flat navigation.
2. **Expose iterative graph search, not one-shot lookup.** Per
   arXiv:2503.22424, add a tool-backend method that lets the agent issue a
   *sequence* of graph queries (follow a caller, then its caller, then
   check `affected_by`) within a single repair attempt, rather than forge
   computing one fixed neighborhood up front and handing it over.
3. **Keep the `ripgrep_tool_backend.py` fallback as the escape hatch** for
   repos where even the precomputed SQLite build is the bottleneck — the
   statement-level graph is a bigger index, so this fallback path becomes
   more important, not less, as R11 is pursued.

## Implementation plan

**Phase 1 — schema extension (~2–3 days)**
- Add statement-level def-use tables to `codegraph.db` (new tables, not a rewrite of existing `callers`/`callees`), populated during the same indexing pass `codegraph.py` already runs, guarded behind a build-time flag since it meaningfully increases index size/build time.

**Phase 2 — iterative query API (~2 days)**
- Add a stateful query interface to `GraphToolBackend` (e.g. `graph_session.follow(edge_type)`) so an agent can chain queries within one repair attempt instead of one fixed neighborhood computed up front.
- Wire `repair_agent.py` to use it when localization confidence is low (i.e. the first-pass neighborhood didn't contain an obvious suspect).

**Phase 3 — benchmark against ARISE's own delta (~1–2 days)**
- Measure pass@1 on `benchmarks/` cases before/after Phase 1+2; ARISE reported +4.7 points over flat navigation — use that as the target to beat or at least approach, and record the actual number here.

**Phase 4 — fallback path check (~0.5 day)**
- Confirm `ripgrep_tool_backend.py` still functions as a documented escape hatch for repos too large to build the extended index, updating its docstring to mention the tradeoff explicitly.

## Related
- [[Environment-Bootstrap]] — same survey, core-capability angle
- [[Environment-Bootstrap]] — navigation UX on top of this index
