*Nothing compounds without credible, hard-to-dismiss results. This page is
the evaluation contract: what we will measure, how, and against whom —
publicly and reproducibly.*

## Why a public evaluation is the single biggest unlock

forge's differentiators (execution-selected patches, blast-radius gate,
statement-level graph, checkpoint-everything) are claims right now. They
become a *product* the day they survive a public, reproducible comparison
against strong baselines. The bar in 2026:

- **Public & reproducible** — harness + case definitions + seeds in-repo
- **Real GitHub issues** — not synthetic planted bugs
- **Reports success AND cost** — tokens, wall-clock time, $ per successful fix
- **Strong baselines** — Claude Code agent mode, Cursor agent, aider,
  SWE-agent-style, OpenHands, plus a pure generate→test loop (no graph)
- **Failure analysis** — *why* a case failed, not just pass/fail

## The evaluation matrix

| Component | Recommendation | Why |
|---|---|---|
| Primary benchmark | **SWE-bench Verified** (or the hardest current public set) + a custom **Real-Issues** set | SWE-bench is the language everyone understands |
| Custom set | **40–60 real closed issues** from mid-sized popular Python repos — boltons, more-itertools, attrs, click, rich, pydantic, … | Shows real-world transfer beyond the benchmark |
| Success definition | Patch makes the **generated regression test + the repo's original suite** pass, **and** matches the real merged PR (or survives human review) | Avoids "passes tests but is wrong" |
| Baselines | Claude/GPT agent modes, aider, pure generate-then-test (no graph), no repair loop | Shows the value of the specific architecture |
| Ablations | ± statement-level graph, ± blast-radius gate, K=1 vs K=4/8 | Proves which pieces actually matter |
| Cost metrics | Tokens, wall-clock, **$ per successful fix** | Critical for practical adoption |
| Transparency | **Full trajectories + every published patch** | Builds trust |

## Execution plan

1. **Start small, publish early.** 10–15 issues is enough to publish
   methodology + first numbers (the existing 4/4 real-bug harness —
   [[Benchmarks]] — is the template; extend it, don't replace it).
2. **Make the harness runnable by others.** It already is:
   [`benchmarks/cie_forge_realbugs/forge_cie_bench.py <case>`](https://github.com/kannamma-labs/atomic-forge/tree/main/benchmarks) —
   one command, deterministic seeds, Ollama-default endpoint. The
   evaluation-extended version must be as runnable.
3. **Track two success numbers**: one-shot success rate, and
   success-within-N-rounds. Report both — they answer different questions.
4. **Hunt for divergent cases on purpose**: the most persuasive datapoints
   are where a pure LLM agent fails and the execution-selected +
   graph-backed loop succeeds. Annotate every such case in the failure
   analysis.

## Ledger status (honest)

| Component | Status |
|---|---|
| Real-issues harness (real merged bugs, live LLM, execution scoring) | live, 4/4 ([[Benchmarks]]) |
| Regression-test generation from issue text, oracle-validated | live, 4/4 ([[Issue-to-PR]] testgen half) |
| campaign50 real-issues campaign (18 repos, `fix --raise-pr`) | **in progress** — see `benchmarks/real_issues/campaign_log.md`. 4 PRs raised on pylint-dev/astroid (#3199, #3259, #3258, #3257 — fuzzer-found crashes), each independently re-verified against the repo's own suite after patching; PR merge status not yet tracked here. One fully-instrumented ledger run recorded so far (astroid#769: `repair_exhausted` after 5 rounds — a real failure case, kept for the failure-analysis writeup, not hidden) |
| SWE-bench Verified harness | **planned** — next milestone |
| 40–60-issue custom Real-Issues set (scaling from 4) | **in progress**, scaled beyond target to 18 repos via campaign50 — see row above |
| Baseline runs (aider / agent modes / pure generate-then-test) | **planned** |
| Ablations (graph, blast-radius gate, K) | **planned** |
| Cost-per-successful-fix reporting | **partially live** — the one fully-logged campaign50 run already carries full token/wall-clock accounting (`campaign50.ledger.jsonl`); needs the rest of the campaign logged the same way before it's reportable in aggregate |

Nothing on the ✅ lines is claimed beyond what the harness actually ran.
This page exists so the gap between "measured" and "designed" stays
visible — [[Benchmarks]] for what's measured today, [[Design-Notes]] for
the why behind the designs. The campaign50 row above is deliberately not
✅ yet: 4 raised PRs are a strong early signal, not a published, reconciled
result set — see `goal.md` (Track B) in the main repo for what
"publishable" means here (ledger and narrative log reconciled,
methodology written up, honest inclusion of the failure case).