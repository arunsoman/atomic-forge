# R1 — Agent-computer interface for code navigation/edit/exec

**Requirement:** Provide a purpose-built agent-computer interface (structured
navigate/view/edit/execute commands) instead of raw shell access, so the LM's
actions are constrained and parseable.

**Sourced from:** SWE-agent (Princeton/Stanford) — Agent-Computer Interface (ACI).

**Status in atomic-forge:** **Met — verified against code 2026-08-29.**
`tools.py`'s own docstring literally calls it "forge's agent-computer
interface": `view_file` is already windowed (~100 lines/call, explicit
truncation + hint), every response is a self-describing envelope
(`ok`/`results`/`truncated`/`hint`), `describe()` self-introspects the
backend's public methods into a manifest (no hand-maintained tool list to
drift), and `repair_agent.py`'s `_attempt_patch.check()` already runs
`lint_gate` on every candidate edit before it's accepted — i.e. Phases 1–3
below were already done. Only a minor convenience (`view_window` by
center+radius instead of explicit start/end) was missing; added below.

**✅ IMPLEMENTED 2026-08-29:** `view_window(path, center_line, radius)`
added to the `ToolBackend` protocol and both bundled backends
(`LocalToolBackend`, `GraphToolBackend`), plus `examples/
ripgrep_tool_backend.py` (kept protocol-conformant per its own
`test_describe_lists_full_protocol` test). Tests added:
`test_tools_local.py::test_view_window_centers_on_line`,
`test_view_window_clamps_start_below_one`, `test_view_window_on_graph_backend`.
Full suite green. **R1 is now fully done — nothing left open.**

## State of the art

- **SWE-agent: Agent-Computer Interfaces Enable Automated Software
  Engineering** (Yang et al., [arXiv:2405.15793](https://arxiv.org/abs/2405.15793),
  NeurIPS'24) — the foundational result. A constrained, LM-friendly
  command/feedback surface (not raw shell) raised SWE-bench pass@1 well
  above prior non-interactive baselines. Specific ACI design choices that
  mattered as much as the underlying model:
  - a windowed file viewer (bounded context per view, not whole-file dumps)
  - linting run automatically on every edit, with errors fed straight back
  - a concise, LM-parseable error format instead of raw stack traces/shell
    output

## Implication for atomic-forge

The gap between forge's current `ToolBackend` (symbol lookups only) and a
full ACI is the edit/execute half: there's no equivalent of "lint on every
edit" or a windowed viewer feeding the model bounded, navigable context.
`patch.py`'s SEARCH/REPLACE normalization chain solves a different problem
(robust patch application) than the ACI's problem (giving the model a
*better interface* to avoid generating a bad edit in the first place). These
are complementary, not redundant — an ACI-style edit-time lint pass would
sit upstream of `patch.py`, catching a class of errors before they ever
reach the normalization/disjointness preflight.

## What needs to be done (to beat the competition)

1. **Windowed file viewer.** Add a `view_window(file, center_line, radius)`
   method to `ToolBackend` that returns a bounded slice (matching
   SWE-agent's ACI design), not the whole file. Wire `repair_agent.py` to
   request windows around suspect lines (from traceback/blast-radius
   evidence it already computes) instead of loading full files into the
   prompt.
2. **Lint-on-edit, before the patch is even scored.** Run the repo's linter
   immediately after `patch.py` applies a hunk, *before* the test suite
   runs. Feed lint errors back in the same structured format as test
   failures so a bad edit is caught and retried within the same K-attempt
   budget, not discovered downstream.
3. **Standardized, concise error format.** Replace raw pytest/traceback dumps
   fed to the model with a fixed schema (file, line, symbol, one-line cause)
   — SWE-agent's paper attributes real gains to this alone, independent of
   model quality. This slots into the same place `checkpoint.py`'s verdict
   taxonomy already captures failures.
4. **Benchmark it.** Add an ACI on/off toggle to `benchmarks/` and measure
   fix-rate delta on the existing suite before claiming the win — SWE-agent's
   own gains were empirically validated, not assumed from the interface
   design.

## Implementation plan

**Phase 1 — windowed viewer (spike, ~1–2 days)**
- Add `ToolBackend.view_window(file: str, center_line: int, radius: int = 40) -> str` to the protocol in `tools.py`; implement in both `LocalToolBackend` and `GraphToolBackend`.
- Wire `repair_agent.py`'s prompt construction to call `view_window` around traceback/blast-radius suspect lines instead of reading whole files.
- Success check: prompt token count drops on multi-hundred-line files in at least one existing `benchmarks/` case, with no fix-rate regression.

**Phase 2 — lint-on-edit (~1–2 days)**
- After `patch.py` applies a hunk and before the test suite runs, invoke the repo's configured linter (ruff/eslint, detected or configurable) on just the touched file(s).
- Feed lint failures back through the same structured-error path added in Phase 3, consuming one of the existing K attempts rather than a silent extra retry.
- Success check: a benchmark case seeded with an obviously-lintable bad edit gets caught and retried within budget instead of reaching the test-run phase.

**Phase 3 — standardized error schema (~1 day)**
- Define a small dataclass/schema (file, line, symbol, one-line cause) in `checkpoint.py` or a new `aci_format.py`; convert both lint failures (Phase 2) and pytest failures into it before they reach the prompt.
- Success check: manual diff of prompt contents before/after — same failure information, meaningfully fewer tokens.

**Phase 4 — benchmark and decide (~1 day)**
- Add an `--aci`/`--no-aci` toggle to the `benchmarks/` harness invocation.
- Run the full suite both ways; only keep the feature on by default if fix-rate is flat-or-better and token/cost usage drops.
- Document the result (even if negative) in this file's Status line.

## Related
- [[req-repo-scale-context]] — the retrieval/navigation half of the same problem
- [[req-enterprise-scale-indexing]] — ACI navigation at larger scale
