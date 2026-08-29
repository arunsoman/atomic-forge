# GitHub Action

This repo doubles as a GitHub Action (`action.yml` + Dockerfile at the repo root) — a thin wrapper around the `atomic-forge` CLI, nothing more:

no hosted service, no bot, everything runs inside the job's own container.
This is deliberately the **only** integration this project ships — CI is
where the target audience (teams with an existing test suite) already is.

## Usage

```yaml
name: atomic-forge
on: [workflow_dispatch]

jobs:
  forge:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: arunsoman/atomic-forge@v0.1.0
        id: forge
        with:
          tasks: tasks.json
          project-dir: forge_out
          api-key: ${{ secrets.FORGE_API_KEY }}
          base-url: https://integrate.api.nvidia.com/v1   # any OpenAI-compatible endpoint
          model: openai/gpt-oss-120b

      - name: Upload generated project + trajectory
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: forge-output
          path: |
            ${{ steps.forge.outputs.project-dir }}
            ${{ steps.forge.outputs.trajectory-path }}

      - name: Fail the job if repair didn't reach green
        if: steps.forge.outputs.success != 'true'
        run: exit 1
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `command` | `run` | `run` (generate/test/repair from `tasks`) or `fix`/`fix-comment` (R8/R9 — one-shot issue/comment-driven fix, fork-only PR) |
| `tasks` | `tasks.json` | `[run]` Path to the `AtomicTaskBatch` JSON, relative to the checkout |
| `project-dir` | `forge_out` | Where files get generated / cloned into |
| `api-key` | *(required)* | Maps to `FORGE_API_KEY` — pass a secret, never a literal key |
| `base-url` | *(unset → OpenAI)* | Maps to `FORGE_BASE_URL` |
| `model` | *(unset)* | Maps to `FORGE_MODEL` |
| `test-cmd` | *(auto-detect)* | `[run]` Force a specific test command |
| `max-rounds` | `3` | Repair rounds before giving up |
| `samples` | `2` | Patch candidates sampled per repair round |
| `timeout` | `300` | Per-command timeout, seconds |
| `report` | `jsonl` | `[run]` `.forge/reports.jsonl` write-back (`jsonl` or `none`) |
| `architect` | `false` | Opt-in planner pass before each repair round — see [[Environment-Bootstrap]] |
| `issue-url` | — | `[fix]` GitHub issue URL |
| `issue-body` | — | `[fix]` The issue body, e.g. `${{ github.event.issue.body }}` — skips an in-container `gh` fetch |
| `repo` | — | `[fix-comment]` `owner/repo`, e.g. `${{ github.repository }}` |
| `comment-body` | — | `[fix-comment]` The review comment text, e.g. `${{ github.event.comment.body }}` |
| `file-path` | — | `[fix-comment]` Repo-relative file the comment was anchored to |
| `line` | — | `[fix-comment]` The line the comment was anchored to (optional) |
| `source-url` | — | `[fix-comment]` The PR/comment URL, for the PR body's "Fixes" link |
| `dry-run` | `false` | `[fix/fix-comment]` Do everything except push/open the PR |

`fix`/`fix-comment` also need a GitHub token with `repo` scope exported as
`GH_TOKEN` (the `gh` CLI's own convention — `pr.py`'s fork-only PR flow
shells out to it), e.g. `env: { GH_TOKEN: ${{ secrets.GITHUB_TOKEN }} }` at
the job or step level (a fine-grained PAT if forking across orgs).

## Outputs

| Output | Meaning |
|---|---|
| `success` | `'true'` if the repair loop reached zero failing tests, or `fix`/`fix-comment` opened (or dry-ran) a PR |
| `project-dir` | Echoes the `project-dir` input, for chaining into `upload-artifact` |
| `trajectory-path` | Path to `.forge/trajectory.jsonl`, if the run got far enough to write one |
| `pr-url` | `[fix/fix-comment]` The opened PR's URL, when `success='true'` and `dry-run` wasn't set |

## Review-comment-driven fix (R8/R9)

`command: fix-comment` answers "turn this review comment into a fix PR" —
same CIE-index → testgen → repair → fork-only-PR pipeline as `fix`, scoped
to the file (and line) the comment was left on. Wire it to a trigger
yourself (this Action never auto-triggers on anything — see "What this is
not" below); a mention-gated example:

```yaml
name: atomic-forge fix-comment
on:
  pull_request_review_comment:
    types: [created]

jobs:
  fix-comment:
    if: contains(github.event.comment.body, '@atomic-forge fix')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: arunsoman/atomic-forge@v0.1.0
        id: forge
        with:
          command: fix-comment
          repo: ${{ github.repository }}
          file-path: ${{ github.event.comment.path }}
          line: ${{ github.event.comment.line }}
          comment-body: ${{ github.event.comment.body }}
          source-url: ${{ github.event.comment.html_url }}
          api-key: ${{ secrets.FORGE_API_KEY }}
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Scope note (stated in `fix.py::run_fix_from_comment`'s own docstring too):
this targets the repo's default branch to clone and fork-PR against, NOT
the exact PR branch the comment was left on — pushing onto an arbitrary
external contributor's own PR branch is a separate, unvalidated
GitHub-permissions problem this deliberately doesn't attempt.

## What this is not

No marketplace-wide promotion, no auto-triggering on every issue/PR by
default (the example above is `workflow_dispatch` — opt-in, on purpose;
the comment-trigger example is opt-in too, gated on a mention), no
separate hosted control plane. It's the CLI, containerized, with GitHub's
input/output plumbing on top. If you want it to run on issues, PRs, or
comments automatically, wire that trigger yourself in your own workflow
file — this Action doesn't assume it for you.

## Local verification

The image builds and runs the same way GitHub runs it:

```bash
docker build -t atomic-forge-action .
docker run --rm -v "$(pwd)":/workspace -w /workspace \
  -e INPUT_TASKS=tasks.json -e INPUT_PROJECT_DIR=out \
  -e INPUT_API_KEY=$FORGE_API_KEY -e INPUT_BASE_URL=$FORGE_BASE_URL -e INPUT_MODEL=$FORGE_MODEL \
  atomic-forge-action
```
