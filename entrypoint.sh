#!/usr/bin/env bash
# Entrypoint for the atomic-forge GitHub Action. Docker actions get their
# `with:` inputs as INPUT_<NAME> env vars (GitHub uppercases and replaces
# '-' with '_'); this maps them onto the plain `atomic-forge` CLI so the
# Action stays a thin wrapper, not a second implementation to keep in sync.
set -euo pipefail

: "${INPUT_COMMAND:=run}"
: "${INPUT_TASKS:=tasks.json}"
: "${INPUT_PROJECT_DIR:=forge_out}"
: "${INPUT_MAX_ROUNDS:=3}"
: "${INPUT_SAMPLES:=2}"
: "${INPUT_TIMEOUT:=300}"
: "${INPUT_TEST_CMD:=}"
: "${INPUT_REPORT:=jsonl}"
: "${INPUT_ARCHITECT:=false}"
: "${INPUT_DRY_RUN:=false}"
: "${INPUT_FORCE:=false}"

if [ -z "${INPUT_API_KEY:-}" ]; then
  echo "::error::atomic-forge Action: 'api-key' input is required (pass a repo/org secret, e.g. secrets.FORGE_API_KEY)"
  exit 1
fi
export FORGE_API_KEY="$INPUT_API_KEY"
[ -n "${INPUT_BASE_URL:-}" ] && export FORGE_BASE_URL="$INPUT_BASE_URL"
[ -n "${INPUT_MODEL:-}" ] && export FORGE_MODEL="$INPUT_MODEL"
[ -n "${INPUT_MODEL_FALLBACKS:-}" ] && export FORGE_MODEL_FALLBACKS="$INPUT_MODEL_FALLBACKS"

# `gh` (required by `fix`/`fix-comment`'s fork-only PR flow, pr.py) picks
# up GH_TOKEN/GITHUB_TOKEN from the environment automatically — the
# workflow exports one of those (e.g. `env: { GH_TOKEN: ${{ secrets.
# GITHUB_TOKEN }} }` or a PAT with repo scope for the fork step) with no
# extra plumbing needed here.

arch_flag=()
[ "$INPUT_ARCHITECT" = "true" ] && arch_flag+=(--architect)
dry_run_flag=()
[ "$INPUT_DRY_RUN" = "true" ] && dry_run_flag+=(--dry-run)
force_flag=()
[ "$INPUT_FORCE" = "true" ] && force_flag+=(--force)

case "$INPUT_COMMAND" in
  run)
    args=(run --tasks "$INPUT_TASKS" --project-dir "$INPUT_PROJECT_DIR"
          --max-rounds "$INPUT_MAX_ROUNDS" --samples "$INPUT_SAMPLES"
          --timeout "$INPUT_TIMEOUT" --report "$INPUT_REPORT" "${arch_flag[@]}")
    [ -n "$INPUT_TEST_CMD" ] && args+=(--test-cmd "$INPUT_TEST_CMD")
    ;;
  fix)
    : "${INPUT_ISSUE_URL:?'issue-url' input is required for command: fix}"
    args=(fix "$INPUT_ISSUE_URL" --max-rounds "$INPUT_MAX_ROUNDS" --samples "$INPUT_SAMPLES"
          "${arch_flag[@]}" "${dry_run_flag[@]}" "${force_flag[@]}")
    if [ -n "${INPUT_ISSUE_BODY:-}" ]; then
      # Prefer the body the triggering workflow already has (e.g.
      # ${{ github.event.issue.body }}) over an in-container `gh` fetch —
      # one less network call, and works even with a minimal GH_TOKEN
      # scope that can push/PR but wasn't granted issue-read.
      body_file="$(mktemp)"
      printf '%s' "$INPUT_ISSUE_BODY" > "$body_file"
      args+=(--issue-body-file "$body_file")
    fi
    ;;
  fix-comment)
    : "${INPUT_REPO:?'repo' input is required for command: fix-comment}"
    : "${INPUT_FILE_PATH:?'file-path' input is required for command: fix-comment}"
    : "${INPUT_COMMENT_BODY:?'comment-body' input is required for command: fix-comment}"
    args=(fix-comment --repo "$INPUT_REPO" --file "$INPUT_FILE_PATH"
          --comment-body "$INPUT_COMMENT_BODY" --max-rounds "$INPUT_MAX_ROUNDS"
          --samples "$INPUT_SAMPLES" "${arch_flag[@]}" "${dry_run_flag[@]}" "${force_flag[@]}")
    [ -n "${INPUT_LINE:-}" ] && args+=(--line "$INPUT_LINE")
    [ -n "${INPUT_SOURCE_URL:-}" ] && args+=(--source-url "$INPUT_SOURCE_URL")
    ;;
  *)
    echo "::error::atomic-forge Action: unknown command '$INPUT_COMMAND' (expected run, fix, or fix-comment)"
    exit 1
    ;;
esac

# Tee to the job log (so the run is still readable live / in the Actions
# UI) while also capturing it so `fix`/`fix-comment`'s machine-parseable
# `pr-url=...` line (see cli.py) can be lifted into the Action's own
# `pr-url` output — set -o pipefail so `status` is atomic-forge's real
# exit code, not tee's.
set +e
set -o pipefail
output_log="$(mktemp)"
atomic-forge "${args[@]}" | tee "$output_log"
status=$?
set +o pipefail
set -e

trajectory_path="$INPUT_PROJECT_DIR/.forge/trajectory.jsonl"
[ -f "$trajectory_path" ] || trajectory_path=""

pr_url="$(grep -o '^\[forge\] pr-url=.*' "$output_log" | tail -1 | sed 's/^\[forge\] pr-url=//')"
rm -f "$output_log"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "success=$([ "$status" -eq 0 ] && echo true || echo false)"
    echo "project-dir=$INPUT_PROJECT_DIR"
    echo "trajectory-path=${trajectory_path:-}"
    echo "pr-url=${pr_url:-}"
  } >> "$GITHUB_OUTPUT"
fi

exit $status
