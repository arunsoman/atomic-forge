# Real-issues campaign — consolidated results (as of 2026-08-31)

This reconciles three previously separate, unpublished-together evaluation
streams into one honest tally. Live PR states below were checked via `gh`
on 2026-08-31; re-verify before republishing if this document ages.

## Headline

- **103 tracked real-issue attempts** (execution-scored, ledgered) across
  **~20 popular Python repos**, plus **4 additional PRs** raised via a
  separate, less-instrumented astroid fuzzer-bug exploration (methodology
  below) — **12 real PRs raised in total**.
- **0 merged so far.** 10 PRs currently open awaiting maintainer review, 2
  closed without merge (1 rejected, 1 blocked by the target repo's
  no-AI-contributions policy — a protocol gap, not a forge quality
  failure; see below).
- This is real, live, external-facing evidence — and it is not yet a
  "success rate" claim. Nothing here should be published as more than: 12
  real PRs raised against real open issues in real repos, patches that
  passed the repo's own test suite plus a generated regression test,
  currently under human review.

## Stream A — original real-bugs harness (`benchmarks/cie_forge_realbugs/`)

4/4 historical bugs (already fixed upstream) independently re-derived by
forge and oracle-scored against the real merged fix. No new PRs (the bugs
are already closed upstream) — this is an oracle-accuracy check, not a
live-PR stream. See [[Benchmarks]] for the existing writeup; unchanged by
this reconciliation.

## Stream B — campaign50 (F1 protocol, `run_campaign.py`, ledgered)

- **1 fully-ledgered run**: `pylint-dev/astroid#769` → `repair_exhausted`
  after 5 rounds (a real failure, kept visible — see
  `campaign50.ledger.jsonl`). 151 LLM calls, ~2.1M tokens, ~43 min
  wall-clock for this single issue — the clearest available cost signal
  per attempt so far.
- **4 additional PRs** raised on separate astroid fuzzer-found crashes
  (#3199, #3259, #3258, #3257) via a hand-run exploration outside
  `run_campaign.py`'s ledger (see `campaign_log.md` for the engineering
  findings from this round — real bugs found and fixed in forge's own
  fault-localization and patch-targeting code along the way). All 4 PRs
  (astroid#3261–#3264) are **currently open**, 0 merged.

## Stream C — sweep round2 + round3 (`benchmarks/real_issues/sweep/`)

The largest stream, previously not reflected anywhere in the wiki.

| Round | Attempts | pr_raised | oracle_reject | repair_fail | infra/bootstrap_fail | policy_excluded |
|---|---|---|---|---|---|---|
| round2 | 55 | 3 | 27 | 23 | 2 | — |
| round3 | 47 | 5 | 6 | 10 | 21 | 5 |
| **combined** | **102** | **8** | **33** | **33** | **23** | **5** |

Repos touched (round3 alone, 19): discord.py, anyio, arrow, gunicorn,
celery, kombu, pint, ipython, pip-tools, networkx, paramiko, black, pip,
setuptools, pytest, trio, sphinx, tqdm, urllib3. Round2 additionally
touched loguru, babel, tenacity, cleo, httpcore, and others per
`round2.log`.

**The 8 PRs raised, live status:**

| PR | Live state | Note |
|---|---|---|
| psf/black#5370, #5371, #5372 | OPEN | 3 separate fixes, same repo |
| python-trio/trio#3498 | OPEN | |
| python-babel/babel#1334 | OPEN | supersedes #1333 (closed — reopened from the org fork instead of a personal account, same commit) |
| jd/tenacity#705 | OPEN | supersedes #704 (closed, same reason) |
| Delgan/loguru#1505 | CLOSED, not merged | no maintainer comment on record; treat as a real rejection until shown otherwise |
| Rapptz/discord.py#10507 | CLOSED, not merged | closed citing the repo's [AI-contributions policy](https://github.com/Rapptz/discord.py/blob/master/.github/CONTRIBUTING.md#ai-contributions) — the protocol's own "grep CONTRIBUTING for AI/LLM policy before raising" step (see `campaign50_targets.json`) exists precisely to catch this and didn't run for this repo in this round. **Action item: backfill the policy-grep step into the sweep runner, not just campaign50.** |

**infra_fail / bootstrap_fail (23/102, ~23%) is the single largest failure
bucket in round3** — larger than repair_fail or oracle_reject individually.
That is the honest, useful finding here: environment/bootstrap fragility
across a long tail of repos, not repair-loop quality, is currently the
biggest thing standing between forge and a higher raise rate at this
scale. Worth its own investigation before the next campaign push, ahead of
tuning the repair loop itself.

## What this changes in the existing evaluation narrative

- The "40–60-issue custom Real-Issues set" target in `Evaluation-Plan.md`
  is **exceeded on attempts (103+)**, not just "in progress" as the last
  update said — but the **outcome distribution matters more than the
  count**: ~32% oracle_reject, ~32% repair_fail, ~22% infra_fail, ~8%
  raised. Report all four, not just the raise count.
- "0 merged" is the number to lead with publicly, not "12 PRs raised" —
  raised-but-unreviewed is not yet a result. Revisit this document once
  reviews land either way.

## Not yet done

- SWE-bench Verified, baselines (aider/agent modes), ablations (graph,
  blast-radius gate, K) — unchanged from `Evaluation-Plan.md`, still
  planned.
- Cost-per-fix in aggregate — only one issue (astroid#769) has full
  token/wall-clock accounting; the sweep runner's `results_round2/3.jsonl`
  records `seconds` and `model` per attempt but not full token counts.
  Backfilling that (or logging it going forward) is a prerequisite for a
  credible $-per-fix number.
