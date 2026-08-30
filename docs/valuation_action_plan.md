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

## Addendum — second report integration (90-day playbook, 2026-08-31)

A second external report (prioritized 90-day GTM playbook) largely converges
with the valuation's thesis — two independent evaluators landing on
*public proof → distribution → monetization, in that order* is strong signal.
Our Phase 0–4 already encodes that spine. This addendum records the deltas:
what report #2 adds, what we resolve, what we reject.

### Adopt (new, actionable — folded into phases)

| Item | Phase | Owner-status |
|---|---|---|
| **PyPI package** (`atomic-forge` name verified free 2026-08-31; current install is `pip install git+…` only, setuptools build already configured) | Phase 3, pulled *forward* — publish v0.2.1 after the next batch so posts/PR footers cite an installable package | needs user's PyPI account + `twine upload`; build/prep + GitHub Action auto-release can be done now |
| **Maintainer opt-in channel**: mid-sized Python projects with heavy issue queues invited first ("run forge on your backlog"), instead of only cold PRs to the top-12 repos. Add maintainer-opted tier to campaign50 alongside tier-1/tier-2 | Phase 3 amendment | list of candidates = user decision; consent-first by construction |
| **Publish failure cases as aggressively as successes** ("credibility compounds faster than hype") | already doctrine — public ledger + funnel README; keep | done ✓ |
| **Public technical posts** from wins we already possess: 63% token saving (CIE A/B), crash-safe resumable checkpoints, blast-radius gate catching superficial fixes | Phase 3 | assistant can draft; user posts (HN, r/LocalLLaMA, X) |
| **Frictionless licensing page**: production-license form + transparent tiers, not buried | Phase 4, pulled forward to Phase 3 (page + terms text only — no pricing commitments) | needs user's pricing decision |
| **`Additional Use Grant: None` is the strictest legal posture available** — BSL permits a generous grant while keeping the commercial right: e.g. free non-commercial/evaluation production use (N runs/mo), or free for orgs under revenue threshold. Adopting one converts report #2's "BSL kills adoption" criticism into "BSL with a fair grant" without abandoning the 2030 GPL path or the commercial thesis | **decision ask**, NOT unilateral: LICENSE is the user's legal/business call | see decisions below |

### Resolve (internal tension in report #2, resolved deliberately)

§2 demands tight integrations with Cursor/Claude Code/Windsurf/aider/OpenHands;
§4 warns against scope creep and says ship the Action + library first. These
conflict. **Resolution: universal surfaces only until proof** — PyPI package,
CLI, and GitHub Action are interface *every agent can shell out to*; per-tool
adapters are sequenced deliberately behind the Phase-2 results, and only for
tools whose users actually ask (no speculative adapters). This matches the
narrow positioning both reports endorse.

### Calibrate (report #2 claims corrected by ground truth)

1. **"First 100–500 stars in 3 months"** — stars are a vanity metric, and
   star-CTA behavior in upstream PRs was ruled out as spam. The 3-month score
   is *usage*: PyPI downloads, Action repos, merged PRs, opt-in maintainers.
2. **Head-to-head vs aider/SWE-agent/OpenHands on the same 50–100 issues** —
   the most credible artifact available, but expensive (3–4 harnesses × token
   budget × harness quirks) and methodologically messy. **Sequenced second**:
   first the bare-model ablation (same model, same harness — our CIE A/B
   methodology, already proven), published as the headline; a matched 25-issue
   competitor subset only if/when there's budget for the full three-tool run.
3. **"Working GitHub Action, first 100–500 stars"** — the Action exists
   (`action.yml`); the real gap is the day-one UX (one-line workflow), docs,
   and one demo repo — cheap, do this Phase 3.

### Updated dashboard rows

| Metric | adds from report #2 |
|---|---|
| PyPI downloads / month | track from publish week |
| Action-adopting repos | track from Phase 3 |
| opt-in maintainer repos | track from campaign tier-3 |
| commercial-page inquiries | track from page launch |

### Decision asks for the owner (not assistant-defaultable)

1. **License posture**: keep `Additional Use Grant: None`, or adopt a generous
   grant (recommended: free production use ≤ threshold, e.g. <$1M revenue, plus
   explicit evaluation grant)? Revisit core-MIT split only if distribution
   stalls by month 6.
2. **PyPI**: create publisher account (assistant preps build + trusted-publisher
   GitHub Action so releases are automatic).
3. **Competitor head-to-head budget**: run 25-issue matched subset now, or
   publish bare-model ablation first and defer competitors?
4. **Maintainer-opt-in list**: pick 5–10 mid-sized Python projects (assistant
   can draft a scoring rubric: issue throughput, maintainer responsiveness,
   test-suite speed, MIT-family license).

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