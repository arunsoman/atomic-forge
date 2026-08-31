# Goal: zero-traction → adopted

Adoption playbook, amended against actual repo state as of 2026-08-31 (see
inline notes marking what's already done vs. still open). Builds on
[[Packaging-and-Roadmap]] and [[Evaluation-Plan]] — this page takes
precedence where it corrects those.

## This week: two parallel tracks, not sequential

The original draft put PyPI/Action polish in "week 1" and benchmark
expansion in "week 2+". Don't sequence them — do both now:

- **Track A (mechanical, low-risk, do it):** front-door fixes below.
- **Track B (the actual credibility unlock):** publish the astroid
  campaign results. This is *already in flight and already has proof* —
  don't wait on it.

Shipping the front door before there's a public number behind "reliable"
risks a worse outcome than no traction: someone installs, gets an
unverified result, bounces. Track B closes that gap and it's most of the
way there already.

## Track A — front-door mechanics

1. **Publish to PyPI.** `publish-pypi.yml` exists but nothing is live —
   `pip install atomic-forge` currently 404s. Set `PYPI_API_KEY` (or wire
   trusted publishing) and cut the release.
2. ✅ **Fix the stale tag references.** Tags `v0.1.0`/`v0.1.1`/`v0.2.0`
   already exist — no new tag needed. Bumped the stale
   `@v0.1.0` refs to `@v0.2.0` in `wiki/Packaging-and-Roadmap.md` and
   `wiki/GitHub-Action.md` (found two extra ones there beyond the original
   scope).
3. **One-command first contact.** `atomic-forge fix <issue-url> --dry-run`
   as the front door; preflight errors name exactly what's missing
   (API key, GH token).
4. **Demo repo** with the Action pre-wired on a couple of known-good
   issues — but sequence this *after* Track B has something to point the
   demo at, not before.
5. ✅ **License messaging — and a real gap it surfaced.** `LICENSE`'s
   `Additional Use Grant` was `None`, meaning the license as written did
   not actually grant free production use — running forge in CI against a
   real repo is production use, and that would have required a commercial
   license from day one, directly undercutting the whole "pip install and
   drop it into your repo" push. Added an Additional Use Grant permitting
   free production use (own/client codebases, CI, the Action); a
   commercial license is now needed only to resell forge itself as a
   hosted service or embed it in a competing offering. README got a
   matching "why BSL not MIT" FAQ line. Signed off and committed
   (`3df25d0`).

## Track B — publish the campaign that's already running

Turned out to be bigger than either the original draft or the first pass
of this page knew: **three separate unpublished eval streams** existed in
the repo (the original 4/4 harness, campaign50/astroid, and sweep
round2+round3 — the last one alone is 102 attempts across ~20 repos and
was not reflected anywhere in the wiki). Reconciled into
`benchmarks/real_issues/RESULTS.md` — commit `c20d3e1`.

1. ✅ Reconciled all three streams: **103 tracked attempts across ~20
   repos, 12 PRs raised, 0 merged so far** (10 open, 2 closed without
   merge). Outcome distribution reported honestly, not just the raise
   count: ~32% oracle_reject, ~32% repair_fail, ~22% infra_fail, ~12%
   pr_raised. infra/bootstrap failure is the single largest bucket — a
   bigger lever right now than repair-loop tuning.
   **Superseded 2026-08-31 (later same day):** this undercounted —
   `results_round4.jsonl` (26 more attempts, run later that day) wasn't
   folded in before this was written, and drifted from
   `benchmarks/README.md`'s PR table as a result. Re-reconciled via the
   new `benchmarks/real_issues/reconcile.py` (reads every round's ledger,
   dedupes retried issues, live-checks PR status): **120 tracked
   attempts, 18 PRs raised, 0 merged** (11 open, 7 closed). See
   `RESULTS.md`. Also changed as of this date: `RESULTS.md` and the wiki
   now report PR outcomes only (raised/open/closed/merged), not the
   internal outcome-distribution breakdown — a deliberate reporting-scope
   narrowing, not a correction; the full breakdown is still logged in
   `sweep/results_round*.jsonl` for anyone auditing the harness.
2. ✅ Write-up done (`RESULTS.md`) — methodology per stream, live PR
   status (checked via `gh`, not stale log claims), the honest headline
   ("0 merged" is the number to lead with, not "12 raised").
3. ✅ Updated `Evaluation-Plan.md`'s ledger table to point at `RESULTS.md`
   and match its numbers.
4. ✅ **Found and fixed a real gap this pass surfaced**: one closed PR
   (discord.py#10507) was closed citing the repo's written
   AI-contributions policy — `campaign50_targets.json` documents a manual
   "grep CONTRIBUTING for AI/LLM policy" protocol step that neither
   `run_campaign.py` nor `sweep.py`'s pipeline actually automated, and the
   existing `pr_writable.py` probe only catches API-level PR gates, not a
   written-but-unenforced policy. Added `check_ai_policy()` to
   `pr_writable.py` — verified it flags discord.py and doesn't
   false-positive on black/trio.
5. **Not yet done, genuinely still open**: 0 of the 18 raised PRs are
   merged. This document needs revisiting once reviews land either way —
   don't publish "raised" as if it were "succeeded."
6. **Priority changed 2026-08-31**: the earlier "only after merged
   results exist" gate on SWE-bench/baselines/ablations is lifted —
   starting the SWE-bench Verified harness now rather than waiting on
   upstream review outcomes we don't control the timeline of. See revised
   priority order below.
7. Checked for PRs "left unaccounted for" — attempts where a patch
   verified green but no PR was ever opened due to our own pipeline gap
   (the class `fix.py`'s post-green force-commit fix, item 3 in the
   session addendum below, was written to close). Found exactly one
   near-miss across all rounds: `Textualize/rich#4208`, marked
   `pr_locked` — not a pipeline gap, but rich's own repo settings gating
   PR creation to collaborators (same category as the discord.py
   AI-policy exclusion). Correctly not pushable; nothing else queued.

## Phase 2 — GitHub Action as primary distribution surface

(Unchanged from the original draft — this was already sound.)

- Zero-config goal: fork-only PRs default, `--dry-run` first-class,
  bootstrap-cache keyed by commit for fast repeat runs on the same HEAD.
- Marketplace listing once the tagged version + PyPI release are both live.
- Document secrets precisely: `FORGE_API_KEY`, `GH_TOKEN`/fine-grained PAT.

## Phase 3 — integrations that multiply reach (after credibility, not before)

- MCP server exposing the repair engine — the CIE backend already proves
  this shape works forge-ward; expose forge the same way.
- Keep `--local-only` strict and documented (Ollama/LM Studio) — genuinely
  underserved in this category, keep it a real differentiator not just a
  flag.
- Later/optional: hosted playground, VS Code/Cursor extension, watchdog
  continuous-repair loop.

## Phase 4 — distribution & feedback loop

- Announcement leads with the astroid campaign numbers, not a features
  list.
- Post to r/MachineLearning, r/LocalLLaMA, HN, relevant coding-agent
  Discords — with the BSL FAQ ready before the licensing question comes up.
- Instrument: Action runs, PyPI downloads, issues/PRs mentioning forge.
- "What broke when you tried it?" Discussions template; use the first
  10–20 external attempts to drive error-message/bootstrap-edge-case
  iteration.

## Positioning (unchanged)

> **"The reliable, test-driven repair engine you can drop under any
> coding agent."**

## Revised priority order

| When | Focus |
|---|---|
| Now | **SWE-bench Verified harness** — start small (10–15 issues), adapt `atomic-forge fix`/`repair` to SWE-bench's Docker eval images rather than building new infra. No longer gated on merged-PR outcomes (changed 2026-08-31). |
| Also now (parallel, mechanical) | PyPI publish + tag-reference fix (Track A) |
| Next | Cost-per-fix backfill (parse the `llm_calls`/token counts already logged per attempt), then ablations (graph, blast-radius gate, K) on the existing `cases/` runner — no new harness needed |
| Then | Baselines (aider, agent modes, pure generate-then-test), marketplace listing + demo repo |
| Then | MCP exposure + public announcement (numbers-first, BSL FAQ ready) |

## Session addendum (2026-08-31): learnings folded back into forge

Manual failure-analysis during round4 turned into 5 permanent pipeline
fixes rather than one-off patches, so future campaign runs stop
rediscovering the same things by burning tokens:

1. `pr_writable.py` — `check_ai_policy()`: grep CONTRIBUTING/PR-template
   docs for a written AI-contributions policy before spending a fix
   attempt (caught discord.py, xarray, sympy).
2. `curate.py` — `maintainer_rejected()`: skip issues an OWNER/MEMBER/
   COLLABORATOR already settled in-thread ("working as intended", etc.) —
   found live on dateutil#1421, which cost 158 LLM calls / ~2.9M tokens
   for a non-bug.
3. `fix.py` — force a commit right after ground-truth-green is confirmed,
   closing the "verified green but never committed" failure that killed
   pylint-dev/pylint#11361's otherwise-correct fix at the PR step.
4. `llm.py` — `FORGE_MODEL_FALLBACKS`: switch models immediately on a
   quota-exhaustion error instead of burning 4 useless backoff retries
   against the same capped model (13/34 sampled failures were this).
5. `sweep.py` — `classify()` now reports `quota_exceeded` and
   `pr_mechanics_fail` as their own categories instead of both silently
   defaulting into `repair_fail`.

All 5 covered by tests (359/359 passing). Commits: `319ccee` (pr_writable
was earlier, `5d1e82d` fix.py, `319ccee` curate.py, `89e8c19` llm.py,
`ef4231a` sweep.py).
