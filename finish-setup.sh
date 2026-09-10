#!/bin/zsh
# Run this ONCE from an iTerm2 window on the Mac mini (via Screen Sharing).
# iTerm2 has macOS Bluetooth permission; this grants it to the solar-control venv too.
set -e
cd /Users/server/solar-control
PY=./.venv/bin/python

echo '==> 1/4  BLE scan (approve the Bluetooth prompt if macOS shows one)'
SCAN_OUT=$($PY -m solar_control scan)
echo "$SCAN_OUT"

ADDR=$(echo "$SCAN_OUT" | awk '/BT-TH|BT-2/ {print $1; exit}')
if [ -z "$ADDR" ]; then
  echo 'No BT-TH / BT-2 module found. Is the BT-1 powered and in range, and not connected in the Renogy phone app?'
  exit 1
fi
echo "==> Found module: $ADDR"

echo '==> 2/4  Updating .env RENOGY_MAC_ADDRESS'
/usr/bin/sed -i '' -E "s|^RENOGY_MAC_ADDRESS=.*|RENOGY_MAC_ADDRESS=${ADDR}|" .env
grep RENOGY_MAC_ADDRESS .env

echo '==> 3/4  Loading LaunchAgent'
launchctl bootout gui/$(id -u)/com.solar-control.api 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.solar-control.api.plist
sleep 8

echo '==> 4/4  Checking the API'
echo '--- /health ---'; curl -s --max-time 10 http://127.0.0.1:8000/health; echo
echo '--- /status ---'; curl -s --max-time 25 http://127.0.0.1:8000/status; echo
echo
echo 'If /status shows solar telemetry, it works. Paste this whole output back to Claude.'
echo 'Recent daemon log:'; tail -n 20 ~/Library/Logs/solar-control/solar-control.err.log
