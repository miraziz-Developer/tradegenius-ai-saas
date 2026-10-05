#!/bin/bash
set -e

echo "[*] Initializing Virtual Display (Xvfb)..."
Xvfb :99 -screen 0 1024x768x16 &
sleep 2

MT5_DIR="/root/.wine/drive_c/Program Files/MetaTrader 5"
CONFIG_FILE="$MT5_DIR/account.ini"

echo "[*] Generating MT5 account configuration..."
mkdir -p "$MT5_DIR"

cat <<EOF > "$CONFIG_FILE"
[Common]
Login=${MT5_LOGIN}
Password=${MT5_PASSWORD}
Server=${MT5_SERVER}
EnableNews=0
EOF

echo "[*] Launching MetaTrader 5 terminal in background via Wine..."
wine "$MT5_DIR/terminal64.exe" /config:account.ini &

echo "[*] Waiting for MT5 to establish broker connection..."
sleep 10

echo "[*] Starting Universal Python Strategy Engine..."
exec wine "C:\\Python311\\python.exe" /app/strategy_engine.py
