# Asides — short companion notes

[![tests](https://github.com/arunsoman/atomic-forge/actions/workflows/test.yml/badge.svg)](https://github.com/arunsoman/atomic-forge/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-MIT-blue.svg)](../LICENSE) [![discussions](https://img.shields.io/github/discussions/arunsoman/atomic-forge)](https://github.com/arunsoman/atomic-forge/discussions)

Short, opinionated notes that sit *alongside* the main docs — the *why*
behind decisions, and the gotchas that aren't obvious from the README. None
of this repeats [`README.md`](../README.md) or the
[benchmark reports](./); it's the margin commentary.

---

## Execution-select, not model-judge-select

When the repair loop samples K candidate patches, it picks the winner by
**running the test suite**, not by asking the model "which one looks right?".
A patch that a model rates highly can still break a caller three files away;
only execution catches that. This is the whole point of the blast-radius gate
too — a "passing" patch that silently regresses a non-tested caller is
rejected. If you ever feel tempted to swap execution-select for a
"model-as-judge" pass to save latency: don't. The latency is the correctness.

## One patch engine, on purpose

There is exactly one SEARCH/REPLACE parser (`patch.py`). Every path that
mutates a file — the agentic loop, the generator fast-path, the repair
loop — goes through it. A second parser means two truths about what a
"valid edit" is, and they drift. While building the benchmark suite we found
a latent bug in its hunk-disjointness check: it used a pairwise-adjacent
overlap test that missed interval *containment* and *straddle*, so two
non-adjacent hunks that overlap could both apply. It's now a running-max
sweep-line with regression tests
([`tests/test_patch.py`](../tests/test_patch.py)). The lesson: even the
"obviously correct" parts of the one canonical engine need tests.

## Checkpoint *before*, not after

Every phase transition writes a SQLite row **before** the work starts, then
marks it done after. So a crash mid-write leaves a record that the file was
*in flight*, and resume re-hashes disk and regenerates only what actually
changed or went missing — never an all-or-nothing restart. If you add a
new phase, wire it through `checkpoint.py` the same way; don't do
"checkpoint on success only", or a crash drops the in-flight unit entirely.

## Exactly one file per `AtomicTask`

An `AtomicTask` is *exactly one file* with a required `test_triad`. This
isn't a stylistic preference — it's what makes the blast-radius gate
tractable. One file in means the set of *other* files that depend on it is
small and enumerable; the gate can re-run the callers and reject a patch
that passes its own triad but breaks a dependent. Two files per task and the
"did I break a caller?" check becomes a multi-file diff-and-guess. Keep the
contract at one file; decompose bigger work into multiple tasks.

## `test_triad`: negative + recovery, not just happy path

A triad is positive / negative / recovery. The **negative** case is the one
people skip and shouldn't: it's the assertion that the function *fails the
right way* on bad input (raises, returns the sentinel, etc.). A triad where
the negative case is missing is a triad where the oracle can't distinguish
"fixed" from "silently swallowed the error". **Recovery** exists so the
post-failure state is checked, not just the failure. When you write a triad,
ask: would this fail if the implementation returned `None` instead of raising?

## CIE as an MCP server, not in-process

forge can use [CIE](https://github.com/arunsoman/cie) as its code-graph
backend, but the integration is deliberately **out-of-process**: CIE is
served as a real MCP server over stdio, and a thin adapter satisfies forge's
`ToolBackend` protocol. Two consequences worth knowing:

- **forge itself is unchanged** — the benchmark harnesses ship the adapter;
  the library never imports CIE. You can run the exact same CIE+forge loop
  from Claude Code or Cursor, because they consume the same MCP surface.
- **Reproducibility** — the graph is built up front (`cie index`), so the
  agent starts with a "fully aware" graph rather than building it lazily
  mid-repair, which would make token costs nondeterministic.

See [`cie-forge-realbug-benchmark.md`](cie-forge-realbug-benchmark.md).

## Standalone seed cases (and the pytest gotcha)

Benchmark cases are extracted to a standalone `mod.py` + `test_mod.py` that
run with just pytest — no repo checkout, no deps beyond stdlib. This is what
makes the loop reproducible and cheap to run. The cost: each case's test
file is named `test_mod.py`, and under pytest's rootdir import they collide
when collected together. That's why `pyproject.toml` pins
`testpaths = ["tests"]` — run `python -m pytest` from the repo root, and
invoke the benchmark harnesses directly (they manage their own collection).

## `.cie/` is gitignored for a reason

`cie index` produces a SQLite graph DB that, on a real project, runs into
hundreds of MB. It's in `.gitignore`. Never `git add` it; if your `git
status` suddenly looks quiet, check that the ignore is still there. A
stale `.cie/` from a different branch is harmless (CIE rebuilds), but a
committed one bloats the repo forever (git doesn't forget).

## Token cost: CIE vs no-CIE, with caveats

The one controlled cost measurement
([`cie-graph-bugfix-benchmark.md`](cie-graph-bugfix-benchmark.md)): on a
single mathematically-subtle planted bug, the CIE-backed agent fixed it in
about **63% fewer tokens** and went green; the same agent *without* the graph
broke the suite and hit the round cap. Treat that as **one data point**, not
a law — N=1, one model, one bug. The real-bug suite (4 cases) is the broader
signal, and even that is N=4, one model, one run each. The honest claim is
"for these bugs, with this model, CIE helped a lot", not "CIE always saves
63%".

## The benchmark is measured, not asserted

Both benchmark harnesses re-check green **against the real merged fix**
(`mod_fixed.py`), not against the agent's own self-report. The CIE-generated
tests are validated as oracles the same way: a generated test only counts
as "valid" if it **fails on the buggy code** (reproduces the bug — not a
collection error, not a hallucinated symbol) **and passes on the real fix**.
A test that fails for the wrong reason is rejected, not counted. This is
what makes the "CIE generated 4/4 valid tests" number mean something.

## How to read these claims

The narrow, defensible claim this repo supports: *for four real bugs from
two permissively-licensed repos, CIE (as an MCP code-graph server) +
forge's repair loop fixed all four from the real PR's test, CIE generated a
valid regression test for all four from just the bug description, and forge
then fixed all four against those generated tests.* Broader generalizations
need more bugs, more models, and more runs — which is exactly the kind of
contribution [`CONTRIBUTING.md`](../CONTRIBUTING.md#adding-a-benchmark-case)
asks for.