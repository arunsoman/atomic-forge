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
   matching "why BSL not MIT" FAQ line. **This LICENSE edit needs your
   explicit sign-off before it's committed** — it's a real legal document,
   not just docs copy.

## Track B — publish the campaign that's already running

`Evaluation-Plan.md` still says the 40–60-issue real-issues set and
baseline runs are "planned." They're not — `campaign50_targets.json`
already targets 18 repos, and the astroid round already landed **4 real
merged-quality PRs** (fuzzer-found crashes, #3261–#3264) with genuine
engineering findings along the way (statement-level spectrum localization
fix, contaminated passing-sample coverage, multi-file patch targeting).
That's a stronger opening story than a demo repo.

1. Finish the current campaign50 pass (or a clean subset of it) to the
   point it's write-up-able — you're already at or past the "10–15 issues
   is enough to publish early" bar the Evaluation-Plan itself sets.
2. Write it up: methodology, per-case outcome, the engineering findings
   above (they're good marketing on their own — real bugs found in your
   own fault-localization and patch pipeline, fixed, regression-tested).
3. ✅ Updated `Evaluation-Plan.md`'s ledger table to reflect actual status
   (astroid PRs raised + independently re-verified, not "planned"; the
   40–60-issue target already scaled to 18 repos via campaign50; the one
   fully-logged ledger run — astroid#769, a real failure case — kept
   visible rather than hidden). Full write-up with reconciled ledger still
   needed before this is a publishable result, not just a status update.
4. Only after this exists: SWE-bench Verified harness, baselines (aider,
   agent modes), ablations (graph, blast-radius gate, K) — these were
   already correctly scoped as later milestones.

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
| Now (parallel) | PyPI publish + tag-reference fix (Track A) **and** finish/write up campaign50 astroid results (Track B) |
| Next | Marketplace listing + demo repo (now backed by real numbers) |
| Then | SWE-bench harness, baselines, ablations, cost-per-fix |
| Then | MCP exposure + public announcement (numbers-first, BSL FAQ ready) |
