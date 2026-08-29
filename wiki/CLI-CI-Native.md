# R12 — Terminal-native CLI fitting into existing CI/CD

**Requirement:** Ship as a terminal-native CLI that fits into existing
CI/CD pipelines rather than requiring a chat or web UI.

**Status in atomic-forge:** Met — `atomic-forge` is a CLI
(`pip install ... && atomic-forge --help`) with an `action.yml` for CI.

## State of the art

No dedicated research literature — CLI/CI packaging is a software
engineering/distribution practice, not an open research question.

## Implication for atomic-forge

Already met; no further action needed. Nothing to track here beyond keeping
`action.yml` and the CLI surface in sync with new capabilities as they land
(e.g. if [[Review-Comment-Driven-Fix]] is added, the Action needs a
comment-trigger entry point too).

## Implementation plan

**One step, no phases:** add the checklist line to `CONTRIBUTING.md`/the PR
template now, so it's already in place by the time the other requirements'
plans start landing new CLI/Action surface.