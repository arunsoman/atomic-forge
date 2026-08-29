# CIE + forge: fixing real open-source bugs (and generating the tests for them)

[![tests](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml/badge.svg)](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-BSL--1.1-blue.svg)](../../LICENSE) [![discussions](https://img.shields.io/github/discussions/kannamma-labs/atomic-forge)](https://github.com/kannamma-labs/atomic-forge/discussions)

A second cross-tool benchmark, alongside
[`docs/cie-graph-bugfix-benchmark.md`](cie-graph-bugfix-benchmark.md) (which
measured CIE-vs-no-CIE token cost on one planted bug). This one answers two
questions on **real bugs from real, actively-maintained open-source repos
with many open issues**:

1. **Can forge's repair loop, backed by CIE as an MCP code-graph server,
   fix real bugs?** (CIE for localization + blast-radius; forge for the
   sample → execution-select → gate → commit loop.)
2. **Can CIE generate a VALID regression test from just a bug
   description** — valid meaning it *fails on the buggy code* (reproduces
   the bug) and *passes on the real fix* (correct oracle, not a false one)
   — and then can forge fix the bug against that CIE-generated test?

Every number below is from a real run of
[`benchmarks/cie_forge_realbugs/forge_cie_bench.py`](../benchmarks/cie_forge_realbugs/forge_cie_bench.py)
and
[`benchmarks/cie_forge_realbugs/cie_testgen_bench.py`](../benchmarks/cie_forge_realbugs/cie_testgen_bench.py)
against a live Ollama tool-calling model (`qwen3.5:cloud`). Raw per-case
JSON is checked in at
[`benchmarks/results/cie_forge_realbugs.json`](../benchmarks/results/cie_forge_realbugs.json)
and
[`benchmarks/results/cie_testgen_realbugs.json`](../benchmarks/results/cie_testgen_realbugs.json).

## The bugs (real, merged, permissively-licensed)

Four real merged bug-fix PRs from two small, stdlib-only, MIT-licensed
repos with many open bug issues — chosen so each buggy function extracts
to a standalone `mod.py` + `test_mod.py` that runs with just pytest:

| case | repo | fix commit | kind |
|---|---|---|---|
| `boltons_bits_offbyone` | mahmoud/boltons | `c1c25da` | off-by-one math bound (`>` should be `>=`) |
| `boltons_singularize_ss` | mahmoud/boltons | `1e61524` | missing branch (`singularize('glass')`→`'glas'`) |
| `mi_sliced_negative` | more-itertools/more-itertools | `958990e` | missing input guard (negative `n`) |
| `mi_running_min_stability` | more-itertools/more-itertools | `d992be0` | algorithmic stability (strict `<`→`<=`) |

Each `seed/mod.py` is the **verbatim pre-fix** function; `seed/mod_fixed.py`
is the **real fix** (kept as the oracle reference); `seed/test_mod.py` is
the real PR's regression test. Provenance is in each `case.json`.

## How CIE + forge are wired (no change to forge)

forge's `ToolBackend` protocol is satisfied by an `MCPToolBackend`
(living in the harness, not forge) that relays each graph call —
`callers`, `affected_by`, `failing_context`, `file_skeleton`,
`search_symbol`, `view_file`, `reindex_file`, … — to a **real
`cie-mcp --embedded` subprocess over stdio** (CIE as an MCP server, the
same way Claude Code/Cursor would use it). A background event-loop thread
bridges forge's synchronous repair loop to the async MCP client. CIE
indexes the case first (`cie index`), so the graph is "fully aware" before
the agent starts. forge itself is **unmodified**; the integration is one
adapter class in the benchmark harness.

## Experiment A — fix with the real PR's regression test

The agent gets the real PR's regression test as the oracle; CIE+forge must
fix the source so the suite goes green. Ground-truth re-checked by the
harness (not self-reported).

| case | green | rounds | failures | LLM calls | tokens | wall |
|---|:---:|:---:|:---:|:---:|---:|---:|
| boltons_bits_offbyone | ✅ | 1 | 1→0 | 7 | 22,287 | 25.7s |
| boltons_singularize_ss | ✅ | 1 | 1→0 | 11 | 45,234 | 40.2s |
| mi_sliced_negative | ✅ | 1 | 1→0 | 14 | 56,000 | 48.9s |
| mi_running_min_stability | ✅ | 1 | 2→0 | 8 | 31,116 | 33.6s |

**4/4 fixed green, each in 1 round.** Every fix matched the real PR's fix
exactly (verified by diffing the patched `mod.py` against
`mod_fixed.py`), and the tests were left untouched.

## Experiment B — CIE generates the test, then forge fixes

Now the harness gives the agent **only a natural-language bug
description** (no test). A CIE-grounded agent uses the graph tools
(`file_skeleton`, `view_file`, `search_symbol`, `callers`) to find the
exact function signature and behavior, then writes `test_mod.py` and
self-checks it **fails on the buggy code**. The harness then independently
validates the generated test as an oracle — measured, not asserted:

- **fails on the buggy `mod.py`** (reproduces the bug — assertion failure,
  not a collection/import error), AND
- **passes on the real `mod_fixed.py`** (correct, not a false oracle).

Only if both hold is the test counted as a **valid oracle**; then forge's
repair loop runs against it.

| case | test gen | fails on buggy | passes on fix | **valid oracle** | gen calls | gen tok | repair green | repair tok |
|---|:---:|:---:|:---:|:---:|:---:|---:|:---:|---:|
| boltons_bits_offbyone | ✅ | ✅ | ✅ | **✅** | 4 | 9,611 | ✅ | 15,990 |
| boltons_singularize_ss | ✅ | ✅ | ✅ | **✅** | 5 | 9,728 | ✅ | 20,667 |
| mi_sliced_negative | ✅ | ✅ | ✅ | **✅** | 5 | 9,892 | ✅ | 25,210 |
| mi_running_min_stability | ✅ | ✅ | ✅ | **✅** | 4 | 11,467 | ✅ | 71,333 |

**CIE generated a valid oracle for 4/4 bugs (including the subtle
type-stability case, where it correctly reached for `Fraction` and pinned
`min(x,y)` returning the left operand on equality), and forge+CIE fixed
4/4 green against those generated tests.**

The hardest generated test (`mi_running_min_stability`) is reproduced in
the appendix below — it is a genuine, grounded regression test, not a
degenerate pass/fail.

## What CIE actually contributed to test generation

The generated tests are grounded by CIE's graph, not guessed:

- `file_skeleton` / `search_symbol` → the **exact** function name and
  signature, so `from mod import Bits` / `running_min` compiles instead of
  hallucinating a method that doesn't exist.
- `view_file` → the real implementation, so the assertion targets the
  actual buggy branch (e.g. the `else: word[:-1]` in `singularize`, the
  `val > 2 ** len_` guard in `Bits`) rather than an invented one.
- `callers` / `affected_by` → the function's real usage, so the test
  reflects how the function is actually called (e.g. `Bits(...).as_bin()`
  round-trips; `sliced('ABCDEF', 3)` still works after the negative guard).

The validity gate (fails-on-buggy AND passes-on-real-fix) is what makes
this honest: a test that fails for the wrong reason (a syntax error, a
hallucinated symbol) is rejected, not counted.

## forge can now raise the PR too

This benchmark also lands forge's last-mile capability: after a green
repair, `atomic-forge repair --raise-pr` pushes the fix on a fresh branch
to `origin` and opens a GitHub pull request with the `gh` CLI (see
[`src/atomic_forge/pr.py`](../src/atomic_forge/pr.py),
[`tests/test_pr.py`](../tests/test_pr.py)). It never force-pushes and never
commits onto the default branch — only a feature branch + a PR. So the
loop is now: **CIE localizes → forge patches → forge commits → forge
opens the PR**, end to end.

## Reproduce

```bash
# one-line install (forge + CIE + pytest), then run either experiment:
pip install git+https://github.com/kannamma-labs/atomic-forge.git \
            git+https://github.com/arunsoman/cie.git pytest

# Experiment A — fix with the real PR's test:
python benchmarks/cie_forge_realbugs/forge_cie_bench.py            # all 4 cases
python benchmarks/cie_forge_realbugs/forge_cie_bench.py boltons_bits_offbyone  # one case

# Experiment B — CIE generates the test, then forge fixes:
python benchmarks/cie_forge_realbugs/cie_testgen_bench.py
```

Each harness indexes the case with `cie index`, spawns `cie-mcp --embedded`
over stdio, and runs forge's real `repair_loop_agentic`. Cases ship next to the
script (`cases/<id>/seed/`); work dirs and result JSON go under your temp dir
(override with `BENCH_WORK_DIR` / `BENCH_OUT`). The LLM is configured via forge's
standard env vars (`FORGE_MODEL` / `FORGE_BASE_URL` / `FORGE_API_KEY`) or Ollama's
(`OLLAMA_BASE_URL` / `OLLAMA_MODEL`); default is a local Ollama `qwen2.5:7b`. Use
any tool-calling OpenAI-compatible model — the recorded run used a larger cloud
model, so absolute token numbers will differ from the table above.

## Honest caveats

- **N = 4, one model, one run per case.** A single measured data point per
  case, not a statistically significant claim. LLM runs vary; the checked-in
  JSON is the exact run reported, not a cherry-picked best.
- **The cases are deliberately tractable** (single-function, stdlib-only,
  small) so the buggy code extracts to a standalone module — this isolates
  the CIE+forge loop from repo-setup noise, but it also means these are
  not the hardest bugs in those repos. The hardest case here
  (`running_min_stability`) is the algorithmic-stability one, and it was
  both generated-and-fixed correctly.
- **Experiment B's "valid oracle" gate is strict but not complete.** A
  generated test that happens to fail on the buggy code for an unrelated
  reason AND pass on the fix would still score "valid"; the gate catches
  hallucinated symbols / collection errors / tests that don't reproduce the
  reported bug, but cannot prove a test captures *exactly* the intended
  invariant. The four generated tests were inspected and do target the
  reported behavior.
- **Test generation used CIE for grounding, but the model still did the
  reasoning.** CIE made the test *well-formed and on-target*; the
  correctness of the assertion itself is the model's, validated by the
  fails-on-buggy / passes-on-fix gate, not by CIE.

The narrow claim this supports: **for these four real bugs from two
permissively-licensed repos, CIE (as an MCP code-graph server) +
forge's repair loop fixed all four from the real PR's test, and CIE
generated a valid regression test for all four from just the bug
description, which forge then fixed — with forge now able to open the PR.**

## Appendix — the CIE-generated test for the hardest case

`mi_running_min_stability` (generated from the bug description, no test
provided; it correctly uses `Fraction` and pins `min`/`max` returning the
left operand on equality):

```python
"""Regression test for running_min/running_max type stability bug."""
import pytest
from fractions import Fraction
from mod import running_min, running_max


def test_running_min_type_stability():
    """running_min must preserve type stability matching plain min()."""
    data = [0, 0.0, Fraction(0)]
    maxlen = 2
    expected_types = [
        type(min(data[0:1])),
        type(min(data[0:2])),
        type(min(data[1:3])),
    ]
    result = list(running_min(data, maxlen=maxlen))
    actual_types = list(map(type, result))
    assert actual_types == expected_types, f"Expected {expected_types}, got {actual_types}"
```

This fails on the buggy code (`<` drops the incumbent → type flips to
`float`) and passes on the real fix (`<=` keeps the incumbent → `int`),
so it is a valid oracle — and forge fixed the bug against it.