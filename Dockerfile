# Docker image for the atomic-forge GitHub Action (see action.yml).
# Not the recommended way to install/run atomic-forge locally — use
# `pip install -e ".[dev]"` for that (see README "Install"). This image
# exists only to give the Action a self-contained, reproducible runtime.
FROM python:3.11-slim

# git is not optional here: sandbox.py's ensure_repo()/per-task commits
# silently no-op without it (confirmed live — see docs/github-action.md).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /action

# Build context is this repo's own checkout — the Action always ships the
# exact commit it's pinned to, no separate publish step required.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
# stacks.py's bare-pytest branch (no requirements.txt/pyproject.toml in
# the *generated* project) shells out to `python -m pytest` directly,
# assuming it's already importable — confirmed live: without this, the
# repair loop "fixes" a missing test runner by hallucinating a fake
# pytest module instead of getting a real signal. The venv branch (when a
# requirements file IS present) already self-installs these, so this is
# only a fallback for the bare case.
RUN pip install --no-cache-dir pytest pytest-asyncio

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
