#!/usr/bin/env bash
# Flash the ESP32-S3 printer-relay firmware and open the debug CDC monitor.
#
# The firmware lives in the ThingPulse fork working tree (see SPIKE-PLAN.md):
FORK="${FORK:-$HOME/Sources/esp32-pendrive-s3-wifi-dongle}"
IDF="${IDF:-$HOME/esp/esp-idf}"

set -euo pipefail

# shellcheck disable=SC1091
source "$IDF/export.sh" >/dev/null 2>&1

cd "$FORK"

# Auto-detect the flashing port (USB-serial/JTAG on the DevKitC UART port).
PORT="${PORT:-}"
if [ -z "$PORT" ]; then
  for p in /dev/ttyACM0 /dev/ttyUSB0 /dev/ttyACM1 /dev/ttyUSB1; do
    [ -e "$p" ] && PORT="$p" && break
  done
fi
if [ -z "$PORT" ]; then
  echo "No serial port found. Plug in the S3 (UART port), or set PORT=/dev/ttyXXX." >&2
  echo "Tip: hold BOOT while plugging in if it won't enter download mode." >&2
  exit 1
fi

echo ">> Flashing $FORK -> $PORT"
idf.py -p "$PORT" flash
echo ">> Flashed. Opening monitor (Ctrl-] to exit). Debug log also on the printer's"
echo "   CDC interface once enumerated as /dev/ttyACM* on the HOST side."
idf.py -p "$PORT" monitor
