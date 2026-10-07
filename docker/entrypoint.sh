#!/bin/bash
set -euo pipefail

mkdir -p "$DATA_DIR"

echo "[entrypoint] starting virtual display"
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x16 -nolisten tcp >/dev/null 2>&1 &
sleep 2

if [ "${ENGINES_ENABLED:-true}" = "true" ]; then
    if /app/docker/setup_wine.sh; then
        export MT5_BASE_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
    else
        # Keep the bot reachable so admins get told; trading stays off until setup succeeds.
        echo "[entrypoint] Wine/MT5 setup FAILED - starting bot with engines disabled" >&2
        export ENGINES_ENABLED=false
        export SETUP_FAILED=1
    fi
fi

echo "[entrypoint] starting TradeGenius"
cd /app
exec python3 -m tradegenius.main
