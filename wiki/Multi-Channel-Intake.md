# R7 — Multi-channel task intake

**Requirement:** Accept task intake from multiple channels (chat/Slack,
issue tracker, direct UI).

**Status in atomic-forge:** Partial, verified 2026-08-29 — `atomic-forge fix
<issue-url>` covers GitHub issue intake, and `--issue-body-file` already
lets a caller supply the bug text directly instead of fetching via `gh`
(the URL is still required for owner/repo/number, so this isn't quite
free-text intake yet). No stdin/`--text` path, no webhook/chat intake.

**✅ IMPLEMENTED 2026-08-29 (stdin path):** `--issue-body-file -` (a literal
dash) now reads the bug text from stdin instead of a file —
`echo "bug text" | atomic-forge fix <url> --issue-body-file -`, no
filesystem step. Note on scope: `fix`'s architecture structurally requires
the GitHub URL regardless (it clones and PRs against a real repo — this
isn't the `run`/`decompose` AtomicTask pipeline this doc's original plan
assumed), so "free-text intake" for `fix` specifically means "skip `gh`
fetch," not "skip the URL too." Test: `test_fix.py::
test_run_fix_issue_body_from_stdin`. Webhook/chat-channel intake remains
unimplemented — still low-priority pending demonstrated demand, per this
doc's own guidance.

## State of the art

No dedicated research literature — this is a distribution/product-surface
decision (which platforms to integrate with), not an open research problem.
The interesting research (turning an issue description into a task
specification) is already covered by [[Execution-Guided-Repair]] and
[[Repo-Scale-Context]]; the "which inbox does it arrive from" question
sits entirely in integration/engineering work.

## Implication for atomic-forge

Low priority relative to the other requirements — worth doing only if there's
demonstrated user demand for non-GitHub-issue intake (Slack, CLI-direct
task description, etc.), since it adds no new capability to the repair loop
itself, only a new entry point into the existing `fix` command.

## Implementation plan

**Phase 1 — extract the parser (~1 day)**
- In the `fix` command's implementation, separate "fetch issue body from a GitHub URL" from "turn issue text into an `AtomicTask`" into two distinct functions, if not already separated.

**Phase 2 — text/stdin intake (~0.5 day)**
- Add a `--text`/stdin path to `fix` that skips the GitHub fetch and calls the parser directly — immediately unblocks CLI-direct task description with near-zero new code.

**Phase 3 — webhook adapter (~2–3 days, only if demand is demonstrated)**
- A minimal HTTP receiver (Slack slash-command or generic webhook) that extracts text from the incoming payload and calls the same parser from Phase 1, then dispatches to `fix`'s existing pipeline.
- Do not build this speculatively — gate on an actual user request, per the note above.