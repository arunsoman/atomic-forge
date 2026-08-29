# R8 — Review-comment-driven fix generation

**Requirement:** Integrate natively into the existing code-review surface
(e.g. respond to review comments by generating a fix PR automatically).

**Status in atomic-forge:** Not implemented.

**✅ IMPLEMENTED 2026-08-29:** `fix.py::run_fix_from_comment` — the same
CIE-index → testgen → repair → ground-truth-recheck → fork-only-PR
pipeline as `run_fix`, refactored into a shared `_run_fix_pipeline` so
both entry points stay one implementation. The comment (+ file, + optional
line) becomes the bug description fed to test generation, prefixed with a
localization hint ("Focus on {file}..."), skipping the `gh issue view`
fetch entirely. Exposed as CLI `atomic-forge fix-comment --repo o/r --file
path --comment-body "..." [--line N] [--comment-body-file -]`. Tests:
`test_fix.py::test_run_fix_from_comment_scopes_bug_to_file`,
`test_run_fix_from_comment_uses_distinct_test_file_from_issue_fix`, plus
4 CLI-level tests in `test_cli_fix_comment.py`. Full suite green (173
passing).

**Scope, stated plainly:** this targets the repo's own default branch to
clone and fork-PR against — same as `run_fix` — NOT the exact PR branch
the comment was left on. Pushing onto an arbitrary external contributor's
own PR branch is a separate, live-GitHub-permissions problem (whose fork,
does forge have write access to *that* branch) this implementation
deliberately does not attempt to solve unvalidated. What's delivered is
"turn a review comment into a scoped fix PR against upstream," which is
the reusable, testable 90% of R8; wiring the Action-side comment trigger
(`@atomic-forge fix` in a PR comment → this function) is R9's remaining
scope, tracked there.

## State of the art

- **Issue-Oriented Agent-Based Framework for Automated Review Comment
  Generation** ([arXiv:2511.00517](https://arxiv.org/abs/2511.00517)) — frames
  the review loop as three sequential subtasks: code-change quality
  estimation → comment generation → code refinement.
- **Leveraging Reviewer Experience in Code Review Comment Generation**
  ([arXiv:2409.10959](https://arxiv.org/abs/2409.10959)) — same three-stage
  framing; reviewer-experience-weighted training improves comment relevance.
- **Retrieval-Augmented Code Review Comment Generation**
  ([arXiv:2506.11591](https://arxiv.org/abs/2506.11591)) — RAG over past
  review exemplars beats pure generation for the *comment-generation*
  subtask specifically (+1.67% exact match, +4.25% BLEU).

## Implication for atomic-forge

Forge would only need the third subtask — code refinement from an
already-written human review comment — which is the best-studied and
least novel of the three. Forge doesn't need comment *generation*
(quality estimation, writing the comment itself); it needs to consume an
existing comment as a task spec and produce a patch, which is structurally
close to forge's existing issue→patch pipeline (`fix` command) with a
review comment substituted for an issue body. Relatively low incremental
research risk if pursued.

## Implementation plan

**Phase 1 — comment-to-task adapter (~1–2 days)**
- Reusing the parser extracted in [[Multi-Channel-Intake]]'s Phase 1, add a `from_review_comment(comment_body, file, line_range)` adapter that constructs an `AtomicTask` scoped to the commented file/line, not the whole repo.

**Phase 2 — scoped repair entrypoint (~1–2 days)**
- Add a fast-path into `repair_agent.py` that skips fault-localization search when a task already carries an explicit file/line target (from Phase 1), starting the K-sampling loop directly at that location.

**Phase 3 — GitHub Action trigger (~1 day)**
- Add a `pull_request_review_comment` event trigger to `action.yml`, gated on a mention pattern (`@atomic-forge fix`), dispatching to Phase 1's adapter — implement together with [[Zero-Friction-Integration]]'s Phase 1 since both need the same trigger plumbing.

**Phase 4 — validate scoped speed/accuracy (~1 day)**
- Compare fix time and success rate for scoped (comment-driven) vs. full (issue-driven) repair on a handful of `benchmarks/` cases retrofitted with a synthetic review comment, confirming the scoped path is actually faster, not just simpler.

## Related
- [[Multi-Channel-Intake]] — a review comment is itself an intake channel