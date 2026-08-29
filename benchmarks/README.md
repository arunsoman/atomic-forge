# Benchmarks

[![tests](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml/badge.svg)](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-BSL--1.1-blue.svg)](../LICENSE) [![discussions](https://img.shields.io/github/discussions/kannamma-labs/atomic-forge)](https://github.com/kannamma-labs/atomic-forge/discussions)

Methodology and harness for validating the claims in the README's "Why
this exists" section — every result is produced by actually running
`atomic-forge` against a live LLM endpoint; nothing here is simulated.

> **Cross-tool benchmark (separate from the cases below):**
> [`docs/cie-graph-bugfix-benchmark.md`](../docs/cie-graph-bugfix-benchmark.md)
> + [`measure_cie_graph_benefit.py`](measure_cie_graph_benefit.py) measure a
> *different* question — does handing a tool-calling agent a real code graph
> (CIE over MCP) change the token/turn/success cost of fixing one
> mathematically-subtle bug vs. plain filesystem tools. It is not the
> atomic-forge repair loop; the cases below are.

## Methodology

Each case in `cases/<id>/` is a **real merged bug fix** from a small,
permissively-licensed open-source Python repo:

- `case.json` — provenance: repo, PR URL, base/merge commit SHAs.
- `seed/` — the **verbatim pre-fix source** of the buggy function(s),
  extracted to a small standalone module (so the run doesn't have to pull
  in an entire multi-thousand-line real file and its full dependency
  tree), plus the **real test file** from that commit (including the new
  regression test the real PR added).
- `task.json` — a hand-written `AtomicTask` describing the bug, in the
  same shape any user would author for their own project.

`benchmarks/run_case.py` copies `seed/` into a fresh temp dir and runs
`atomic-forge repair --tasks task.json --project-dir <tmp>` — the real
CLI, as a subprocess, exactly as a user would invoke it. This is
deliberately **`repair`-only**, not the full `run` pipeline: `seed/`
already contains the real (buggy) file and the real (currently-failing)
test, so this isolates and measures the repair loop specifically —
localize → sample → select → gate — against a real bug and a real
oracle, not a test the model itself just wrote.

A case is scored **PASS** only if the real test suite (including the
pre-existing tests, not just the new regression test) is green at the
end — a fix that passes the new test by breaking something else does not
count.

Recorded per case: resolution (pass/fail), repair rounds, blast-radius
gate reject count (parsed from the run's own `.forge/trajectory.jsonl`,
not self-reported), LLM calls/tokens, wall-clock time.

## Running the benchmarks

`python benchmarks/run_case.py --all` (needs
`FORGE_API_KEY`/`FORGE_BASE_URL`/`FORGE_MODEL` set, or `OPENAI_API_KEY`).
Raw per-case JSON (including stdout/trajectory excerpts) lands in
`benchmarks/results/<case_id>.json` (git-ignored — regenerate locally).
`benchmarks/build_results_table.py` turns those into a results table.

## Resume speedup

`benchmarks/measure_resume.py` runs the exact pattern documented in the
main README's "Resumable runs" section (`RunCheckpointer` +
`diff_file_hashes`) for real: generate a batch cold, simulate one file
going stale, and time regenerating only that file vs. the full cold run.

## Adding a case

1. Find a real, small, merged bug-fix PR in a permissively-licensed
   Python repo — single function, has its own regression test.
2. `mkdir benchmarks/cases/<id>/seed`, extract the pre-fix function(s) +
   real test(s) verbatim (trim surrounding file if needed for
   tractability, but never rewrite the buggy logic or the test itself).
3. Verify locally that the real test fails against the seed as extracted
   (`pytest seed/test_mod.py`) — if it doesn't fail, the case doesn't
   test anything.
4. Write `task.json` (one `AtomicTask`, `action: "modify"`) and
   `case.json` (provenance + a short note).
5. `python benchmarks/run_case.py <id>`.
