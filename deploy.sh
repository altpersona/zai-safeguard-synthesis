#!/bin/bash
# deploy.sh — install the safeguard-verdict-synthesis proxy (or roll it back)
# and restart the user service.
#
# Deploying arms the verdict synthesis: CC auto mode stops making billed
# classifier fallback calls, and CC itself will never block an action in
# auto mode (local PreToolUse hooks become the only gate).
#
#   deploy.sh           install + restart
#   deploy.sh rollback  restore the pre-sg backup + restart
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)/zai-rewrite-proxy.py"
DST="${ZAI_PROXY_DST:-$HOME/.local/bin/zai-rewrite-proxy.py}"
BAK="$DST.pre-sg.bak"

mkdir -p "$(dirname "$DST")"

if [ "${1:-}" = "rollback" ]; then
    [ -f "$BAK" ] || { echo "no backup at $BAK"; exit 1; }
    cp "$BAK" "$DST"
else
    [ -f "$SRC" ] || { echo "missing $SRC (run from the repo root)"; exit 1; }
    if [ -f "$DST" ]; then
        cp -n "$DST" "$BAK"   # keep the first (pre-sg) backup only
    fi
    cp "$SRC" "$DST"
    chmod +x "$DST"
fi

systemctl --user restart zai-rewrite-proxy
sleep 1
systemctl --user is-active --quiet zai-rewrite-proxy
echo "active. watch verdict synthesis:"
echo "  journalctl --user -u zai-rewrite-proxy -f | grep zai-sg"
