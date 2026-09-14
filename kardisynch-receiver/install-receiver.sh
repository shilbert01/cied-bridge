#!/usr/bin/env bash
#
# Provision a freshly-imaged Raspberry Pi OS Lite (Pi Zero W) as a KardiSynch
# receiver. Run this ON the Pi, as root, from a copy of the repo:
#
#     sudo bash kardisynch-receiver/install-receiver.sh [hid|mass-storage]
#
# It: enables the dwc2 USB gadget, installs the receiver payload + systemd units,
# writes /etc/kardisynch-receiver/receiver.conf, and enables the services.
#
# Assumes WiFi + SSH are already set (Raspberry Pi Imager). To (re)configure the
# relayap WiFi from here too, pass CONFIGURE_WIFI=1 (see below) — but note that
# doing so over an SSH-over-WiFi session can drop your connection.
#
# Idempotent: safe to re-run (updates payload, re-enables services).
#
# Config (env-overridable):
#     NODE_ID         hid-leipzig-01      this presenter's hub node id
#     HUB_URL         http://10.99.0.1:8090
#     CONFIGURE_WIFI  0                   set 1 to write the relayap connection here
#     WIFI_SSID       relayap
#     WIFI_PSK        RelayPrint#1
#     WIFI_COUNTRY    DE
set -euo pipefail

MODE="${1:-hid}"

NODE_ID="${NODE_ID:-hid-leipzig-01}"
HUB_URL="${HUB_URL:-http://10.99.0.1:8090}"
CONFIGURE_WIFI="${CONFIGURE_WIFI:-0}"
WIFI_SSID="${WIFI_SSID:-relayap}"
WIFI_PSK="${WIFI_PSK:-RelayPrint#1}"
WIFI_COUNTRY="${WIFI_COUNTRY:-DE}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # kardisynch-receiver/

log()  { echo -e "\033[1;36m==>\033[0m $*"; }
warn() { echo -e "\033[1;33mwarn:\033[0m $*" >&2; }
die()  { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

# ── preflight ───────────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash $0 $MODE"
command -v systemctl >/dev/null || die "no systemd — is this Raspberry Pi OS?"
command -v python3   >/dev/null || die "python3 missing (base image should have it)"

# Locate the shared hub client (source of truth lives in import-relay).
HUB_CLIENT=""
for c in "$SRC_DIR/../import-relay/daemon/hub_client.py" "$SRC_DIR/hub_client.py"; do
    [ -f "$c" ] && { HUB_CLIENT="$c"; break; }
done
[ -n "$HUB_CLIENT" ] || die "hub_client.py not found. Copy the import-relay dir too, e.g.:
    rsync -a pacemaker-bridge/{kardisynch-receiver,import-relay} pi@<this-host>:~/"

# Where does Raspberry Pi OS keep the boot config? (Bookworm moved it.)
if [ -d /boot/firmware ]; then BOOT=/boot/firmware; else BOOT=/boot; fi

case "$MODE" in
    hid) ;;
    mass-storage)
        die "mass-storage mode reuses import-relay — see kardisynch-receiver/mass-storage/README.md" ;;
    *) die "usage: $0 [hid|mass-storage]" ;;
esac

# ── 1. enable the dwc2 USB gadget ───────────────────────────────────────────
log "Enabling dwc2 OTG + libcomposite…"
bash "$SRC_DIR/common/setup-otg.sh"

# ── 2. install the HID payload ──────────────────────────────────────────────
log "Installing payload to /usr/local/sbin…"
install -Dm755 "$SRC_DIR/hid/gadget-hid.sh"  /usr/local/sbin/gadget-hid.sh
install -Dm755 "$SRC_DIR/hid/hid_sender.py"  /usr/local/sbin/hid_sender.py
install -Dm755 "$SRC_DIR/common/set-mode.sh" /usr/local/sbin/set-mode.sh
install -Dm644 "$HUB_CLIENT"                 /usr/local/sbin/hub_client.py

# ── 3. config (never clobber an edited one) ─────────────────────────────────
CONF=/etc/kardisynch-receiver/receiver.conf
if [ -f "$CONF" ]; then
    warn "$CONF exists — leaving it as-is (edit manually if NODE_ID/HUB_URL changed)"
else
    log "Writing $CONF (node=$NODE_ID hub=$HUB_URL)…"
    install -Dm600 "$SRC_DIR/common/receiver.conf.example" "$CONF"
    sed -i "s|^NODE_ID=.*|NODE_ID=${NODE_ID}|; s|^IMPORT_HUB_URL=.*|IMPORT_HUB_URL=${HUB_URL}|" "$CONF"
fi
mkdir -p /var/lib/kardisynch-receiver/spool

# ── 4. systemd units ────────────────────────────────────────────────────────
log "Installing systemd units…"
install -Dm644 "$SRC_DIR/hid/kardisynch-gadget-hid.service" /etc/systemd/system/kardisynch-gadget-hid.service
install -Dm644 "$SRC_DIR/hid/kardisynch-hid-sender.service" /etc/systemd/system/kardisynch-hid-sender.service
systemctl daemon-reload

# ── 5. optional WiFi (opt-in; can drop an SSH-over-WiFi session) ─────────────
if [ "$CONFIGURE_WIFI" = "1" ]; then
    log "Configuring WiFi '$WIFI_SSID' (country $WIFI_COUNTRY)…"
    raspi-config nonint do_wifi_country "$WIFI_COUNTRY" 2>/dev/null || true
    rfkill unblock wifi 2>/dev/null || true
    if command -v nmcli >/dev/null; then
        nmcli con delete "$WIFI_SSID" 2>/dev/null || true
        nmcli con add type wifi ifname wlan0 con-name "$WIFI_SSID" ssid "$WIFI_SSID" 2>/dev/null || true
        nmcli con modify "$WIFI_SSID" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PSK" \
            802-11-wireless.hidden yes connection.autoconnect yes
        nmcli con up "$WIFI_SSID" || warn "could not bring up $WIFI_SSID now (will retry on boot)"
    else
        warn "nmcli not found — configure WiFi via raspi-config or wpa_supplicant manually"
    fi
fi

# ── 6. enable services (start now only if the gadget bus is already live) ────
UDC="$(ls /sys/class/udc 2>/dev/null | head -1 || true)"
if [ -n "$UDC" ]; then
    log "UDC present ($UDC) — enabling + starting HID mode now…"
    systemctl enable kardisynch-gadget-hid kardisynch-hid-sender
    /usr/local/sbin/set-mode.sh hid
    NEED_REBOOT=0
else
    log "No UDC yet (dwc2 needs a reboot) — enabling services for next boot…"
    systemctl enable kardisynch-gadget-hid kardisynch-hid-sender
    NEED_REBOOT=1
fi

# ── done ────────────────────────────────────────────────────────────────────
echo
log "Receiver installed (mode=hid, node=$NODE_ID, hub=$HUB_URL)."
cat <<DONE

  Config:   $CONF
  Services: kardisynch-gadget-hid, kardisynch-hid-sender  (enabled)

  Check after boot:
    ls /dev/hidg0
    systemctl status kardisynch-hid-sender --no-pager
    journalctl -u kardisynch-hid-sender -f

  Prerequisite for round-trip: the import hub must run on the Pi3
    (import-relay/pi3-server/import-server.py -> enable import-server).
DONE

if [ "$NEED_REBOOT" = "1" ]; then
    echo
    warn "Reboot required for the USB gadget to come up:  sudo reboot"
fi
