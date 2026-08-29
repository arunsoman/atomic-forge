# Docker image for the atomic-forge GitHub Action (see action.yml).
# Not the recommended way to install/run atomic-forge locally — use
# `pip install -e ".[dev]"` for that (see README "Install"). This image
# exists only to give the Action a self-contained, reproducible runtime.
FROM python:3.11-slim

# git is not optional here: sandbox.py's ensure_repo()/per-task commits
# silently no-op without it (confirmed live — see docs/github-action.md).
# gh is required by `command: fix` / `command: fix-comment` (pr.py's
# fork-only PR flow shells out to it) — installed via the official apt
# repo since Debian slim's own repos don't carry it. Harmless (unused,
# no extra runtime cost beyond image size) for the default `command: run`
# path, which never calls gh.
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
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

# CIE (https://github.com/arunsoman/cie) + the `mcp` python client are
# REQUIRED (not optional) for `command: fix` / `command: fix-comment` —
# see fix.py's module docstring and cie_backend.py::require_cie(), which
# checks for both. Installed here, not left to a runtime pip install,
# since the Action's container is immutable once built — `command: run`
# (the default) never imports either, so this only costs image size, not
# functionality, for run-only users.
RUN pip install --no-cache-dir "git+https://github.com/arunsoman/cie.git" mcp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
