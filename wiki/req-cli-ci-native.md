# R12 — Terminal-native CLI fitting into existing CI/CD

**Requirement:** Ship as a terminal-native CLI that fits into existing
CI/CD pipelines rather than requiring a chat or web UI.

**Sourced from:** Factory.ai (Droid).

**Status in atomic-forge:** Met — `atomic-forge` is a CLI
(`pip install ... && atomic-forge --help`) with an `action.yml` for CI.

## State of the art

No dedicated research literature — CLI/CI packaging is a software
engineering/distribution practice, not an open research question.

## Implication for atomic-forge

Already met; no further action needed. Nothing to track here beyond keeping
`action.yml` and the CLI surface in sync with new capabilities as they land
(e.g. if [[req-review-comment-driven-fix]] is added, the Action needs a
comment-trigger entry point too).

## What needs to be done (to beat the competition)

**Maintenance only, not new work.** Add a checklist item to the PR template
for any new capability: "does `action.yml` expose this?" — the risk here
isn't building the CLI/CI surface (done), it's letting it drift out of sync
as [[req-review-comment-driven-fix]] and [[req-multi-channel-intake]] add new
entry points that need their own Action triggers.

## Implementation plan

**One step, no phases:** add the checklist line to `CONTRIBUTING.md`/the PR
template now, so it's already in place by the time the other requirements'
plans start landing new CLI/Action surface.
