#!/bin/bash
# Pi3 export server + relay AP setup — run as root on fresh Raspberry Pi OS.
#
# Creates a hidden WiFi AP (relayap) on wlan0 for the Pi Zero export node to connect to.
# eth0 stays on the main LAN for management.
# Samba serves /var/lib/cied-export/import as \\PI3\import for CIED Studio.
#
# Subnet:  10.99.0.0/24
#   Pi3 wlan0:   10.99.0.1
#   Pi Zero:     10.99.0.2  (static, set in export.conf / S40wifi)
#
# AP passphrase is RelayPrint#1 — change in /etc/hostapd/hostapd.conf after setup.

set -euo pipefail

# ── Clinic feature manifest ──────────────────────────────────────────────────
# Source the per-clinic manifest + helpers; anything unset defaults to "on" so a
# box without a manifest behaves like the pre-feature-flag script.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CLINIC_CONF="${CLINIC_CONF:-/etc/cied-bridge/clinic.conf}"
[ -f "$SELF_DIR/../../tools/feature-lib.sh" ] && . "$SELF_DIR/../../tools/feature-lib.sh"
[ -f "$CLINIC_CONF" ] && . "$CLINIC_CONF"
: "${FEATURE_RELAY_AP:=on}"
: "${FEATURE_SAMBA_SHARE:=on}"
: "${FEATURE_IMPORT_HUB:=on}"
# Minimal fallbacks if feature-lib.sh wasn't found alongside the script.
type feature_enabled >/dev/null 2>&1 || feature_enabled() { eval "_v=\${$1:-}"; case "$(printf %s "$_v"|tr A-Z a-z)" in on|yes|1|true) return 0;; *) return 1;; esac; }
type apply_feature_service >/dev/null 2>&1 || apply_feature_service() { f=$1; shift; if feature_enabled "$f"; then systemctl enable $* >/dev/null 2>&1||true; systemctl restart $* >/dev/null 2>&1||true; else systemctl disable --now $* >/dev/null 2>&1||true; fi; }

AP_IFACE=wlan0
AP_IP=10.99.0.1
AP_DHCP_START=10.99.0.10
AP_DHCP_END=10.99.0.50
: "${AP_SSID:=relayap}"
: "${AP_PASS:=RelayPrint#1}"
: "${AP_CHANNEL:=6}"
: "${AP_HIDDEN:=off}"
AP_HIDE=$([ "$(printf %s "$AP_HIDDEN"|tr A-Z a-z)" = on ] && echo 2 || echo 0)

IMPORT_DIR=/var/lib/cied-export/import

apt-get update -qq
apt-get install -y hostapd dnsmasq samba chrony

# ── Stop services while reconfiguring ────────────────────────────────────────
systemctl stop hostapd dnsmasq smbd nmbd 2>/dev/null || true
rfkill unblock wlan 2>/dev/null || true

# ── Prevent NetworkManager from touching wlan0 ───────────────────────────────
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-relay-ap.conf << 'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
systemctl restart NetworkManager 2>/dev/null || true

# ── Static IP on wlan0 ───────────────────────────────────────────────────────
cat > /etc/systemd/system/relay-ap-ip.service << EOF
[Unit]
Description=Static IP for relay AP (${AP_IFACE})
After=hostapd.service
Requires=hostapd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip addr add ${AP_IP}/24 dev ${AP_IFACE}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable relay-ap-ip.service

ip addr add "${AP_IP}/24" dev "$AP_IFACE" 2>/dev/null || true
ip link set "$AP_IFACE" up 2>/dev/null || true

# ── hostapd — hidden AP ──────────────────────────────────────────────────────
cat > /etc/hostapd/hostapd.conf << EOF
interface=${AP_IFACE}
driver=nl80211
ssid=${AP_SSID}
ignore_broadcast_ssid=${AP_HIDE}
hw_mode=g
channel=${AP_CHANNEL}
country_code=DE
ieee80211d=1
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=${AP_PASS}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
chmod 600 /etc/hostapd/hostapd.conf

sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

# Unblock WiFi before hostapd starts (NetworkManager can soft-block it on first boot)
mkdir -p /etc/systemd/system/hostapd.service.d
cat > /etc/systemd/system/hostapd.service.d/rfkill-unblock.conf << 'EOF'
[Service]
ExecStartPre=/usr/sbin/rfkill unblock wlan
EOF

# ── dnsmasq — DHCP on relay subnet ───────────────────────────────────────────
[ -f /etc/dnsmasq.conf ] && mv /etc/dnsmasq.conf /etc/dnsmasq.conf.orig

cat > /etc/dnsmasq.d/relay-ap.conf << EOF
interface=${AP_IFACE}
bind-interfaces
dhcp-range=${AP_DHCP_START},${AP_DHCP_END},12h
EOF

# ── chrony — NTP server for Pi Zero export node ──────────────────────────────
if ! grep -q "local stratum 10" /etc/chrony/chrony.conf; then
    cat >> /etc/chrony/chrony.conf << 'EOF'

# Serve local clock to relay subnet even without internet NTP access
local stratum 10
allow 10.99.0.0/24
EOF
fi

# ── Import directory + Samba share ───────────────────────────────────────────
mkdir -p "$IMPORT_DIR"
chmod 777 "$IMPORT_DIR"

# Write smb.conf preserving any existing global section
cat > /etc/samba/smb.conf << 'EOF'
[global]
   workgroup = WORKGROUP
   server string = CIED Bridge Export
   log level = 1
   map to guest = bad user

[import]
   path = /var/lib/cied-export/import
   browseable = yes
   read only = no
   guest ok = yes
   create mask = 0666
   directory mask = 0777
EOF

# ── Export ingest web server ─────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SCRIPT_DIR/export-config-server.py" /usr/local/sbin/
install -m 644 "$SCRIPT_DIR/export-config.service"   /etc/systemd/system/

# ── Import hub (queue between senders and presenter nodes) ────────────────────
# Installed only if the import-relay tree is present AND the feature is on.
# Same subnet/AP as the export relay — presenters pull, senders POST to :8090.
IMPORT_HUB_INSTALLED=0
IMPORT_HUB_SRC="$SCRIPT_DIR/../../import-relay/pi3-server"
if feature_enabled FEATURE_IMPORT_HUB && [ -f "$IMPORT_HUB_SRC/import-server.py" ]; then
    install -m 755 "$IMPORT_HUB_SRC/import-server.py"      /usr/local/sbin/
    install -m 644 "$IMPORT_HUB_SRC/import-server.service" /etc/systemd/system/
    mkdir -p /var/lib/cied-import-hub
    IMPORT_HUB_INSTALLED=1
elif feature_enabled FEATURE_IMPORT_HUB; then
    echo "NOTE: FEATURE_IMPORT_HUB=on but import-relay/pi3-server not found — skipping install."
fi

systemctl daemon-reload

# ── Enable and start (per feature manifest) ──────────────────────────────────
# FEATURE_RELAY_AP is the substrate every relay feature needs; keep hostapd's
# `start` (not restart) — brcmfmac is fussy about being restarted.
if feature_enabled FEATURE_RELAY_AP; then
    systemctl unmask hostapd
    systemctl enable  hostapd dnsmasq chrony >/dev/null 2>&1 || true
    systemctl restart dnsmasq chrony 2>/dev/null || true
    systemctl start   hostapd dnsmasq
    echo "   [FEATURE_RELAY_AP=on]  up:   hostapd dnsmasq chrony"
else
    systemctl disable --now hostapd dnsmasq 2>/dev/null || true
    echo "   [FEATURE_RELAY_AP=off] down: hostapd dnsmasq"
fi
apply_feature_service FEATURE_SAMBA_SHARE "smbd nmbd export-config"
if [ "$IMPORT_HUB_INSTALLED" = "1" ]; then
    apply_feature_service FEATURE_IMPORT_HUB "import-server"
else
    systemctl disable --now import-server 2>/dev/null || true
fi

echo ""
echo "Clinic:     ${CLINIC_NAME:-unset}"
echo "Relay AP:   ssid=${AP_SSID} $([ "$AP_HIDE" = 2 ] && echo '(hidden)' || echo '(broadcast)')  passphrase=${AP_PASS}  [FEATURE_RELAY_AP=$FEATURE_RELAY_AP]"
echo "Pi3 wlan0:  ${AP_IP}"
if feature_enabled FEATURE_SAMBA_SHARE; then
    echo "SAMBA:      \\\\\\\\$(hostname)\\import  →  ${IMPORT_DIR}"
fi
if [ "$IMPORT_HUB_INSTALLED" = "1" ]; then
    echo "Import hub: http://${AP_IP}:8090/  (senders POST /submit; presenters pull)"
fi
echo ""
echo "Pi Zero should connect to relayap and claim 10.99.0.2."
echo "KardiSynch importDir: map \\\\\\\\PI3\\import as a network drive."
echo ""
echo "Web UI: http://${AP_IP}/"
