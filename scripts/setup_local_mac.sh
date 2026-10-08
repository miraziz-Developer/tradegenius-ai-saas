#!/bin/bash
# One-time local setup on macOS with CrossOver: creates a "TradeGenius" bottle with
# Windows Python 3.11 + MetaTrader5/pandas + the MetaTrader 5 terminal. Idempotent.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CX=/Applications/CrossOver.app/Contents/SharedSupport/CrossOver
BOTTLE=TradeGenius
BDIR="$HOME/Library/Application Support/CrossOver/Bottles/$BOTTLE"
CACHE="$REPO/data/installers"
WINE=("$CX/bin/wine" --bottle "$BOTTLE")
PY_DIR="$BDIR/drive_c/Python311"
MT5_DIR="$BDIR/drive_c/Program Files/MetaTrader 5"

[ -x "$CX/bin/wine" ] || { echo "CrossOver not found in /Applications" >&2; exit 1; }

if [ ! -d "$BDIR" ]; then
    echo "[setup] creating CrossOver bottle $BOTTLE"
    "$CX/bin/cxbottle" --bottle "$BOTTLE" --create --template win10_64 --description "TradeGenius MT5 engine"
fi

mkdir -p "$CACHE"
fetch() { [ -s "$CACHE/$1" ] || curl -fsSL -o "$CACHE/$1" "$2"; }
echo "[setup] downloading installers"
fetch python-3.11.9-embed-amd64.zip https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
fetch get-pip.py https://bootstrap.pypa.io/get-pip.py
fetch mt5setup.exe https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe

if [ ! -f "$PY_DIR/Scripts/pip.exe" ]; then
    echo "[setup] installing Windows Python 3.11 (embeddable) into the bottle"
    rm -rf "$PY_DIR"
    mkdir -p "$PY_DIR"
    unzip -q "$CACHE/python-3.11.9-embed-amd64.zip" -d "$PY_DIR"
    sed -i '' 's/^#[[:space:]]*import site/import site/' "$PY_DIR/python311._pth"
    echo 'Lib\site-packages' >> "$PY_DIR/python311._pth"
    "${WINE[@]}" 'C:\Python311\python.exe' "$CACHE/get-pip.py" --no-warn-script-location
fi

echo "[setup] installing Python packages inside the bottle"
"${WINE[@]}" 'C:\Python311\python.exe' -m pip install --quiet --disable-pip-version-check \
    --no-warn-script-location -r "$REPO/requirements-wine.txt"
"${WINE[@]}" 'C:\Python311\python.exe' -c "import MetaTrader5, pandas; print('[setup] MetaTrader5', MetaTrader5.__version__, '| pandas', pandas.__version__)"

if [ ! -f "$MT5_DIR/terminal64.exe" ]; then
    echo "[setup] installing MetaTrader 5 (an installer window may appear — let it finish)"
    "${WINE[@]}" "$CACHE/mt5setup.exe" /auto &
    for i in $(seq 1 180); do
        [ -f "$MT5_DIR/terminal64.exe" ] && break
        sleep 5
    done
    [ -f "$MT5_DIR/terminal64.exe" ] || { echo "[setup] ERROR: MetaTrader 5 did not install" >&2; exit 1; }
    sleep 60   # installer keeps downloading components after terminal64.exe appears
    pkill -f "mt5setup.exe" || true
    pkill -f "MetaTrader 5.terminal64.exe" || true
fi

echo "[setup] done. MT5: $MT5_DIR"
