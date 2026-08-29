# CIE code-graph bugfix benchmark — with vs. without a code graph

[![tests](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml/badge.svg)](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-BSL--1.1-blue.svg)](../../LICENSE) [![discussions](https://img.shields.io/github/discussions/kannamma-labs/atomic-forge)](https://github.com/kannamma-labs/atomic-forge/discussions)

A *cross-tool* benchmark, deliberately separate from
[`benchmarks/README.md`](../benchmarks/README.md) (which measures
atomic-forge's *own* repair loop). This one asks a different question:
**does handing a tool-calling LLM agent a real code graph (CIE, served as
an MCP server) change how much it costs — in tokens, turns, and
success — to fix one mathematically-subtle bug with a multi-file blast
radius, compared to the same agent with only plain filesystem tools?**

Every number below is from a real run of
[`benchmarks/measure_cie_graph_benefit.py`](../benchmarks/measure_cie_graph_benefit.py)
against a live Ollama tool-calling model. Raw per-turn JSON (tool traces,
token counts, final test output) is checked in at
[`benchmarks/results/cie_vs_no_cie.json`](../benchmarks/results/cie_vs_no_cie.json).

## The bug (real, now fixed + regression-tested)

`src/atomic_forge/patch.py::validate_hunk_disjointness` is the preflight
that rejects a SEARCH/REPLACE patch whose hunks target overlapping
regions of the original file. Its job is interval-overlap detection.

The **planted bug** is a pairwise-**adjacent** overlap check:

```python
located.sort(key=lambda o: o.span[0])
for a, b in zip(located, located[1:]):
    if a.span[1] > b.span[0]:   # only compares each span to the NEXT one
        conflicting_ids.add(id(a)); conflicting_ids.add(id(b))
```

This is **mathematically wrong for the interval-containment + straddle
case**. Given three spans sorted by start — `A=[0,14]` (a container),
`B=[2,4]` (nested inside `A`), `C=[7,14]` (straddles `A` but starts after
`B` ends) — the adjacent pairs are `(A,B)` → overlap *caught*, and
`(B,C)` → `4 > 7` is false, so `C` is **never compared against `A`** and
slips through as "clean" even though `C.start=7 < A.end=14`. The wrongly-
"clean" hunk then gets applied over `A`'s region, corrupting the output.

The **correct fix** is a sweep-line tracking the running maximum end:

```python
max_end = -1; max_end_owner = None
for o in located:
    if o.span[0] < max_end:                 # overlaps the widest still-open span
        conflicting_ids.add(id(o))
        if max_end_owner is not None: conflicting_ids.add(id(max_end_owner))
    if o.span[1] > max_end:
        max_end = o.span[1]; max_end_owner = o
```

The regression test
(`tests/test_patch.py::test_nested_then_straddling_hunks_all_conflict`)
constructs exactly the container/nested/straddle shape and asserts all
three are flagged (so none apply). This bug was a real latent defect in
this repo's own `patch.py`; it is now fixed and regression-tested there.
The benchmark replays the *pre-fix* state.

## Why this bug is the interesting one for a code-graph benchmark

The fix itself is small. The **cost is in the exploration**, and that is
exactly where a code graph pays off:

- **Locating** the failing logic inside a 450-line `patch.py` — a graph
  gives `file_skeleton` (signatures only) and `search_symbol` (one call,
  ~480 chars) instead of reading the whole file.
- **Blast radius**: `validate_hunk_disjointness` is called by
  `apply_hunks` → `apply_search_replace` → consumed by `repair.py`,
  `generate_agent.py`, and `repair_agent.py`. A correct fix must not
  break those callers (e.g. must not rename the function). The graph's
  `callers()` answers "who calls this?" in **one 421-char call**. With
  only filesystem tools, the agent has to grep/read three more files —
  or, as actually happened, *not check at all* and break the call site.

## Methodology

- **Model**: `qwen3.5:cloud` via a local Ollama OpenAI-compatible endpoint
  (tool-calling / function-calling). One model, identical system prompt
  and seed task for both cases.
- **The agent**: a plain tool-calling loop (`measure_cie_graph_benefit.py`),
  not atomic-forge's own pipeline — to isolate the *code graph* variable
  from atomic-forge's repair loop. Up to 22 turns per case.
- **Case 1 — WITH CIE**: the agent gets 7 CIE graph tools served by the
  real `cie-mcp --embedded` MCP server over stdio (`affected_by`,
  `callers`, `callees`, `search_symbol`, `file_skeleton`, `path_between`,
  `view_file`) plus `edit_file` and `run_tests` — 9 tools total. CIE had
  indexed the buggy project first (`cie index`), so the graph was "fully
  aware" of the code before the agent started.
- **Case 2 — NO CIE**: the same agent, same model, same prompt, same
  buggy project copy — but only plain filesystem tools (`read_file`,
  `list_dir`, `grep`) plus `edit_file` and `run_tests` — 5 tools. No
  graph; the agent must explore by reading files.
- **Both start from an identical buggy copy** with the same failing
  test; the buggy file is reset before each case.
- **Success criterion**: the *real* test suite `tests/test_patch.py`
  (14 tests) is green at the end — a fix that passes the new regression
  test by breaking the existing ones does not count (this is the same
  rule the main benchmark suite uses).

## Results

| metric                       | WITH CIE       | NO CIE          |
|------------------------------|---------------:|----------------:|
| test suite green at end      | **yes** ✅      | **no** ❌ (11 failed, 3 passed) |
| agent turns used             | 12             | 22 (hit the cap) |
| LLM calls                    | 12             | 22              |
| tool calls                    | 11             | 22              |
| prompt tokens                | 79,494         | 222,920         |
| completion tokens            | 3,471          | 1,289           |
| **total tokens**             | **82,965**     | **224,209**     |
| wall time                    | 62.5 s         | 51.3 s          |
| tools exposed                | 9 (7 graph)   | 5 (0 graph)     |

**Token delta: +141,244 (+63.0% more without CIE)** — and the no-CIE run
did not converge at all.

### What each agent actually did

**WITH CIE** (11 tool calls, green): surgical exploration —
`view_file` on windowed line ranges of `test_patch.py` and `patch.py`,
`search_symbol` to jump to `apply_hunks` then `validate_hunk_disjointness`
(~480 chars each), then **`callers(validate_hunk_disjointness)`** — one
421-char call that listed every call site. Having seen the blast radius,
the agent edited **only the function body** (kept the name), ran the
tests once, and finished green.

**NO CIE** (22 tool calls, broken): brute exploration — it `read_file`'d
the **entire 450-line `patch.py` seven times** (24,538 chars each ≈
~170k characters of re-reading the same file), grepped repeatedly, ran
the tests, and then made a fatal mistake: it **renamed the function to
`validate_hunk_disjointness_DEBUG`** without updating the call site in
`apply_hunks` — a `NameError` that broke **11** tests. Because it had no
graph, it never asked "who calls this?" before renaming. The remaining
turns were greps and re-reads trying to recover; it hit the turn cap
still broken. The suite went from 1 failing / 13 passing (the starting
state) to 11 failing / 3 passing — worse than where it started.

## Reproduce

```bash
# prereqs: Ollama running locally with a tool-calling model; the `cie`
# package importable; this repo's venv has atomic_forge + pytest.
export BENCH_MODEL=qwen3.5:cloud
export CIE_ROOT=/path/to/cie        # where the cie/ package lives

# 1. build the buggy throwaway copy + failing test
python benchmarks/measure_cie_graph_benefit.py --setup

# 2. index it with CIE (so the graph is "fully aware" before case 1)
PYTHONPATH=$CIE_ROOT python -m cie.cli index "$BENCH_PROJECT" --db "$CIE_DB"

# 3. run both cases; writes benchmarks/results/cie_vs_no_cie.json
python benchmarks/measure_cie_graph_benefit.py
```

All paths are env-overridable (`BENCH_PROJECT`, `CIE_DB`, `CIE_ROOT`,
`BENCH_MODEL`, `OLLAMA_BASE_URL`, `VENV_PY`, `BENCH_MAX_TURNS`); the
defaults match the run that produced the checked-in results.

## Honest caveats (read these before quoting a number)

- **N = 1.** One run, one model, one bug. This is a single measured
  data point, not a statistically significant claim. LLM runs vary; a
  re-run could move the no-CIE run onto a better or worse path. The
  checked-in JSON is the exact run reported, not a cherry-picked best.
- **The no-CIE failure is partly a model mistake** (renaming the
  function), not pure token math. That is the *point* — the graph's
  `callers()` tool is what made the CIE agent check the blast radius
  before editing, and the absence of that tool is what let the no-CIE
  agent break its own call site. The token gap and the success gap have
  the same root cause: visibility into the call graph.
- **Tool counts differ (9 vs 5).** CIE adds graph-query tools; that is
  the intervention being measured. The graph-tool *schemas* do add some
  per-turn prompt overhead, yet the CIE run still used ~63% fewer total
  tokens — because surgical queries replaced seven full-file reads.
- **The bug was planted** (a real latent bug class in this repo's own
  `patch.py`, now fixed and regression-tested). It is not a sampled
  external PR like the cases in `benchmarks/cases/`; it is a controlled
  defect chosen to exercise exactly the localization + blast-radius work
  a code graph is built for.
- **Wall time is noisy.** The no-CIE run was *faster* in wall time
  (51.3 s vs 62.5 s) despite using far more tokens, because it issued
  more short, parallelizable tool calls and never converged — speed
  without success is not the win being measured here.

The claim this benchmark supports is narrow and specific: **for this
one mathematically-subtle bug with a three-file blast radius, a code
graph (CIE over MCP) let a tool-calling agent fix it correctly in fewer
turns and ~63% fewer tokens, where the same agent without the graph
broke the suite and did not converge.** It is one honest data point, not
a generalization.