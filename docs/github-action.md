# GitHub Action

This repo doubles as a GitHub Action (`action.yml` + `Dockerfile` at the
repo root) — a thin wrapper around the `atomic-forge` CLI, nothing more:
no hosted service, no bot, everything runs inside the job's own container.
This is deliberately the **only** integration this project ships (see
`docs/adoption-and-distribution-plan.md`, objective 4) — CI is where the
target audience (teams with an existing test suite) already is.

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
| `tasks` | `tasks.json` | Path to the `AtomicTaskBatch` JSON, relative to the checkout |
| `project-dir` | `forge_out` | Where files get generated |
| `api-key` | *(required)* | Maps to `FORGE_API_KEY` — pass a secret, never a literal key |
| `base-url` | *(unset → OpenAI)* | Maps to `FORGE_BASE_URL` |
| `model` | *(unset)* | Maps to `FORGE_MODEL` |
| `test-cmd` | *(auto-detect)* | Force a specific test command |
| `max-rounds` | `3` | Repair rounds before giving up |
| `samples` | `2` | Patch candidates sampled per repair round |
| `timeout` | `300` | Per-command timeout, seconds |
| `report` | `jsonl` | `.forge/reports.jsonl` write-back (`jsonl` or `none`) |

## Outputs

| Output | Meaning |
|---|---|
| `success` | `'true'` if the repair loop reached zero failing tests |
| `project-dir` | Echoes the `project-dir` input, for chaining into `upload-artifact` |
| `trajectory-path` | Path to `.forge/trajectory.jsonl`, if the run got far enough to write one |

## What this is not

No marketplace-wide promotion, no auto-triggering on every issue/PR by
default (the example above is `workflow_dispatch` — opt-in, on purpose),
no separate hosted control plane. It's the CLI, containerized, with
GitHub's input/output plumbing on top. If you want it to run on issues or
PRs automatically, wire that trigger yourself in your own workflow file —
this Action doesn't assume it for you.

## Local verification

The image builds and runs the same way GitHub runs it:

```bash
docker build -t atomic-forge-action .
docker run --rm -v "$(pwd)":/workspace -w /workspace \
  -e INPUT_TASKS=tasks.json -e INPUT_PROJECT_DIR=out \
  -e INPUT_API_KEY=$FORGE_API_KEY -e INPUT_BASE_URL=$FORGE_BASE_URL -e INPUT_MODEL=$FORGE_MODEL \
  atomic-forge-action
```
