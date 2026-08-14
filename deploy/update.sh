#!/usr/bin/env bash
# Deploy the live HolaFresca (holafresca.uk, :8100) to the head of origin/main.
#
#     deploy/update.sh            # deploy if origin/main moved
#     deploy/update.sh --check    # say what a deploy would do, change nothing
#     deploy/update.sh --force    # rebuild + restart even if already at head
#
# Runs *on the server box*. From a dev machine use deploy/deploy.sh, which is
# just this script over ssh.
#
# Unlike sync-integration.sh, this never does `git reset --hard`: the live
# service serves this working tree, so a stray edit here is someone's work, not
# drift to be flattened. A dirty tree aborts the deploy instead.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${HOLAFRESCA_BRANCH:-main}"
SERVICE="${HOLAFRESCA_SERVICE:-holafresca-dev.service}"
HEALTH_URL="${HOLAFRESCA_HEALTH_URL:-http://localhost:8100/api/health}"
cd "$REPO"

CHECK=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --check|-n) CHECK=1 ;;
        --force|-f) FORCE=1 ;;
        *) echo "usage: $0 [--check] [--force]" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

# `ssh box deploy/update.sh` is a non-interactive shell, so ~/.bashrc returns
# early and nvm is never loaded. Today that is survivable — there is a system
# npm at /usr/bin/npm on the same node major — but the node this box develops
# against is nvm's, and "deploys work until someone removes the distro node" is
# not a property worth having. Fall back to nvm when PATH has nothing.
ensure_npm() {
    command -v npm >/dev/null 2>&1 && return 0
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        set +u  # nvm.sh trips over `set -u`
        # shellcheck disable=SC1091
        . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true
        set -u
    fi
    command -v npm >/dev/null 2>&1
}

# The per-account Ocado keys are templated on OCADO_ACCOUNTS, so .env.example's
# OCADO_MAIN_EMAIL and this box's OCADO_NIKHIL_EMAIL are one key wearing two
# names. Fold the slug out before comparing the two files, or the check reports
# seven missing keys that are all present.
slug_filter() {
    local file="$1" slug
    local -a expr=(-e 's/^$//')
    while read -r slug; do
        [ -n "$slug" ] && expr+=(-e "s/^OCADO_${slug}_/OCADO_@_/")
    done < <(sed -n 's/^OCADO_ACCOUNTS=//p' "$file" | tr ',' '\n' | tr -d ' ' \
             | tr '[:lower:]' '[:upper:]')
    sed "${expr[@]}"
}

env_keys() { sed -n 's/^\([A-Z_][A-Z0-9_]*\)=.*/\1/p' "$1" | slug_filter "$1" | sort -u; }

# A config key this deploy *introduces* is a silent 500 at request time, and
# worth a shout. A key that has been absent for months is either optional or
# already known about — warning about those on every deploy is how a warning
# stops being read, so only the diff counts.
report_new_config_keys() {
    [ "$config" -eq 1 ] && [ -f .env ] && [ -f .env.example ] || return 0
    local added missing
    added="$(git diff "$before" "$target" -- .env.example \
             | sed -n 's/^+\([A-Z_][A-Z0-9_]*\)=.*/\1/p' | slug_filter .env.example | sort -u)"
    [ -n "$added" ] || return 0
    missing="$(comm -23 <(printf '%s\n' "$added") <(env_keys .env))"
    [ -n "$missing" ] && warn "this deploy added config that .env does not set: $(printf '%s' "$missing" | tr '\n' ' ')"
    return 0
}

# --- refuse to deploy over local work -------------------------------------
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no >&2
    die "working tree has uncommitted changes — commit, stash or revert them first."
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$current_branch" = "$BRANCH" ] || die "on branch '$current_branch', expected '$BRANCH'."

# --- what would change? ----------------------------------------------------
before="$(git rev-parse HEAD)"
git fetch --quiet origin "$BRANCH"
target="$(git rev-parse FETCH_HEAD)"

if [ "$before" = "$target" ] && [ "$FORCE" -eq 0 ]; then
    say "already at $(git log --oneline -1 HEAD)"
    exit 0
fi

changed() { ! git diff --quiet "$before" "$target" -- "$@"; }

if [ "$before" != "$target" ]; then
    say "$(git rev-list --count "$before".."$target") new commit(s):"
    git log --oneline --reverse "$before".."$target" | sed 's/^/    /'
fi

deps=0; lockfile=0; web=0; migrations=0; config=0
[ "$before" != "$target" ] && {
    changed requirements.txt          && deps=1
    changed frontend/package-lock.json && lockfile=1
    changed frontend                  && web=1
    changed alembic/versions          && migrations=1
    changed .env.example              && config=1
}
[ "$FORCE" -eq 1 ] && { deps=1; web=1; }

if [ "$CHECK" -eq 1 ]; then
    say "would deploy ${before:0:7} -> ${target:0:7}"
    [ "$deps" -eq 1 ]       && echo "    - reinstall python deps"
    [ "$lockfile" -eq 1 ]   && echo "    - npm ci"
    [ "$web" -eq 1 ]        && echo "    - rebuild frontend"
    [ "$migrations" -eq 1 ] && echo "    - snapshot db, then alembic upgrade head"
    echo "    - alembic upgrade head (no-op if already current)"
    echo "    - restart $SERVICE and wait for $HEALTH_URL"
    exit 0
fi

# --- deploy ----------------------------------------------------------------
# The service runs with UVICORN_RELOAD=true watching app/, so it will reload
# itself part-way through the pull. That is harmless — the explicit restart at
# the end is what settles it on the final tree.
say "pulling $BRANCH"
git merge --ff-only --quiet "$target"

if [ "$deps" -eq 1 ]; then
    say "requirements.txt changed — installing python deps"
    ./.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
fi

if [ "$web" -eq 1 ]; then
    ensure_npm || die "npm not found (looked on PATH and in ${NVM_DIR:-$HOME/.nvm})"
    if [ "$lockfile" -eq 1 ]; then
        say "package-lock.json changed — npm ci"
        npm --prefix frontend ci --silent
    fi
    say "building frontend"
    npm --prefix frontend run build --silent
fi

# Nothing runs migrations at boot, so they have to happen here. Snapshot first
# when the schema is actually about to move: the nightly backup may be up to a
# day old, and `alembic downgrade` is not a restore.
if [ "$migrations" -eq 1 ]; then
    say "new migrations — snapshotting database first"
    ./.venv/bin/python -m app.backup snapshot
fi
say "alembic upgrade head"
./.venv/bin/alembic upgrade head

say "restarting $SERVICE"
systemctl --user restart "$SERVICE"

# --- health ----------------------------------------------------------------
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
        say "healthy — now at $(git log --oneline -1 HEAD)"
        report_new_config_keys
        exit 0
    fi
    sleep 1
done

printf '\n'
systemctl --user --no-pager --lines=25 status "$SERVICE" >&2 || true
die "did not come healthy within 30s. Previous good revision was ${before:0:7}
   (roll back with: git -C $REPO reset --hard $before && systemctl --user restart $SERVICE
    — but check the migrations above first; a schema change does not roll back with the code.)"
