#!/usr/bin/env bash
# Flash a T-Dongle-S3 as CIED Bridge Lite sender or receiver.
# The device must be in download mode (plug in with BOOT held).
#
# Usage:
#   ./flash.sh --sender [OPTIONS]
#   ./flash.sh --receiver [OPTIONS]
#
# Options:
#   --sender            Build and flash as sender
#   --receiver          Build and flash as receiver
#   --ssid NAME         WiFi SSID (default: CIED-Bridge)
#   --password PASS     WiFi password (default: cied-bridge-lite)
#   --sender-id ID      Sender bay ID (default: bay1, sender only)
#   --port PORT         Serial port (default: auto-detect /dev/ttyACM*)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ROLE=""
SSID=""
PASSWORD=""
SENDER_ID=""
PORT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sender)   ROLE=sender; shift ;;
        --receiver) ROLE=receiver; shift ;;
        --ssid)     SSID="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --sender-id) SENDER_ID="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# \?//; p }' "$0"
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$ROLE" ]]; then
    echo "Error: specify --sender or --receiver" >&2
    exit 1
fi

# Auto-detect port
if [[ -z "$PORT" ]]; then
    PORT=$(ls /dev/ttyACM* 2>/dev/null | head -1) || true
    if [[ -z "$PORT" ]]; then
        echo "Error: no device found. Plug in with BOOT held." >&2
        exit 1
    fi
    echo "Using port: $PORT"
fi

# Source ESP-IDF
if [[ -f "$HOME/esp/esp-idf/export.sh" ]]; then
    source "$HOME/esp/esp-idf/export.sh" > /dev/null 2>&1
else
    echo "Error: ESP-IDF not found at ~/esp/esp-idf" >&2
    exit 1
fi

cd "$SCRIPT_DIR"

# Patch sdkconfig
if [[ "$ROLE" == "sender" ]]; then
    sed -i 's/^# CONFIG_BRIDGE_LITE_ROLE_SENDER is not set$/CONFIG_BRIDGE_LITE_ROLE_SENDER=y/' sdkconfig
    sed -i 's/^CONFIG_BRIDGE_LITE_ROLE_RECEIVER=y$/# CONFIG_BRIDGE_LITE_ROLE_RECEIVER is not set/' sdkconfig
    if [[ -n "$SENDER_ID" ]]; then
        sed -i "s/^CONFIG_BRIDGE_LITE_SENDER_ID=.*$/CONFIG_BRIDGE_LITE_SENDER_ID=\"$SENDER_ID\"/" sdkconfig
    fi
else
    sed -i 's/^CONFIG_BRIDGE_LITE_ROLE_SENDER=y$/# CONFIG_BRIDGE_LITE_ROLE_SENDER is not set/' sdkconfig
    sed -i 's/^# CONFIG_BRIDGE_LITE_ROLE_RECEIVER is not set$/CONFIG_BRIDGE_LITE_ROLE_RECEIVER=y/' sdkconfig
fi

if [[ -n "$SSID" ]]; then
    sed -i "s/^CONFIG_BRIDGE_LITE_WIFI_SSID=.*$/CONFIG_BRIDGE_LITE_WIFI_SSID=\"$SSID\"/" sdkconfig
fi
if [[ -n "$PASSWORD" ]]; then
    sed -i "s/^CONFIG_BRIDGE_LITE_WIFI_PASS=.*$/CONFIG_BRIDGE_LITE_WIFI_PASS=\"$PASSWORD\"/" sdkconfig
fi

echo "Building $ROLE..."
idf.py build 2>&1 | tail -3

echo "Flashing $ROLE to $PORT..."
python3 -m esptool --chip esp32s3 -p "$PORT" -b 460800 \
    --before default_reset --after hard_reset \
    write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m \
    0x0    build/bootloader/bootloader.bin \
    0x8000 build/partition_table/partition-table.bin \
    0x10000 build/cied-bridge-lite.bin

echo ""
echo "Done — $ROLE flashed successfully."
if [[ "$ROLE" == "sender" ]]; then
    id="${SENDER_ID:-bay1}"
    echo "  Role:      SENDER ($id)"
else
    echo "  Role:      RECEIVER"
fi
echo "  WiFi SSID: ${SSID:-$(grep CONFIG_BRIDGE_LITE_WIFI_SSID= sdkconfig | cut -d'"' -f2)}"
echo "  Unplug and replug without BOOT to start."
