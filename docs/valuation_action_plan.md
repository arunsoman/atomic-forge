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
| GitHub Action installs (stretch) | **100+ in 90 days** — report #3's gate, coincides with our Phase-3 gate (#3 = independently derived) |
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

## Addendum 2 — third report: trajectory portfolio (2026-08-31)

A third perspective maps four futures and asks which is in play. It's
strategy-level, so the deltas are portfolio decisions and measurement sticks,
not new engineering work.

### Trajectory ranking (deliberate portfolio choice)

| # | Trajectory | Report | Our stance |
|---|---|---|---|
| 1 | Enterprise repair bot (GitHub App backlog automation) | “$50–200/seat/mo, 500–1k orgs by 2028” | **primary commercial path** — the proven-fix moat (execution-selected + blast-radius) is exactly its differentiation vs Copilot Autofix; Phase 3 + maintainer channel feed it |
| 2 | Embedded kernel under coding surfaces | “$100k–$1M/yr royalties” | **upside, gated on proof** — requires demonstrated cost/quality superiority (Phase-2 ablation + $/fix curve); the Red-Hat-model licensing ask should be pitched only after numbers exist |
| 3 | Evaluation harness for research | “cited in 5–10% of SWE-bench submissions” | **credibility engine, not a business** — its risk (“nobody pays for test harnesses”) is correct; use it to earn citations that de-risk #1/#2, nothing more |
| 4 | OSS sustainability layer | “10,000 projects by 2028” | **mission + dataset accrual, not revenue** — the 10k figure is fantasy at current traction; the honest near-term expression is the maintainer-opt-in channel + campaign ledger |

The report's most-likely exit (acqui-hire/tech-license for the checkpointing +
execution-selection IP) means **every phase ideally produces artifacts a
platform acquirer would want to inspect**: measured cost curves, the 7-way
verdict taxonomy in action, crash-safe checkpointing evidence from real runs.
The campaign ledger satisfies this by construction.

### The 90-day gate — two independent reports now converge

Report #3: “10+ real merged PRs on high-profile repos and >100 GitHub Action
installs ⇒ a shot at trajectory #2/#3.” The **10+ merged PRs gate already ==
our Phase-3 gate** (set before this report arrived). Adopting one new row:
`GitHub Action installs: 100+` as the matching Phase-3 stretch metric.

### Decisive variables → plan ownership

| Variable (report #3) | Owning phase | Current measured state |
|---|---|---|
| SWE-bench ≥30% at <$5/fix | Phase 2 | unmeasured — that's why Phase 2 exists; do not promise before measuring |
| Platform adoption | Phase 3 → 4 | 0 external; gate 3 repos → then pitch |
| License sales before 2030 GPL conversion | Phase 4 | 0; friction fix in Addendum decision ask #1 |
| Community traction (1k+ stars by end-2026) | — | **calibrated away**: usage metrics (installs, merged PRs, opted-in maintainers) instead of stars |

### Competitive watch (new entry point in the landscape)

Report #3 names a risk reports #1–2 missed: **GitHub's own Copilot Autofix /
CodeQL repair flows improve rapidly** — never build the *workflow* moat
(issue→PR bot is a commodity); build the *verification* moat (statement-level
def-use graphs, blast-radius gating, execution-selected patches). Campaign PRs
should translate this into one-line proof: every PR footer already demonstrates
the machinery; the wiki publishes the gatekeeping (what got *blocked* and why —
blocked runs are evidence too).

### NumFOCUS synergy (ground-truth note)

Tier-1 targets (pandas, numpy, xarray, dask, matplotlib…) are NumFOCUS-family
projects with DCO sign-off — the exact foundation ecosystem trajectory #4
dreams about. Clean, consent-earned, execution-verified merges there double as
Seed evidence for both the sustainability narrative and enterprise repair cases.

## Addendum 3 — intake quality note: fourth report (2026-08-31)

**Verdict: invalid input — premise failure.** The evaluator never read the
README; it extrapolated from the name alone and analyzed a *frontend Atomic
Design scaffolder* (shadcn/ui-era component generation, Figma sync, Bit.dev
competition). That product does not exist. Atomic Forge is an autonomous
software-repair engine (issue → regression test → execution-selected patch →
PR) for Python. Every valuation tier, market context, and competitor mentioned
is out of domain. **Nothing integrated; no phase changes.**

Forward-looking use: reports #1–3 were README/experience-grounded and passed a
premise check; #4 failed it. **Standing intake rule: every future external
evaluation gets a premise check first** (did the evaluator actually read the
code/README or run the tool?). Reports that fail are logged here, not blended
into strategy — otherwise noise compounds into the plan.

Three transferables that survive the premise failure:

1. **The moat taxonomy generalizes, and the tiers are a useful ladder:**
   commodity (anyone can write it in a weekend) → replication-hard
   machinery (AST-level understanding) → *compounder* (proprietary measured
   data). Our honest position: the code sits at tier 2 — replaceable with
   effort — while the **failure taxonomy + selection policy + run ledger**
   (what it learned from 55+ real attempts) is the only tier-3 asset, and it
   only exists if runs keep happening. One more argument for firing the
   campaign.
2. **DD checklist → dashboard rows:** PyPI download *growth* (post-publish),
   entity continuity (Kannamma Labs as a legal counterparty is genuinely an
   asset), and presence of a hosted layer — all already tracked or in Phase 4.
3. **“Open-source code alone is incredibly hard to monetize”** — third
   independent confirmation (after reports #1 and #2) that the hosted/managed
   service around the core is the revenue vehicle, reinforcing the Phase-4
   hosted-loop item.

Closing note, kept for morale alone: by the *invalid* report's own moat
taxonomy, our real product already clears its “high moat” bar (AST
manipulation, migration-grade refactors) — while scoring a product that
doesn't exist. The lesson is about input rigor, not valuation.

## Addendum 4 — the assistant's position (post-input synthesis, 2026-08-31)

Having worked *inside* the codebase through every failure these reports only
summarized, my position on three points diverges from all four inputs:

**1. SWE-bench is the right appendix, the wrong headline.** It is pre-triaged
data — every task reproduces, every fix point is real, curated by humans. That
is exactly the distribution our machinery is *least needed for*; the campaign's
real-world distribution (30–50% stale, unmaintained issues, empty suites) is
what buyers actually face and what we gate cheaply. Do the 100-instance slice,
as planned — but framed as the **same-model ablation** (loop on/off, cost +
regression rate), never a leaderboard SOTA run. The hero artifact is the
campaign ledger; SWE-bench is its standardized durability check.

**2. Every report priced the code; none priced the ledger.** The compounding
asset is not the repair agent — it is the dataset of
(issue, probe-verdict, test, patch, exit-verdict, cost) tuples. Each run
yields either a PR or a labeled failure; both train the selection policy and
freeze a moat competitors can't download. Reports #1 ("intelligence dataset")
and #3 (acqui-IP) both gesture at this without the operational conclusion:
**the ledger is the product**, and every meta-hour not spent producing ledger
rows is deferred valuation.

**3. The moat is not repair — it's change provenance.** Frontier-model churn
erodes any claim a bare retry-loop can't replicate within quarters. What
doesn't erode: futures expect autonomous changes in *regulated* codebases to
come with machine-checkable provenance — probe, failing test, execution-\
selected patch, blast-radius net Δ, full artifact chain. No vendor ships
that today; forge's exit taxonomy + checkpoints *are* it, mislabeled as a dev
tool. Target the buyer whose constraint is trust, not raw capability
(finance/healthcare/code-is-liability). The campaign PRs and the public
funnel are the first page of that trust ledger.

Where I fully **agree** with the evaluators: proof before distribution,
distribution before monetization, Action+PyPI as the universal surfaces,
hosted layer as the revenue vehicle, publish failures. Their timeline tables
were internally inconsistent (100–500 stars in 90 days vs "nothing exists at
0 stars"); the honest scorecard is our dashboard, not their scenarios.

My standing risk register (not in any report): solo-maintainer fragility,
LLM budget growing linearly with batch size (the ledger doubles as the
budget guard — measure $/resolved before scaling out), fresh-account API
throttling on the fork/PR path, and cold-PR reputation risk (the opt-in
channel retroactively underpriced by report #3).

Therefore, this week, strictly in order: fire batch1 → publish the evidence
wiki (funnel + CIE 63% ablation + ledger) → PyPI + self-hosted Action demo →
license grant decision → *then* SWE-bench slice. Each step must produce an
artifact before the next begins.

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