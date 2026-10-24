#!/bin/bash
# Run on an edge node as root to switch it to the CIEDCentral WiFi AP.
# Usage: sudo bash update-to-ap.sh
set -euo pipefail

CONF=/etc/cied-edge/node.conf
CA_DEST=/etc/cied-edge/central-ca.crt
AP_SSID="CIEDCentral"
AP_PASS="CiedCentral#1"
AP_IP="10.42.0.1"

# ── Step 1: connect to AP first so we can reach the central server ──────────
echo "==> Connecting to WiFi AP: $AP_SSID"
if command -v nmcli &>/dev/null; then
    nmcli device wifi connect "$AP_SSID" password "$AP_PASS" ifname wlan0
    sleep 3
else
    echo "    nmcli not found — configure WiFi manually (SSID: $AP_SSID, pass: $AP_PASS)"
    echo "    Then re-run this script."
    exit 1
fi

# ── Step 2: fetch fresh CA cert from central (skip verify for bootstrap) ────
echo "==> Fetching CA cert from central server"
if command -v curl &>/dev/null; then
    curl -fsSk "https://${AP_IP}:8443/ca.crt" -o "$CA_DEST"
elif command -v wget &>/dev/null; then
    wget -qO "$CA_DEST" --no-check-certificate "https://${AP_IP}:8443/ca.crt"
else
    echo "ERROR: neither curl nor wget found — install one and re-run"
    exit 1
fi
chmod 644 "$CA_DEST"
echo "    Saved to $CA_DEST"

# ── Step 3: update node.conf ─────────────────────────────────────────────────
echo "==> Updating CENTRAL_URL in $CONF"
sed -i "s|^CENTRAL_URL=.*|CENTRAL_URL=https://${AP_IP}:8443/ingest|" "$CONF"
sed -i "s|^CENTRAL_EXPORT_URL=.*|CENTRAL_EXPORT_URL=https://${AP_IP}:8443/ingest-export|" "$CONF"

if ! grep -q "^CA_CERT=" "$CONF"; then
    echo "CA_CERT=$CA_DEST" >> "$CONF"
fi

# ── Step 4: restart daemons ──────────────────────────────────────────────────
echo "==> Restarting forwarder and storage-daemon"
systemctl restart forwarder.service storage-daemon.service 2>/dev/null || true

echo ""
echo "Done. Edge node will send to https://${AP_IP}:8443"
echo "Verify: journalctl -fu forwarder"
