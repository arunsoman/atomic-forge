**atomic-forge** is an agentic **issue → failing test → repair → PR** engine — a library and CLI built around three things most coding-agent tools treat as an afterthought:

*The reliable, test-driven repair engine you can drop under any coding agent.*

- **A strict, machine-checkable task contract.** Every unit of work is an `AtomicTask` — exactly one file, with a required `test_triad` (positive / negative / recovery), enforced at construction time by pydantic, not hoped for in a prompt.
- **Crash-safe, resumable runs.** Every phase transition is durably checkpointed to SQLite *before* the work starts. Resume re-hashes files on disk and regenerates only what changed — never an all-or-nothing restart.
- **A real repair loop, not a retry loop.** Failing tests become ranked suspects, patches are K-sampled in parallel and selected by *actually running the suite*, and a blast-radius gate rejects a "passing" patch that silently breaks a caller elsewhere.

Everything here is measured where possible — real merged GitHub bug fixes, run live against a real LLM endpoint, scored by running each repo's own test suite (see [[Benchmarks]]).

---

## The one-command version

```bash
atomic-forge fix https://github.com/owner/repo/issues/123
```

Fetches the issue, clones the repo, bootstraps the environment (deterministic probe, optional agentic fallback), generates a *failing regression test from the issue text*, repairs against it, and opens a PR from **your fork** — never pushing to `origin`. Full walkthrough: [[Issue-to-PR]].

## Start here

| Page | What it covers |
|---|---|
| [[Quickstart]] | First run in 5 minutes: task contract, `decompose`, `run`, Python API |
| [[Installation-and-LLM-Setup]] | Install, LLM endpoints (OpenAI / Ollama / any OpenAI-compatible), `--local-only` |
| [[CLI-Reference]] | Every phase and flag of `atomic-forge` |
| [[Issue-to-PR]] | The `fix` / `fix-comment` pipelines: issue → regression test → repair → fork-only PR |
| [[Benchmarks]] | Measured results: 4/4 real open-source bugs, CIE token savings, reproducible harness |
| [[Evaluation-Plan]] | The public-evaluation contract: SWE-bench + real issues, baselines, ablations, cost-per-fix |
| [[Packaging-and-Roadmap]] | GitHub-Action-first distribution: drop-in in <5 minutes, example workflows |
| [[How-Is-This-Different]] | Honest comparison vs aider, Cursor/Claude Code, SWE-agent, OpenHands, Devin |

## Going deeper

| Page | What it covers |
|---|---|
| [[Bootstrap-Gate]] | Environment bootstrap for *any* GitHub repo (R16): deterministic probe + Repo2Run-style agentic fallback |
| [[Statement-Level-Graph]] | Statement-level def-use graph (R11) and the ARISE-shaped localization story |
| [[Repair-Loop]] | localize → sample → select → gate: the repair pipeline internals |
| [[Checkpointing-and-Resumability]] | The SQLite run record, 7-way verdicts, hash-diff resume |
| [[CIE-Integration]] | Using CIE (Code Insight Engine) as the code-graph backend over MCP |
| [[Watchdog]] | Production loop: detect → repair → canary → promote/rollback |
| [[Architecture]] | Module map of the whole library |
| [[GitHub-Action]] | Running forge from CI |
| [[Requirements-and-Roadmap]] | Competitive requirement survey R1–R16, each with its own page |
| [[Design-Notes]] | The *why* behind the designs |

## Contributing

See [`CONTRIBUTING.md`](https://github.com/arunsoman/atomic-forge/blob/main/CONTRIBUTING.md) for dev setup, the `AtomicTask` contract, and how to add a benchmark case.

## License

[MIT](https://github.com/arunsoman/atomic-forge/blob/main/LICENSE).