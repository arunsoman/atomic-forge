#!/usr/bin/env bash
# Publish ./wiki/*.md to the GitHub wiki (github.com/arunsoman/atomic-forge/wiki).
#
# One-time setup (GitHub has no API for this): open
#   https://github.com/arunsoman/atomic-forge/wiki
# click "Create the first page", save anything (e.g. "wiki") — this
# initializes the wiki git repo. Then run this script any time wiki/ changes.
#
# The wiki/ directory in this repo is the source of truth; the GitHub wiki
# is the published copy.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIKI_SRC="$REPO_DIR/wiki"
WIKI_URL="https://github.com/arunsoman/atomic-forge.wiki.git"
WIKI_DIR="${TMPDIR:-/tmp}/atomic-forge-wiki"

if [[ ! -d "$WIKI_SRC" ]]; then
  echo "error: $WIKI_SRC not found" >&2; exit 1
fi

if [[ -d "$WIKI_DIR/.git" ]]; then
  git -C "$WIKI_DIR" pull --ff-only -q
else
  rm -rf "$WIKI_DIR"
  git clone -q "$WIKI_URL" "$WIKI_DIR"
fi

# remove files deleted/renamed in the source tree, then copy current pages
for f in "$WIKI_DIR"/*.md; do
  b="$(basename "$f")"
  [[ -f "$WIKI_SRC/$b" ]] || rm -f "$f"
done
cp -f "$WIKI_SRC"/*.md "$WIKI_DIR/"
git -C "$WIKI_DIR" add -A
if git -C "$WIKI_DIR" diff --cached --quiet; then
  echo "wiki: no changes to publish"
else
  git -C "$WIKI_DIR" commit -q -m "sync wiki from repo ($(date -u +%Y-%m-%dT%H:%MZ))"
  git -C "$WIKI_DIR" push -q origin master
  echo "wiki: published $(ls "$WIKI_SRC" | grep -c '\.md$') pages"
fi