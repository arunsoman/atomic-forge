# R9 — Zero-friction platform integration

**Requirement:** Require zero new developer tooling/workflow — operate
entirely inside the host platform (GitHub Actions, PR comments) rather than
a separate product.

**Sourced from:** GitHub Copilot coding agent, Sweep.dev.

**Status in atomic-forge:** Partial, verified 2026-08-29 — ships as a
GitHub Action (`action.yml`, Docker-based, thin wrapper over the CLI per
`entrypoint.sh`) and a `fix` CLI subcommand producing a fork-only PR. Gap
is more specific than originally scoped: `action.yml` only wraps the
`run` phase today (`tasks.json` → generate/qa/repair) — it has no `command`
input to invoke `fix` at all, so today's Action can't do issue-URL-driven
fixing even manually, let alone from a comment trigger.

**✅ IMPLEMENTED 2026-08-29:** `action.yml` gained a `command` input
(`run`/`fix`/`fix-comment`) plus the inputs each needs (`issue-url`,
`issue-body`, `repo`, `comment-body`, `file-path`, `line`, `source-url`,
`dry-run`) and a `pr-url` output. `entrypoint.sh` dispatches on
`INPUT_COMMAND`, builds the right CLI invocation per command (with clear
`::error::`s on missing required inputs), and greps a machine-parseable
`[forge] pr-url=...` line (added to `cli.py`'s `fix`/`fix-comment`
handlers) out of the run to populate the `pr-url` output. `Dockerfile`
now installs `gh` (apt, official repo) + CIE (`git+https://github.com/
arunsoman/cie.git`) + `mcp` — all three required for `fix`/`fix-comment`
to function inside the Action's container, previously entirely absent.

**Verified by actually building and running the image**, not just
reading the YAML: `docker build` succeeds; `gh --version`, `python -c
"import mcp, cie.mcp_server"` both work inside the built image; all three
commands (`run`, `fix`, `fix-comment`) were run with missing-required-
input combinations and confirmed to fail with the correct, specific
`::error::`/exit-2 messages; `fix`/`fix-comment` with valid inputs were
confirmed to progress all the way through `require_cie()` (previously the
first thing that would fail) to the real `git clone` step (fails only
because the test used a placeholder `o/r` repo — the correct, expected
failure for that input, not a wiring bug). Docs: `docs/github-action.md`
updated with the new inputs/outputs table and a full comment-triggered
workflow example (`pull_request_review_comment` + mention gate).

Remaining, explicitly out of scope here: this repo's own `.github/
workflows/` doesn't need the comment-trigger workflow itself (that's for
*consuming* repos to add, per this Action's own "What this is not" — it
never auto-triggers on anything by default); the docs example is the
reference a consumer copies.

## State of the art

No meaningful academic literature — this is purely a distribution/adoption
decision, not a research problem. The nearest genuinely researched adjacent
topic is developer trust/adoption of AI-generated PRs (human factors, not
computer science per se), which isn't sourced here since it wasn't part of
the original competitive scan.

## Implication for atomic-forge

Already substantially met via `action.yml` + the `fix` CLI. The remaining
gap (in-PR-comment triggering, e.g. `@atomic-forge fix this`) is a small,
well-scoped engineering task, not a research gap — pairs naturally with
[[Environment-Bootstrap]] if that requirement is pursued, since both
need the same "listen for a GitHub comment, dispatch a fix run" plumbing.

## What needs to be done (to beat the competition)

1. **Add an `issue_comment`/`pull_request_review_comment` trigger to
   `action.yml`** alongside the existing entry points, gated on a mention
   pattern (e.g. `@atomic-forge fix`), dispatching to the same `fix` CLI
   path — this single change closes both R8 and R9's remaining gap at once.
2. **No new product surface.** Resist building a dashboard/web UI to compete
   on "polish" — Copilot and Sweep's edge here is specifically *not*
   requiring a new surface; matching that means staying inside GitHub
   Actions + PR comments, not adding one.

## Implementation plan

**Phase 1 — Action trigger (~1 day, shared with [[Environment-Bootstrap]])**
- Add the `issue_comment` / `pull_request_review_comment` event trigger and mention-pattern gate to `action.yml`.

**Phase 2 — dispatch wiring (~0.5 day)**
- Route the triggered event to the existing `fix` CLI (issue-comment case) or the new scoped adapter (review-comment case), reusing whichever pipeline already exists by the time this ships.

**Phase 3 — docs (~0.5 day)**
- Update README/action docs with the mention-trigger usage example, keeping the "zero new tooling" framing explicit and comparing directly to Copilot's `@copilot` pattern.
