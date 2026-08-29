*Scored "by actually running each repo's own test suite, not by asking a
model which patch looks right." Harness + seeds are in the repo; every
number here is reproducible.*

## Measured, live-LLM results

### Real bugs from real open-source repos

forge+CIE fixed **4/4** real merged-bug cases (from more-itertools and
boltons, both MIT with open issue trackers), driven by each real PR's
regression test — **each in 1 repair round**, every fix semantically
matching the merged PR.

Then the harder half: given *only the bug description*, CIE generated a
regression test for **4/4** cases — validated to (a) fail on the buggy
code on a real assertion and (b) pass on the real merged fix — and
forge+CIE fixed **4/4** against those *generated* tests.

`→ [docs/cie-forge-realbug-benchmark.md](https://github.com/arunsoman/atomic-forge/blob/main/docs/cie-forge-realbug-benchmark.md)`

### CIE-vs-no-CIE token cost

On one mathematically-subtle planted bug: the CIE-backed agent fixed it
correctly in **~63% fewer tokens**; the same agent without the graph broke
the suite and did not converge.

`→ [docs/cie-graph-bugfix-benchmark.md](https://github.com/arunsoman/atomic-forge/blob/main/docs/cie-graph-bugfix-benchmark.md)`

```bash
# reproduce:
pip install git+https://github.com/arunsoman/atomic-forge.git \
            git+https://github.com/arunsoman/cie.git pytest
python benchmarks/cie_forge_realbugs/forge_cie_bench.py boltons_bits_offbyone
python benchmarks/measure_cie_graph_benefit.py
```

## Harness design

- **Cases** are real merged GitHub bug fixes, not synthetic planted bugs —
  `benchmarks/` holds the case definitions, seeds, and runner.
- **Scoring** = the repo's own test suite executed against the patch;
  generated regression tests must fail-then-pass on the real fix.
- **Raw outputs** are committed under
  [`benchmarks/results/`](https://github.com/arunsoman/atomic-forge/tree/main/benchmarks/results).

## Benchmarks shipped vs. claimed (honest ledger)

| Claim | Status |
|---|---|
| Real-bug fix rate (CIE-backed `fix`), testgen validity | ✅ measured, 4/4 + 4/4 above |
| CIE token savings | ✅ measured (−63% on the case) |
| Statement-graph localization delta (R11, ARISE-style) | Graph shipped + tested; the *live-LLM delta* is pending ([[Statement-Level-Graph]]) |
| Agentic bootstrap success rate on un-curated repos | Gate + fallback shipped; EnvBench-style measurement pending ([[Bootstrap-Gate]]) |
| `--architect` (planner pass) fix-rate delta | Shipped default-off pending live-LLM A/B ([[Environment-Bootstrap]]) |

Anything not on the ✅ lines is **not claimed** — the ledger exists so the
boundary stays explicit.