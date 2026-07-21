#!/usr/bin/env bash
# Keep the HolaFresca testing checkout pinned to the head of the integration
# branch. That branch is the cumulative "Testing" stack, rebuilt locally by
# ticket-runner in the main HolaFresca repo
# (~/Documents/Programming/AI/HolaFresca), which is this checkout's git `origin`
# -- so we sync from origin, not GitHub.
#
# Default: fetch + hard-reset the checkout to the integration head, then reinstall
# Python deps / rebuild the frontend only when their inputs changed. With
# --restart, also restart the LAN service, but only when the head actually moved.
# Used both as the service's ExecStartPre (no --restart, so a manual/boot start
# lands on head) and by the sync timer (--restart, to pick up new deploys).
#
# This script is served from within the testing checkout at deploy/, so a
# `git reset --hard` restores it rather than losing it.
set -euo pipefail

# When invoked from the main repo's post-commit hook, git exports GIT_DIR etc.
# pointing at the committing repo; clear them so our git commands act on the
# testing checkout below, not on whoever called us.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX 2>/dev/null || true

repo="$HOME/Documents/Programming/AI/HolaFresca-testing"
branch="integration/hola-fresca"
service="holafresca-testing.service"
cd "$repo"

before="$(git rev-parse HEAD)"
git fetch --quiet origin "$branch"
target="$(git rev-parse FETCH_HEAD)"

if [ "$before" = "$target" ]; then
  exit 0
fi

git reset --hard --quiet "$target"

# Reinstall Python deps only when requirements.txt changed between the two heads.
if ! git diff --quiet "$before" "$target" -- requirements.txt; then
  ./.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
fi

# Rebuild the frontend only when the frontend changed (any file under frontend/).
if ! git diff --quiet "$before" "$target" -- frontend; then
  if ! git diff --quiet "$before" "$target" -- frontend/package-lock.json; then
    npm --prefix frontend ci --silent
  fi
  npm --prefix frontend run build --silent
fi

if [ "${1:-}" = "--restart" ]; then
  systemctl --user restart "$service"
fi
