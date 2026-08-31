# Real-issues campaign — consolidated results (as of 2026-08-31)

Regenerate the numbers below with `python benchmarks/real_issues/reconcile.py`
— it reads every round's result ledger (round1 pilot through round4),
dedupes issues attempted more than once, and checks live PR status via
`gh`. Don't hand-edit the counts in this file; hand-editing across
multiple docs from multiple jsonl files is exactly how this document
drifted out of sync with `benchmarks/README.md` in the first place (this
file was written before `results_round4.jsonl` existed and was never
revisited — see git history).

> **Reporting policy (changed 2026-08-31):** this document now reports PR
> outcomes only — raised / open / closed / merged. It no longer publishes
> the internal outcome-distribution breakdown (oracle_reject / repair_fail
> / infra_fail rates) that earlier versions of this file led with. That
> breakdown is still logged in full in `sweep/results_round*.jsonl` and
> `campaign_log.md` for anyone auditing the harness itself — it's just no
> longer part of what this file publishes as the headline.

## Headline

- **120 tracked real-issue attempts** (deduped across rounds) across
  **~20 popular Python repos**.
- **18 real PRs raised**, live-checked via `gh` on 2026-08-31: **11 open,
  7 closed without merge, 0 merged.**
- This is real, live, external-facing evidence — and it is not yet a
  "success rate" claim. Nothing here should be published as more than: 18
  real PRs raised against real open issues in real repos, patches that
  passed the repo's own test suite plus a generated regression test,
  currently under human review (or already declined).

## The 18 PRs raised, live status

| PR | Live state | Note |
|---|---|---|
| psf/black#5370, #5371, #5372 | OPEN | 3 separate fixes, same repo |
| python-trio/trio#3498 | OPEN | |
| dateutil/dateutil#1554, #1555, #1556 | OPEN | 3 separate fixes, same repo |
| mahmoud/glom#317 | OPEN | |
| more-itertools/more-itertools#1243 | OPEN | |
| python-babel/babel#1334 | OPEN | supersedes #1333 (closed — reopened from the org fork instead of a personal account, same commit) |
| jd/tenacity#705 | OPEN | supersedes #704 (closed, same reason) |
| Delgan/loguru#1505 | CLOSED, not merged | no maintainer comment on record; treat as a real rejection until shown otherwise |
| Rapptz/discord.py#10507 | CLOSED, not merged | closed citing the repo's [AI-contributions policy](https://github.com/Rapptz/discord.py/blob/master/.github/CONTRIBUTING.md#ai-contributions); `pr_writable.py`'s `check_ai_policy()` now catches this class before an attempt is spent |
| pylint-dev/pylint#11371 | CLOSED, not merged | |
| pylint-dev/astroid#3261, #3262, #3263, #3264 | CLOSED, not merged | hand-run exploration outside the ledgered sweep, see `campaign_log.md` |

## Streams

- **`benchmarks/cie_forge_realbugs/`** — 4/4 historical bugs (already
  fixed upstream) independently re-derived by forge and oracle-scored
  against the real merged fix. Oracle-accuracy check, not a live-PR
  stream.
- **`campaign50.ledger.jsonl`** — 1 fully-ledgered attempt
  (`pylint-dev/astroid#769`), full token/wall-clock accounting: 151 LLM
  calls, ~2.1M tokens, ~43 min wall-clock — the clearest per-attempt cost
  signal available so far. Plus 4 hand-run astroid PRs (above).
- **`sweep/results.jsonl` → `results_round4.jsonl`** — the main campaign,
  120 deduped attempts across ~20 repos, 14 of the 18 PRs above.

Repos touched: discord.py, anyio, arrow, gunicorn, celery, kombu, pint,
ipython, pip-tools, networkx, paramiko, black, pip, setuptools, pytest,
trio, sphinx, tqdm, urllib3, babel, tenacity, cleo, httpcore, dateutil,
glom, more-itertools, pylint, astroid, and others.

Several real forge bugs were found and fixed along the way — see
`campaign_log.md` and `goal.md`'s session addendum for the full
root-cause writeups (AI-policy detection, maintainer-already-rejected
skip, verified-green-but-uncommitted fix, model-fallback on quota
exhaustion, failure-category granularity). All covered by regression
tests.

## Not yet done

- SWE-bench Verified, baselines (aider/agent modes), ablations (graph,
  blast-radius gate, K) — see `goal.md` for current sequencing.
- Cost-per-fix in aggregate — only `campaign50.ledger.jsonl`
  (astroid#769) has full token/wall-clock accounting. The sweep runner's
  `results_round*.jsonl` files already log `llm_calls`/token counts
  inline in each attempt's `log_tail` string; extracting those into
  structured fields is a parsing task, not a re-run — next up.
