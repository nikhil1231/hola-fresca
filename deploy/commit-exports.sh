#!/usr/bin/env bash
# Commit and push the nightly mapping export.
#
# Runs from holafresca-backup.service, straight after `app.backup
# export-mappings` rewrites exports/*.csv. Committing those was deliberately
# manual while the box was the machine you worked on. It is not that machine any
# more — it is only ever deployed to — and manual has become never.
#
# The invariant that shapes everything here: this must never leave an unpushed
# commit behind. HEAD sitting ahead of origin is harmless by itself (the deploy
# treats it as already deployed), but the next push from the laptop makes the
# two diverge, and a diverged tree is one the deploy timer skips silently. The
# backstop would be dead with nothing to say so. Hence: the commit and the push
# are one operation, and a failed push takes the commit back down with it.
# Nothing is lost — the CSVs stay dirty and tomorrow tries again.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${HOLAFRESCA_BRANCH:-main}"
cd "$REPO"

say() { printf '==> %s\n' "$*"; }
unstage() { git reset --quiet -- exports/; }

# The same lock the deploy takes. The backup timer fires at 03:00 with up to
# fifteen minutes of jitter and the deploy timer runs every five, so these two
# land on each other sooner or later, and two git operations over one working
# tree is exactly what the lock is for.
exec 9>"$REPO/.git/holafresca-deploy.lock"
if ! flock -w 300 9; then
    say "a deploy is holding the lock; leaving tonight's export for tomorrow"
    exit 0
fi

# --update stages only files git already tracks under exports/, and only ones
# already modified. Never `-A`: an unattended committer must not sweep up
# whatever else happens to be dirty, nor adopt stray files something dropped in
# exports/.
git add --update -- exports/
if git diff --cached --quiet -- exports/; then
    exit 0  # the export produced nothing new tonight
fi

# Anything staged outside exports/ would ride along on this commit.
if ! git diff --cached --quiet -- ':(exclude)exports/'; then
    unstage
    say "something outside exports/ is staged; not committing over it"
    exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current_branch" != "$BRANCH" ]; then
    unstage
    say "on branch '$current_branch', not '$BRANCH'; leaving the export uncommitted"
    exit 0
fi

if ! git fetch --quiet origin "$BRANCH"; then
    unstage
    say "cannot reach origin; leaving tonight's export for tomorrow"
    exit 0
fi

# Only ever commit onto a tree that is level with origin. If origin has moved,
# fast-forwarding is the deploy timer's job and not this script's: doing it here
# would land prod on new code with a stale frontend build and an unmigrated
# database, because nothing here builds or migrates.
if [ "$(git rev-parse HEAD)" != "$(git rev-parse FETCH_HEAD)" ]; then
    unstage
    say "origin/$BRANCH has moved; letting the deploy timer catch up first"
    exit 0
fi

git commit --quiet -m "chore: refresh the mapping export" \
                   -m "Written by holafresca-backup.service."

if git push --quiet origin "$BRANCH"; then
    say "pushed $(git log --oneline -1)"
else
    # Take our own commit back rather than leave the box a commit ahead of
    # origin. --mixed puts the working tree back exactly as the export left it:
    # dirty, and ready for another go tomorrow.
    git reset --quiet --mixed HEAD~1
    say "push failed; commit rolled back, export left dirty for tomorrow"
    exit 1
fi
