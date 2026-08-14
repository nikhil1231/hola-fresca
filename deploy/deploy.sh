#!/usr/bin/env bash
# Deploy holafresca.uk from a dev machine: run deploy/update.sh on the server
# box over ssh.
#
#     deploy/deploy.sh            # deploy origin/main
#     deploy/deploy.sh --check    # dry run: say what would happen
#     deploy/deploy.sh --force    # rebuild + restart even if already at head
#
# Push first — this deploys what GitHub has, not what is on your disk.
#
# The box is reached by trying several addresses in order, because Tailscale is
# the usual route but not a dependency: if the tailnet is down and you are on
# the house LAN, the mDNS name or the LAN address still gets there. Override
# the whole list with HOLAFRESCA_HOST=... to force one.
set -euo pipefail

SSH_USER="${HOLAFRESCA_SSH_USER:-nikhil}"
REMOTE_DIR="${HOLAFRESCA_REMOTE_DIR:-Documents/Programming/AI/HolaFresca}"

if [ -n "${HOLAFRESCA_HOST:-}" ]; then
    CANDIDATES=("$HOLAFRESCA_HOST")
else
    CANDIDATES=(
        nikhil-hp-pavilion                       # Tailscale MagicDNS, from anywhere
        nikhil-hp-pavilion.tail1360ba.ts.net     # ...spelled out, if MagicDNS search is off
        nikhil-hp-pavilion.local                 # mDNS, same LAN, no tailnet needed
        192.168.0.219                            # LAN address, if mDNS is being mDNS
    )
fi

host=""
for candidate in "${CANDIDATES[@]}"; do
    printf 'trying %s... ' "$candidate"
    if ssh -o ConnectTimeout=4 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
           "$SSH_USER@$candidate" true 2>/dev/null; then
        echo "ok"
        host="$candidate"
        break
    fi
    echo "no"
done

if [ -z "$host" ]; then
    echo >&2
    echo "Could not reach the box at any of: ${CANDIDATES[*]}" >&2
    echo "Check \`tailscale status\`, or set HOLAFRESCA_HOST=<addr> to force one." >&2
    exit 1
fi

# -t so the deploy's progress and colours arrive as it goes, not in one lump at
# the end, and so Ctrl-C reaches the remote script.
exec ssh -t "$SSH_USER@$host" "$REMOTE_DIR/deploy/update.sh $*"
