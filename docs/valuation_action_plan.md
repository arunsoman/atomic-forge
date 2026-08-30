# From $150K–$400K to $1M+: the operational plan

Response to the external valuation (2026-08-30). The evaluator's thesis:
*"the next $1M of value comes from proving, quantitatively, that forge makes
autonomous code changes substantially more trustworthy than ordinary coding
agents."* Agreed. This is how we produce that proof with the resources we have.

**Key reframe the evaluator missed:** the 55→3 funnel (~5.5%) is not one number,
it's five failure classes with different root causes — and our pilot RCA
(`benchmarks/real_issues/rca_pilot_runs_1_3.md`) already measured them:

| Failure class (measured) | Share of losses | Fix status |
|---|---|---|
| Stale/irreproducible issues | ~50% of pilot targets | ✅ F1 `--repro` gate (shipped bd15fe1) |
| Testgen can't write a test (oracle_reject 27/55 round-2) | ~49% of round-2 losses | 🔧 F4 `--test-file` + budget nudge (next) |
| Repair doesn't converge (repair_fail 23/55) | ~42% | 🔧 model upgrades + MetaOriginTracker (F7) |
| Bootstrap gaps (extras, gate health) | 2/55 + pilot data | 🔧 F2/F6 (small) |
| Ops/artifact fragility | pilot | ✅ F1b/F1c + `--work-root` (shipped) |

A realistic target is not "fix everything" — it's **moving the
reproducible→PR-opened conversion from 5.5% to 25–30%**, which is entirely
consistent with the measured failure decomposition.

---

## Phase 0 — Instrument the funnel (this week, ~1 day)

Every future run becomes a ledger row; every terminal event is auditable.
This is the seed of the "software-repair intelligence dataset" (evaluator §11).

- [ ] `benchmarks/real_issues/run_campaign.py`: campaign runner that per run
  (1) re-verifies issue state via API, (2) runs with `--repro` + durable
  `--work-root`, (3) appends one ledger row to
  `benchmarks/real_issues/campaign50.ledger.jsonl`:
  `{date, repo, issue, state_check, stage, exit_reason, llm_calls,
  prompt_tokens, completion_tokens, wall_s, pr_url, model}`
  (4) copies `.forge/{exit_audit.jsonl,learning.json,trajectory.jsonl}` into
  `logs/<repo-issue>/`
- [ ] README benchmark table gains a **funnel view** (attempted → pre-flight
  blocked → testgen-written → reproduced → repaired → PR → merged), not totals.
  Evidence quality 7/10 → 8.5/10 comes from this, free.

**Gate:** ledger exists; next 10 runs auto-log themselves.

## Phase 1 — Kill the two dominant loss classes (weeks 1–3)

1. **F4 `--test-file`** (the oracle_reject killer): operator-authored regression
   test bypasses testgen. Converts the largest loss bucket into viable runs and
   produces *stronger* PRs (human-curated test + forge-verified patch).
2. **F4b testgen hygiene**: write-attempt mandated by turn N−2; identical
   tool-call results short-circuited (sphinx burned 10 exploratory turns);
   testgen bug-class tagging in exit_audit detail.
3. **F6 gate honesty**: persist probe tail + F-rate into `.forge/gate.json`;
   mass-failing probes flagged in run results (never again "passed" on
   `FFFFFFFF... [2%]`).
4. **F7 MetaOriginTracker** (stretch): the dask postmortem's proposed CIE tool —
   dtype/meta dataflow provenance. Highest-leverage new tool for repair_fail.

**Gate:** on the next 20-issue batch, testgen-write rate ≥ 60%, and
pre-flight-blocked runs cost < 1k tokens each (F8 economics).

## Phase 2 — SWE-bench Verified slice + cost-per-fix (weeks 3–6)

The single biggest missing proof point (evaluator §9A). Practical scope:

- [ ] Run a stratified **100-instance slice** of SWE-bench Verified (Python —
      forge's ast-exact home turf), full instrumentation.
- [ ] Report the evaluator's metrics, all already logged per-run:
      resolve rate, **$ / successful fix**, wall-clock / fix, tokens / fix,
      **regression rate** (blast-radius gate output).
- [ ] **Ablation is the headline**: same model, bare agent vs forge loop.
      We already have this methodology (CIE A/B: 63% token saving). The
      commercially defensible claim is *"same model, same tasks, X% higher
      verified resolution at Y% lower cost, with Z% regression rate"* —
      cost-controlled trust, not raw SOTA chasing.
- [ ] Publish as a wiki page + machine-readable `results/swebench_slice.json`.
      Honest numbers beat none; transparency *is* the marketing.

**Gate:** public results page. Do **not** promise "70%+" anywhere before
measured — the claim we sell is the cost/trust curve, not leaderboard SOTA.

## Phase 3 — Adoption: the GitHub Action IS the product surface (weeks 4–10)

"Evidence → adoption" per evaluator's $1M–$3M row:

- [ ] Make `action.yml` the drop-in story: *"opt-in forge bot on your repo —
      it opens regression-tested, blast-radius-checked PRs on your issues,
      with full provenance."* Repo owners opt in → consent-first by design.
- [ ] The **50-repo campaign becomes the public case study**: publish the
      ledger (failures included) as a wiki page. Target: **10+ merged PRs in
      recognizable repos** ≈ "credible external adoption" + maintainer quotes.
- [ ] One-pager: **cost-per-successful-fix** with real Phase-2 numbers
      (evaluator §9B — the ROI math an engineering VP can run).

**Gate:** ≥3 external repos opt into the Action; ≥10 merged campaign PRs.

## Phase 4 — Commercial track (quarter)

- Pricing page anchored on **$/verified-fix** tiers (BSL already supports it —
  evaluator called the license "commercially important"; make the production-
  license terms prominent rather than buried).
- 2–3 design partners from Action opt-ins (free pilots OK — their telemetry,
  opt-in and aggregated, feeds the dataset moat).
- Positioning copy locked: **"the verification and repair layer for coding
  agents"** — never "an AI coding agent."

**Gate:** 1 paying or LOI design partner.

---

## Non-goals (deliberate)

- No new agent features until Phase 2 evidence exists (evaluator: features ≠ value).
- No raw-SOTA SWE-bench chasing; the product claim is cost-controlled trust.
- No star-baiting / promo footers in upstream PRs (provenance footer only).
- No re-raising maintainer-closed PRs (round-2 loguru precedent).

## Metric dashboard (update per phase)

| Metric | Today | Phase-1 gate | Phase-2 gate | Commercial trigger |
|---|---|---|---|---|
| issue→reproducible conversion | ~5.5% end-to-end | 50% | 60%+ | — |
| reproducible→PR-opened | ~66% (3/4.5) | 75% | 80%+ | — |
| $ / successful fix | unmeasured | measured | published | tier pricing |
| regression rate (blast radius) | gate exists | logged per run | published | enterprise ask |
| externals using the Action | 0 | — | 3 repos | 10+ |