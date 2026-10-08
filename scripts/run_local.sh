#!/bin/bash
# Run the bot + engine supervisor locally on macOS, using the CrossOver bottle created by
# scripts/setup_local_mac.sh. Reads secrets from .env in the repo root.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
[ -f .env ] || { echo "Missing .env (copy .env.example and fill it in)" >&2; exit 1; }
set -a; . ./.env; set +a

CX=/Applications/CrossOver.app/Contents/SharedSupport/CrossOver
BDIR="$HOME/Library/Application Support/CrossOver/Bottles/TradeGenius"
win_path() { echo "Z:$(echo "$1" | tr '/' '\\')"; }

export DATA_DIR="${DATA_DIR:-$REPO/data}"
export WINE_CMD="${WINE_CMD:-$CX/bin/wine --bottle TradeGenius}"
export MT5_BASE_DIR="${MT5_BASE_DIR:-$BDIR/drive_c/Program Files/MetaTrader 5}"
export ENGINE_SCRIPT="${ENGINE_SCRIPT:-$(win_path "$REPO/engine/engine.py")}"

if [ "${ENGINES_ENABLED:-true}" = "true" ] && [ ! -f "$MT5_BASE_DIR/terminal64.exe" ]; then
    echo "MT5 not installed in the CrossOver bottle — run scripts/setup_local_mac.sh first," >&2
    echo "or set ENGINES_ENABLED=false in .env to run the bot without trading." >&2
    exit 1
fi

exec .venv/bin/python -m tradegenius.main
