#!/usr/bin/env bash
# Point a Cloudflare Tunnel at the local HolaFresca server.
#
#     deploy/setup-tunnel.sh hola.example.com
#
# Everything here is idempotent — re-running after a hostname change or a failed
# attempt is fine. It does not create the Cloudflare account, the zone, or the
# Access policy: those are dashboard steps, and deploy/README.md lists them.
#
# Prerequisite: `cloudflared tunnel login` has been run once, which opens a
# browser, asks which zone to authorise, and leaves a certificate in
# ~/.cloudflared/cert.pem.
set -euo pipefail

HOSTNAME=${1:-}
TUNNEL_NAME=${TUNNEL_NAME:-holafresca}
LOCAL_PORT=${LOCAL_PORT:-8100}
CLOUDFLARED=${CLOUDFLARED:-$HOME/.local/bin/cloudflared}
CONFIG_DIR=$HOME/.cloudflared

if [ -z "$HOSTNAME" ]; then
    echo "usage: $0 <hostname>   e.g. $0 hola.example.com" >&2
    exit 2
fi

if [ ! -x "$CLOUDFLARED" ]; then
    echo "cloudflared not found at $CLOUDFLARED" >&2
    exit 1
fi

if [ ! -f "$CONFIG_DIR/cert.pem" ]; then
    echo "Not logged in to Cloudflare yet. Run:" >&2
    echo "    $CLOUDFLARED tunnel login" >&2
    exit 1
fi

# Note the deleted_at test. A live tunnel does not omit the field or set it to
# null — it carries Go's zero time, "0001-01-01T00:00:00Z", which is a perfectly
# truthy string. Testing it for emptiness matches nothing at all, and the
# symptom is a config file with a blank tunnel id that cloudflared exits 255 on.
lookup_uuid() {
    "$CLOUDFLARED" tunnel list --output json | python3 -c "
import json, sys

name = sys.argv[1]
for t in json.load(sys.stdin):
    live = (t.get('deleted_at') or '').startswith('0001-01-01')
    if t.get('name') == name and live:
        print(t['id'])
        break
" "$TUNNEL_NAME"
}

# `tunnel create` fails if the name is taken, which on a re-run is the normal
# case rather than an error — so look first.
UUID=$(lookup_uuid)
if [ -z "$UUID" ]; then
    echo "Creating tunnel '$TUNNEL_NAME'..."
    "$CLOUDFLARED" tunnel create "$TUNNEL_NAME" >/dev/null
    UUID=$(lookup_uuid)
fi
if [ -z "$UUID" ]; then
    echo "Could not resolve a tunnel id for '$TUNNEL_NAME'" >&2
    exit 1
fi
echo "Tunnel $TUNNEL_NAME = $UUID"

cat >"$CONFIG_DIR/config.yml" <<EOF
# Written by deploy/setup-tunnel.sh — edit there, not here.
tunnel: $UUID
credentials-file: $CONFIG_DIR/$UUID.json

ingress:
  - hostname: $HOSTNAME
    service: http://localhost:$LOCAL_PORT
  # Anything else that reaches this tunnel is not for us.
  - service: http_status:404
EOF
echo "Wrote $CONFIG_DIR/config.yml -> http://localhost:$LOCAL_PORT"

# Creates the proxied CNAME for the hostname. Safe to repeat; it updates an
# existing record that already points at this tunnel.
"$CLOUDFLARED" tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" || true

UNIT_DIR=$HOME/.config/systemd/user
mkdir -p "$UNIT_DIR"
install -m 644 "$(dirname "$0")/cloudflared.service" "$UNIT_DIR/cloudflared.service"
systemctl --user daemon-reload
systemctl --user enable --now cloudflared.service
systemctl --user restart cloudflared.service

echo
echo "Tunnel service:"
systemctl --user --no-pager --lines=0 status cloudflared.service || true
echo
echo "Next: put the Access application in front of https://$HOSTNAME (see deploy/README.md),"
echo "then set HOLAFRESCA_ACCESS_* in .env and restart holafresca-dev.service."
