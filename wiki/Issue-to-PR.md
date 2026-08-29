`atomic-forge fix <github_issue_url>` is the fully autonomous one-shot:
hand it an issue, and (with CIE + an LLM endpoint in your env) it fetches
the issue, clones the repo, **generates a failing regression test from the
issue text**, repairs against that test, and on green opens a PR — from
your **fork**, never pushing to `origin`.

```bash
export FORGE_MODEL=qwen3.5:cloud FORGE_BASE_URL=http://localhost:11434/v1 FORGE_API_KEY=ollama
pip install git+https://github.com/arunsoman/atomic-forge.git \
            git+https://github.com/arunsoman/cie.git pytest

atomic-forge fix https://github.com/mahmoud/boltons/issues/123 --dry-run   # try without pushing
atomic-forge fix https://github.com/mahmoud/boltons/issues/123            # fix + open PR from your fork
```

## The pipeline, in order

1. **preflight** — bails immediately if the LLM env or CIE isn't available
   (CIE is **required** for `fix`, not optional).
2. **fetch + clone** — reads the issue via `gh`, shallow-clones the repo.
3. **bootstrap** — the [[Bootstrap-Gate]] proves "at least one test in this
   repo is discoverable and executable" before any repair logic runs:
   deterministic stack probe (6 ecosystems), and — opt-in, Docker-only — a
   Repo2Run-style agentic fallback for repos no registered stack detects.
4. **CIE populates the graph** — `cie index` the checkout; CIE serves graph
   tools over MCP ([Statement-Level-Graph]]-aware localization).
5. **regression test generated from the issue text**, grounded in the real
   signatures, and the harness validates it reproduces the bug (fails on
   the buggy code on an *assertion* — a collection/import error or a test
   that passes on buggy code is rejected and **no PR is raised**).
6. **forge repairs** — the real [[Repair-Loop]] re-runs that test each
   round until green or `--max-rounds` (default 5).
7. **PR from your fork** — on green, forks the repo, pushes the fix branch
   to the **fork only** (never to `origin`), and opens `fork → upstream`
   via `gh`.

`--dry-run` does everything except the push/PR. Local file as issue body:
`--issue-body-file bug.txt` (or `-` for stdin).

## `fix-comment`: the same pipeline from a review comment

PR-review-comment-driven fixes ([[req-review-comment-driven-fix]]): scoped
to the commented file/line instead of a full issue.

```bash
atomic-forge fix-comment --repo owner/repo \
  --comment-body "This throws TypeError when the list is empty" \
  --file src/queue.py --line 88
```

## Safety properties

- **Fork-only pushes.** The fix branch goes to your fork; `origin` is never
  pushed to. The gate to raising anything is a *validated failing
  regression test*, not model confidence.
- **Reproducible validation** — the generated test must fail on the buggy
  code (assertion failure, not collection error) before repair starts; a
  fix is only PR-able if it turns that test green *and* the repo's own
  suite stays green.
- **Every action is on the record** — append-only JSONL trajectory
  ([[Checkpointing-and-Resumability]] for the run record).