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

## Real-issue PR campaign

Separate from the curated `cases/` above: `benchmarks/real_issues/` runs
forge against **live, open, well-documented GitHub issues** (curated by
`curate.py`/`curate_round2.py` — open, bug-flavored, has a repro signal;
pre-screened for PR-writable upstreams via `pr_writable.py`) with
`atomic-forge fix <issue-url> --raise-pr`, end to end: clone → bootstrap
gate → CIE-generated regression test → repair loop → fork + PR against
the issue's own upstream repo. Every attempt (pass or fail) is logged to
`real_issues/sweep/results_round2.jsonl`, resumable.

Round 2 (2026-08-29): 55 of 61 curated issues attempted, 3 landed a
verified fix and a PR:

| Repo | Issue | PR | Fork | Status |
|---|---|---|---|---|
| [python-babel/babel](https://github.com/python-babel/babel) | [#1219](https://github.com/python-babel/babel/issues/1219) | [babel#1334](https://github.com/python-babel/babel/pull/1334) | `kannamma-labs` | open |
| [jd/tenacity](https://github.com/jd/tenacity) | [#531](https://github.com/jd/tenacity/issues/531) | [tenacity#705](https://github.com/jd/tenacity/pull/705) | `kannamma-labs` | open |
| [Delgan/loguru](https://github.com/Delgan/loguru) | [#1501](https://github.com/Delgan/loguru/issues/1501) | [loguru#1505](https://github.com/Delgan/loguru/pull/1505) | personal fork | closed by maintainer, unmerged — not resubmitted |

(The babel/tenacity PRs were originally opened from a personal fork,
then migrated to org-owned forks under `kannamma-labs` — same commits,
new PR — once the org fork setup was in place; the closed originals link
to their replacements. loguru's PR predates the org migration and was
left as-is once the maintainer closed it, out of respect for that
decision — all future runs raise PRs from `kannamma-labs` forks only.)

Remaining 52 attempts: 27 `oracle_reject` (CIE couldn't confirm the bug
reproduces at HEAD within budget), 23 `repair_fail` (reproduced, but the
repair loop didn't land a green fix within its round budget), 2
`bootstrap_fail`. Two real forge bugs were found and fixed along the way
(see `src/atomic_forge/bootstrap.py`/`stacks.py`): a base-image picker
that asked an LLM before trusting unambiguous repo markers, and a bare
dev-convenience `Makefile` false-positiving as a C/C++ signal on pure
Python repos (`benoitc/gunicorn`) — both covered by regression tests in
`tests/test_bootstrap_agentic.py`.

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
