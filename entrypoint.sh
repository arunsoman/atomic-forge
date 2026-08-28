#!/usr/bin/env bash
# Entrypoint for the atomic-forge GitHub Action. Docker actions get their
# `with:` inputs as INPUT_<NAME> env vars (GitHub uppercases and replaces
# '-' with '_'); this maps them onto the plain `atomic-forge` CLI so the
# Action stays a thin wrapper, not a second implementation to keep in sync.
set -euo pipefail

: "${INPUT_TASKS:=tasks.json}"
: "${INPUT_PROJECT_DIR:=forge_out}"
: "${INPUT_MAX_ROUNDS:=3}"
: "${INPUT_SAMPLES:=2}"
: "${INPUT_TIMEOUT:=300}"
: "${INPUT_TEST_CMD:=}"
: "${INPUT_REPORT:=jsonl}"

if [ -z "${INPUT_API_KEY:-}" ]; then
  echo "::error::atomic-forge Action: 'api-key' input is required (pass a repo/org secret, e.g. secrets.FORGE_API_KEY)"
  exit 1
fi
export FORGE_API_KEY="$INPUT_API_KEY"
[ -n "${INPUT_BASE_URL:-}" ] && export FORGE_BASE_URL="$INPUT_BASE_URL"
[ -n "${INPUT_MODEL:-}" ] && export FORGE_MODEL="$INPUT_MODEL"

args=(run --tasks "$INPUT_TASKS" --project-dir "$INPUT_PROJECT_DIR"
      --max-rounds "$INPUT_MAX_ROUNDS" --samples "$INPUT_SAMPLES"
      --timeout "$INPUT_TIMEOUT" --report "$INPUT_REPORT")
[ -n "$INPUT_TEST_CMD" ] && args+=(--test-cmd "$INPUT_TEST_CMD")

set +e
atomic-forge "${args[@]}"
status=$?
set -e

trajectory_path="$INPUT_PROJECT_DIR/.forge/trajectory.jsonl"
[ -f "$trajectory_path" ] || trajectory_path=""

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "success=$([ "$status" -eq 0 ] && echo true || echo false)"
    echo "project-dir=$INPUT_PROJECT_DIR"
    echo "trajectory-path=${trajectory_path:-}"
  } >> "$GITHUB_OUTPUT"
fi

exit $status
