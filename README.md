# atomic-forge

[![release](https://img.shields.io/github/v/release/kannamma-labs/atomic-forge)](https://github.com/kannamma-labs/atomic-forge/releases/latest)
[![tests](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml/badge.svg)](https://github.com/kannamma-labs/atomic-forge/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-BSL--1.1-blue.svg)](./LICENSE)
[![python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue.svg)](https://www.python.org/downloads/)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-atomic--forge-blue.svg)](./action.yml)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/kannamma-labs/atomic-forge/compare)
[![discussions](https://img.shields.io/github/discussions/kannamma-labs/atomic-forge)](https://github.com/kannamma-labs/atomic-forge/discussions)
[![stars](https://img.shields.io/github/stars/kannamma-labs/atomic-forge?style=social)](https://github.com/kannamma-labs/atomic-forge/stargazers)

> **The reliable, test-driven repair engine you can drop under any coding agent.**
> (Issue → failing test → repair → PR — an agentic coding-engine library
> and CLI with a **machine-checked task contract**, **crash-safe resumable
> runs**, and patches selected by *executing the test suite* — not by
> asking a model which patch looks right.) a **machine-checked task
> contract**, **crash-safe resumable runs**, and a repair loop whose
> patches are selected by *executing the test suite* — not by asking a
> model which patch looks right.

- ✅ **Issue → PR in one command** — `atomic-forge fix <issue-url>`: fetch, bootstrap, generate a *failing regression test from the issue text*, repair, open a PR from your fork
- ✅ **Execution-selected repairs** — K sampled patches, selected by running the real suite, gated by a static blast-radius check before anything is committed
- ✅ **Crash-safe & resumable** — every phase checkpointed to SQLite *before* work starts; resume regenerates only what changed on disk
- ✅ **Statement-level code graph** — def-use at statement granularity, `ast`-exact for Python, honest `heuristic` tiers elsewhere
- ✅ **Bootstrap any repo** — 6 ecosystems detected deterministically; opt-in Repo2Run-style agentic fallback in a Docker sandbox for the rest
- ✅ **Bring your own everything** — LLM (any OpenAI-compatible endpoint), tool backend, deploy target, reporter — all protocols with real reference implementations
- ✅ **Measured, not asserted** — real merged open-source bug fixes, 4/4, scored by running each repo's own suite

## Quickstart

```bash
pip install git+https://github.com/kannamma-labs/atomic-forge.git

export FORGE_API_KEY=sk-... FORGE_BASE_URL=https://api.openai.com/v1 FORGE_MODEL=gpt-4o-mini
# or fully local: FORGE_MODEL=qwen3.5:cloud FORGE_BASE_URL=http://localhost:11434/v1 FORGE_API_KEY=ollama

# one-shot: issue → regression test → repair → PR from your fork
atomic-forge fix https://github.com/mahmoud/boltons/issues/123 --dry-run

# full pipeline from a task batch
atomic-forge run --tasks tasks.json --project-dir ./out
```

📄 **[Deep-dive docs live in the wiki](https://github.com/kannamma-labs/atomic-forge/wiki)** — quickstart, CLI reference, the repair loop, the bootstrap gate, benchmarks methodology, and the full R1–R16 requirements survey.

## Measured results

| | Result | How |
|---|---|---|
| Real open-source bugs (CIE + forge) | **4/4 fixed**, 1 round each, matching the merged PR | real regression tests from [more-itertools](https://github.com/more-itertools/more) & [boltons](https://github.com/mahmoud/boltons) |
| Regression tests generated from the issue text alone | **4/4 valid** (fail on buggy, pass on real fix) | same benchmark, second half |
| Code-graph (CIE) token cost | **~63% fewer tokens**, without the graph the agent didn't converge | planted subtle bug, same agent, A/B |
| Live GitHub issue campaign | **55 real issues attempted, 3 real PRs opened** against upstream (2 open, 1 closed by maintainer unmerged) | end-to-end `fix <issue-url> --raise-pr`, no cherry-picking — see [PR log](benchmarks/README.md#real-issue-pr-campaign) |

Harness + seeds: [`benchmarks/`](benchmarks/) — methodology in the [wiki's Benchmarks page](https://github.com/kannamma-labs/atomic-forge/wiki/Benchmarks). Forks and PRs from the live campaign are raised and maintained under the [`kannamma-labs`](https://github.com/kannamma-labs) account, not a personal one.

## How it fits the ecosystem

aider and Cursor are pair programmers; SWE-agent is a research harness;
OpenHands and Devin are hosted agent surfaces. forge is the **library
layer under all of those conversations**: a strict task contract, a
repair loop you can embed in your own tooling, and a CLI/GitHub-Action
surface for autonomous issue → PR.

| | |
|---|---|
| [**Quickstart**](https://github.com/kannamma-labs/atomic-forge/wiki/Quickstart) | First run: task contract, `decompose`, `run`, Python API |
| [**Issue → PR**](https://github.com/kannamma-labs/atomic-forge/wiki/Issue-to-PR) | `fix` / `fix-comment` pipelines, fork-only PRs, safety properties |
| [**Bootstrap gate**](https://github.com/kannamma-labs/atomic-forge/wiki/Bootstrap-Gate) | Environment bootstrap for *any* GitHub URL, agentic fallback |
| [**Benchmarks**](https://github.com/kannamma-labs/atomic-forge/wiki/Benchmarks) | Measured results + the shipped-vs-claimed ledger |
| [**How it's different**](https://github.com/kannamma-labs/atomic-forge/wiki/How-Is-This-Different) | Honest landscape vs Cursor/Claude Code, Devin, aider, SWE-agent / OpenHands — and when to use them instead |
| [**Evaluation plan**](https://github.com/kannamma-labs/atomic-forge/wiki/Evaluation-Plan) | The public-evaluation contract we'll be judged by: SWE-bench + real issues, baselines, ablations, cost-per-fix |
| [**Packaging & roadmap**](https://github.com/kannamma-labs/atomic-forge/wiki/Packaging-and-Roadmap) | GitHub-Action-first distribution: drop-in in <5 minutes, example workflows |
| [**Requirements R1–R16**](https://github.com/kannamma-labs/atomic-forge/wiki/Requirements-and-Roadmap) | Competitive survey, one page per requirement, status against real code |

## Notable modules

- **`patch.py`** — SEARCH/REPLACE parser with a tested normalization
  fallback chain and a disjointness preflight that rejects overlapping
  hunks *before* any text is rewritten.
- **`repair_agent.py`** — localization by evidence (traceback frames,
  symbol resolution, blast radius, statement-level def-use), K
  independently-sampled parallel attempts selected by *execution*, and a
  blast-radius gate with rejection feedback into the next round.
- **`checkpoint.py` / `checkpoint_store.py`** — per-file hash-diffing on
  resume; 7-way verdict taxonomy and full phase history, not a boolean.
- **`concurrency.py`** — ramp up by 1 per success, step down by 2 on a 429,
  with a monotonic counter so an in-flight success can't race a
  rate-limit step-down.
- **`bootstrap.py`** — the environment gate + Docker-only agentic fallback
  with snapshot/rollback and a bootstrap cache.
- **`tools.py` / `codegraph.py` / `graph_statements.py`** — `ToolBackend`
  protocol, two included backends (in-memory and SQLite call graph with
  statement tables), plus a worked `ripgrep` reference backend in
  [`examples/`](examples/). Bring your own (a real language server) behind
  the same protocol.

See the [wiki's Architecture page](https://github.com/kannamma-labs/atomic-forge/wiki/Architecture) for the full module map.

## GitHub Action

This repo is also a GitHub Action (`action.yml` + `Dockerfile`) — a thin,
containerized wrapper around the CLI. No hosted service, no bot; it runs
inside your job's container. See the
[wiki's GitHub Action page](https://github.com/kannamma-labs/atomic-forge/wiki/GitHub-Action)
for inputs/outputs and worked workflows.

## Contributing & community

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, the `AtomicTask`
  contract, how to add a benchmark case
- [Discussions](https://github.com/kannamma-labs/atomic-forge/discussions) —
  questions and ideas
- Companion design notes: the [wiki's Design Notes](https://github.com/kannamma-labs/atomic-forge/wiki/Design-Notes)

## License

Licensed under the Business Source License 1.1 — see [LICENSE](LICENSE). Production use requires a commercial license; the code converts to GPL-3.0-or-later on 2030-08-29.