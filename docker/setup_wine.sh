#!/bin/bash
# One-time setup of the Wine prefix on the persistent disk:
# Windows Python 3.11 + MetaTrader5/pandas packages + MetaTrader 5 terminal.
# Idempotent: skipped when the marker file exists. First run takes ~5-10 minutes.
set -euo pipefail

MARKER="$WINEPREFIX/.tradegenius_setup_v1"
PY_WIN='C:\Python311\python.exe'
MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"

if [ -f "$MARKER" ] && [ -f "$MT5_DIR/terminal64.exe" ]; then
    echo "[setup] wine prefix ready"
    # Keep Python packages in sync with the image on every deploy (fast when up to date).
    timeout 600 wine "$PY_WIN" -m pip install --quiet --disable-pip-version-check \
        -r /app/requirements-wine.txt || echo "[setup] warning: pip sync failed"
    exit 0
fi

echo "[setup] initializing wine prefix at $WINEPREFIX"
mkdir -p "$WINEPREFIX"
wineboot --init
wineserver -w

PY_DIR="$WINEPREFIX/drive_c/Python311"
if [ ! -f "$PY_DIR/Scripts/pip.exe" ]; then
    echo "[setup] installing Windows Python 3.11 (embeddable)"
    rm -rf "$PY_DIR"
    mkdir -p "$PY_DIR"
    unzip -q /opt/installers/python-3.11.9-embed-amd64.zip -d "$PY_DIR"
    # Enable site-packages, which the embeddable distribution turns off by default.
    sed -i 's/^#\s*import site/import site/' "$PY_DIR/python311._pth"
    echo 'Lib\site-packages' >> "$PY_DIR/python311._pth"
    timeout 900 wine "$PY_WIN" 'Z:\opt\installers\get-pip.py' --no-warn-script-location
    wineserver -w
fi

echo "[setup] installing Python packages inside Wine"
timeout 900 wine "$PY_WIN" -m pip install --no-cache-dir --disable-pip-version-check \
    -r /app/requirements-wine.txt

if [ ! -f "$MT5_DIR/terminal64.exe" ]; then
    # MT5 requires a Windows 10 environment.
    wine reg add 'HKEY_CURRENT_USER\Software\Wine' /v Version /t REG_SZ /d win10 /f >/dev/null 2>&1 || true
    for attempt in 1 2 3; do
        echo "[setup] installing MetaTrader 5, attempt $attempt (downloads from MetaQuotes)"
        wine /opt/installers/mt5setup.exe /auto >"$WINEPREFIX/mt5setup-$attempt.log" 2>&1 &
        for i in $(seq 1 180); do
            [ -f "$MT5_DIR/terminal64.exe" ] && break
            sleep 5
        done
        if [ -f "$MT5_DIR/terminal64.exe" ]; then
            # The installer keeps downloading components after terminal64.exe appears.
            sleep 60
            wineserver -k || true
            break
        fi
        echo "[setup] attempt $attempt failed; installer log tail:"
        tail -5 "$WINEPREFIX/mt5setup-$attempt.log" || true
        wineserver -k || true
        sleep 10
    done
fi

if [ ! -f "$MT5_DIR/terminal64.exe" ]; then
    echo "[setup] ERROR: MetaTrader 5 installation failed" >&2
    exit 1
fi

touch "$MARKER"
echo "[setup] done"
