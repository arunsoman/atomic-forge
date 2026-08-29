`atomic-forge <phase> [options]` — full surface of the CLI. Source of truth: [`cli.py`](https://github.com/arunsoman/atomic-forge/blob/main/src/atomic_forge/cli.py).

## Phases

| Phase | What it does |
|---|---|
| `run` | The full generate → qa → repair pipeline over a task batch |
| `generate` | Only the generation phase |
| `qa` | Only test synthesis from `test_triad`s |
| `repair` | Only the repair loop over existing failures (supports `--raise-pr`) |
| `decompose` | Draft an `AtomicTaskBatch` from a loose spec (`--spec`, `--out`) |
| `watch` | Production [[Watchdog]]: detect → repair → canary → promote/rollback |
| `fix` | One-shot issue → regression test → repair → fork-only PR ([[Issue-to-PR]]) |
| `fix-comment` | Same pipeline driven by a PR review comment ([[Issue-to-PR]]) |

## Common flags

| Flag | Applies to | Meaning |
|---|---|---|
| `--tasks` | run/generate/qa/repair | Task batch JSON (default `tasks.json`) |
| `--project-dir` | all | Target project directory (default `./forge_out`) |
| `--test-cmd` | run/repair/fix | Force a test command instead of auto-detection |
| `--backend local\|graph` | all (tool backend) | `local` = in-memory symbol index; `graph` = persisted SQLite call graph at `.forge/codegraph.db` |
| `--max-rounds` | repair/fix | Repair rounds (default: 3 for repair, 5 for fix) |
| `--samples` | repair/fix | Patch candidates K per round (default 2) |
| `--architect` | repair/fix | Opt-in planner pass before each round's K-sampling (one extra LLM call; default off — see [[req-planner-executor-split]]) |
| `--local-only` | all | Refuse non-loopback LLM endpoints ([[req-data-privacy-no-training]]) |
| `--report jsonl` | run | Write artifacts/status/repair events to `.forge/reports.jsonl` |
| `--timeout` | run | Per-phase seconds (default 300) |

## `fix` flags

| Flag | Meaning |
|---|---|
| `url` | Positional GitHub issue URL |
| `--dry-run` | Do everything except push to the fork / open the PR |
| `--issue-body-file` | Local file (or `-` for stdin) as the issue body instead of `gh` fetch |
| `--project-dir` | Use a checkout you've already set up (skips the bootstrap gate — you've vouched for it) |
| `--install-cmd` | Override the project install command; pass `""` to skip installing |
| `--max-turns` | Max test-generation agent turns (default 10) |
| `--pr-base` / `--pr-branch` / `--pr-title` | PR targeting |
| `--skip-bootstrap` | Skip the [[Bootstrap-Gate]] on a cold clone whose suite you already know runs |
| `--bootstrap-timeout` | Seconds the gate's test probe may run (default 600) |

## `raise-pr` flags (`repair`)

| Flag | Meaning |
|---|---|
| `--raise-pr` | After a green repair, push the fix on a fresh branch and open a GitHub PR via `gh` |
| `--pr-base` | PR base branch (default: repo default) |
| `--pr-branch` | Feature branch name (default `forge/fix-<ts>`) |
| `--pr-title` | Override the PR title |
| `--pr-body-file` | Path to a markdown PR body |

## `watch` flags

| Flag | Meaning |
|---|---|
| `--log-file` | Log file to tail for tracebacks |
| `--deploy-cmd` | Shell-quoted argv to start the app with a `{port}` token, e.g. `'python app.py {port}'`. Omit to patch+commit with no canary phase |
| `--health-path` | HTTP path the canary is health-checked on (default `/`) |
| `--canary-percent` | Traffic % sent to the canary (default 10) |
| `--health-checks` | Consecutive healthy checks required to promote (default 5) |
| `--poll-interval` | Seconds between log polls (default 5.0) |
| `--max-cycles` | Stop after N poll cycles (omit to run forever) |

## `fix-comment` flags

| Flag | Meaning |
|---|---|
| `--repo` | `owner/repo` the PR lives in |
| `--comment-body` / `--comment-body-file` | The review comment text (file may be `-` for stdin) |
| `--file` / `--line` | The file/line the comment was anchored to |
| `--source-url` | The PR/comment URL, used for the PR body's `Fixes` link |

## Environment variables

See [[Installation-and-LLM-Setup]] for the full LLM resolution order:
`FORGE_MOCK`, `FORGE_API_KEY`, `FORGE_BASE_URL`, `FORGE_MODEL`,
`OPENAI_API_KEY` (+ `OPENAI_BASE_URL`/`OPENAI_MODEL`), and
`FORGE_ENABLE_AGENTIC_BOOTSTRAP` ([[Bootstrap-Gate]] agentic fallback, opt-in).