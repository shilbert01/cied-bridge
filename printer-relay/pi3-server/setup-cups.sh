#!/bin/bash
# Relay hub setup — run as root on fresh Raspberry Pi OS or Debian/Ubuntu server.
#
# Combined hub: printer relay + data export. Roles are composable via
# clinic.conf feature flags (all default to on).
#
# Creates a WiFi AP (relayap) on the relay interface for relay nodes (ESP32, Pi Zero).
# eth0 stays on the main LAN for management.
# If an RT5370 USB adapter is present, udev pins it to wlan_ap for stable naming.
#
# Printer role: CUPS raw queue + LPR (port 515) + USB autoconf + watchdog + web UI.
# Data role:    Samba share (\\<host>\import) + export ingest server + import hub.
#
# Subnet:  10.99.0.0/24
#   Hub AP iface: 10.99.0.1
#   Relay nodes:  10.99.0.2+ (static or DHCP)
#
# AP passphrase is RelayPrint#1 — change in /etc/hostapd/hostapd.conf after setup.

set -euo pipefail

# ── Platform detection ──────────────────────────────────────────────────────
# Pi-specific sections (dwc2 USB driver, /boot/firmware config, raspi-config)
# are skipped on non-Pi hardware where they don't apply.
IS_RPI=false
if [ -f /proc/device-tree/model ] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_RPI=true
fi

# ── Clinic feature manifest ──────────────────────────────────────────────────
# Source the per-clinic manifest + helpers; anything unset defaults to "on" so a
# box without a manifest behaves like the pre-feature-flag script.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CLINIC_CONF="${CLINIC_CONF:-/etc/cied-bridge/clinic.conf}"
[ -f "$SELF_DIR/../../tools/feature-lib.sh" ] && . "$SELF_DIR/../../tools/feature-lib.sh"
[ -f "$CLINIC_CONF" ] && . "$CLINIC_CONF"
: "${FEATURE_RELAY_AP:=on}"
: "${FEATURE_PRINTER_WEBUI:=on}"
: "${FEATURE_SAMBA_SHARE:=on}"
: "${FEATURE_IMPORT_HUB:=on}"
type feature_enabled >/dev/null 2>&1 || feature_enabled() { eval "_v=\${$1:-}"; case "$(printf %s "$_v"|tr A-Z a-z)" in on|yes|1|true) return 0;; *) return 1;; esac; }
type apply_feature_service >/dev/null 2>&1 || apply_feature_service() { f=$1; shift; if feature_enabled "$f"; then systemctl enable $* >/dev/null 2>&1||true; systemctl restart $* >/dev/null 2>&1||true; else systemctl disable --now $* >/dev/null 2>&1||true; fi; }

# ── Pin WiFi interface names via udev ────────────────────────────────────────
# USB adapter enumeration order is not stable across reboots — wlan0/wlan1 can
# swap. Pin by MAC so hostapd always gets the right radio.
_pin_wifi_interfaces() {
    local rt5370_mac="" onboard_mac="" usb_mac="" usb_name=""
    # Find RT5370 by its USB vendor:product in the sysfs path (Pi3 + RT5370 combo)
    # wlan* = Pi/classic naming; wlp* = x86 predictable naming
    for dev in /sys/class/net/wlan* /sys/class/net/wlp*; do
        [ -e "$dev/device" ] || continue
        local usbdev=$(readlink -f "$dev/device" 2>/dev/null)
        if echo "$usbdev" | grep -q "148f.*5370" 2>/dev/null; then
            rt5370_mac=$(cat "$dev/address" 2>/dev/null)
        elif echo "$usbdev" | grep -q "/usb" 2>/dev/null; then
            usb_mac=$(cat "$dev/address" 2>/dev/null)
            usb_name=$(basename "$dev")
        else
            onboard_mac=$(cat "$dev/address" 2>/dev/null)
        fi
    done
    if [ -n "$rt5370_mac" ]; then
        local rules=/etc/udev/rules.d/70-wifi-names.rules
        cat > "$rules" << EOF
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="$rt5370_mac", NAME="wlan_ap"
${onboard_mac:+SUBSYSTEM==\"net\", ACTION==\"add\", ATTR{address}==\"$onboard_mac\", NAME=\"wlan_int\"}
EOF
        echo "   udev: RT5370 ($rt5370_mac) -> wlan_ap${onboard_mac:+, onboard ($onboard_mac) -> wlan_int}" >&2
        echo "wlan_ap"
    elif [ -n "$usb_mac" ]; then
        # Non-Pi server with a USB WiFi adapter — pin it for stable naming
        local rules=/etc/udev/rules.d/70-wifi-names.rules
        cat > "$rules" << EOF
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="$usb_mac", NAME="wlan_ap"
EOF
        echo "   udev: USB WiFi ($usb_mac, was $usb_name) -> wlan_ap" >&2
        echo "wlan_ap"
    else
        # Single wireless interface (onboard or PCI) — use it as-is
        local first=$(ls -1d /sys/class/net/wlan* /sys/class/net/wlp* 2>/dev/null | head -1)
        if [ -n "$first" ]; then
            echo "   using $(basename "$first") (only wireless interface)" >&2
            basename "$first"
        else
            echo "   WARNING: no wireless interface found" >&2
            echo "wlan0"
        fi
    fi
}

AP_IFACE=$(_pin_wifi_interfaces)
AP_IP=10.99.0.1
AP_SUBNET=10.99.0.0
AP_NETMASK=255.255.255.0
AP_DHCP_START=10.99.0.10
AP_DHCP_END=10.99.0.50
: "${AP_SSID:=relayap}"
: "${AP_PASS:=RelayPrint#1}"
: "${AP_CHANNEL:=6}"
: "${AP_HIDDEN:=off}"
: "${WIFI_COUNTRY:=DE}"
AP_HIDE=$([ "$(printf %s "$AP_HIDDEN"|tr A-Z a-z)" = on ] && echo 2 || echo 0)

apt-get update -qq
PACKAGES="hostapd dnsmasq cups cups-bsd iptables chrony sshpass rfkill samba ipp-usb"
if ! $IS_RPI; then
    # x86/server: ghostscript provides 'gs' for PCL→PDF conversion (the Python
    # code tries gpcl6, pcl6, gs in order). wireless-tools for iwconfig if needed.
    PACKAGES="$PACKAGES ghostscript wireless-tools"
fi
apt-get install -y $PACKAGES

# ── Stop services while reconfiguring ────────────────────────────────────────
systemctl stop hostapd dnsmasq 2>/dev/null || true

# Set the WiFi regulatory country — without this, hostapd may fail with
# "Operation not possible due to RF-kill".
if $IS_RPI; then
    raspi-config nonint do_wifi_country "$WIFI_COUNTRY" 2>/dev/null || true
fi
iw reg set "$WIFI_COUNTRY" 2>/dev/null || true
rfkill unblock wifi 2>/dev/null || rfkill unblock wlan 2>/dev/null || true

# ── Prevent NetworkManager / wpa_supplicant from touching the AP interface ───
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-relay-ap.conf << EOF
[keyfile]
unmanaged-devices=interface-name:${AP_IFACE}
EOF
systemctl restart NetworkManager 2>/dev/null || true

# ── Static IP on the AP interface ────────────────────────────────────────────
# Modern Raspberry Pi OS uses NetworkManager, not dhcpcd.  The AP iface is unmanaged
# by NM (see conf.d above) so we need a systemd oneshot to assign the IP after
# hostapd brings the interface up.

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

# Apply immediately (in case hostapd is already running). Tolerate failure here:
# hostapd brings the AP iface up itself, and we must not abort the whole setup if the
# interface is briefly still blocked/renaming.
ip addr add "${AP_IP}/24" dev "$AP_IFACE" 2>/dev/null || true
ip link set "$AP_IFACE" up 2>/dev/null || true

# ── hostapd — broadcast WPA2-PSK AP ──────────────────────────────────────────
# ignore_broadcast_ssid=0 (BROADCAST). Hiding the SSID bought no real security
# (it is trivially discovered) but it BROKE the Pico 2W: the CYW43439's join path
# does not reliably probe for a hidden SSID, so it cycled join<->fail ~100+ times
# and almost never got an IP (a broadcast AP joined first try). The Pi Zero's
# wpa_supplicant (scan_ssid=1) coped, which masked this. Un-hidden = Pico works.
#
# WPA2-PSK only. WPA3-SAE was attempted but the Pi3's Broadcom brcmfmac chip
# cannot host SAE/PMF in AP mode (nl80211 key validation fails, 2026-07-26).
# Non-Pi servers MAY support SAE if the WiFi chipset's driver handles 802.11w,
# but we keep WPA2-PSK for relay compatibility (ESP32/Pico clients don't need SAE).
cat > /etc/hostapd/hostapd.conf << EOF
interface=${AP_IFACE}
driver=nl80211
# Control interface so the web UI can read associated-station signal strength
# (hostapd_cli all_sta) for the Edge nodes panel.
ctrl_interface=/var/run/hostapd
ctrl_interface_group=0
ssid=${AP_SSID}
ignore_broadcast_ssid=${AP_HIDE}
hw_mode=g
channel=${AP_CHANNEL}
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=${AP_PASS}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ap_max_inactivity=5
EOF
chmod 600 /etc/hostapd/hostapd.conf

# Point hostapd at the config
sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

# ── ARP — fast expiry on AP iface so reconnecting edge nodes aren't blocked ──
cat > /etc/sysctl.d/90-relay-ap-arp.conf << EOF
net.ipv4.neigh.${AP_IFACE}.base_reachable_time_ms = 5000
net.ipv4.neigh.${AP_IFACE}.gc_stale_time = 5
net.ipv4.neigh.${AP_IFACE}.delay_first_probe_time = 1
EOF
# Clean up old fixed-name file if present
rm -f /etc/sysctl.d/90-wlan0-arp.conf

# ── dnsmasq — DHCP on relay subnet (Pi Zero uses static, but keep for fallback) ──
# Back up original config if present
[ -f /etc/dnsmasq.conf ] && mv /etc/dnsmasq.conf /etc/dnsmasq.conf.orig

# bind-dynamic (not bind-interfaces): the AP iface may not be up / may have no address
# when dnsmasq starts (hostapd + the static-IP oneshot run around the same time);
# bind-dynamic binds as the address appears instead of failing at startup.
# No dhcp-host line: the range below is the pool, and the Pi Zero uses a static
# 10.99.0.2. (The old "dhcp-host=*,10.99.0.10 # comment" was invalid syntax —
# '*' is not a valid host and dnsmasq rejects trailing inline comments — and it
# aborted the whole setup.)
cat > /etc/dnsmasq.d/relay-ap.conf << EOF
interface=${AP_IFACE}
bind-dynamic
dhcp-range=${AP_DHCP_START},${AP_DHCP_END},12h
EOF

# ── USB driver: dwc2 replaces dwc_otg (Pi only) ────────────────────────────
# dwc_otg's FIQ FSM only handles split transactions — non-split bulk OUT hangs
# on NAK storms between consecutive print jobs. dwc2 is the mainline driver for
# the same hardware and retries properly at USB 2.0 high speed.
# Non-Pi servers use standard XHCI USB — none of this applies.
if $IS_RPI; then
    BOOT_CONFIG="/boot/firmware/config.txt"
    if [ -f "$BOOT_CONFIG" ]; then
        if ! grep -q "dtoverlay=dwc2" "$BOOT_CONFIG"; then
            cp "$BOOT_CONFIG" "${BOOT_CONFIG}.pre-dwc2"
            if grep -q '^\[all\]' "$BOOT_CONFIG"; then
                sed -i '/^\[all\]/a dtoverlay=dwc2,dr_mode=host' "$BOOT_CONFIG"
            else
                printf '\n[all]\ndtoverlay=dwc2,dr_mode=host\n' >> "$BOOT_CONFIG"
            fi
            echo "Added dtoverlay=dwc2,dr_mode=host to $BOOT_CONFIG (reboot required)"
            NEEDS_REBOOT=1
        fi
    fi
    BOOT_CMDLINE="/boot/firmware/cmdline.txt"
    if [ -f "$BOOT_CMDLINE" ] && grep -q "dwc_otg\.speed=" "$BOOT_CMDLINE"; then
        cp "$BOOT_CMDLINE" "${BOOT_CMDLINE}.pre-dwc2"
        sed -i 's/ *dwc_otg\.speed=[0-9]*//' "$BOOT_CMDLINE"
        echo "Removed dwc_otg.speed from $BOOT_CMDLINE"
    fi
fi

# ── CUPS + ipp-usb ──────────────────────────────────────────────────────────
# We use ipp-usb (IPP-over-USB, protocol 4) instead of the kernel usblp driver
# (protocol 2). The HP Color LaserJet Pro MFP 3302's firmware permanently NAKs
# the protocol-2 bulk-OUT endpoint after processing a large PostScript job,
# wedging all subsequent prints until power-cycled. IPP-over-USB uses a
# separate USB interface with its own endpoints; the printer's IPP
# implementation properly manages job separation at the protocol level.
# ipp-usb is udev-activated (starts when a printer is connected).

# Blacklist usblp so it doesn't claim the printer interface
echo "blacklist usblp" > /etc/modprobe.d/blacklist-usblp.conf
rmmod usblp 2>/dev/null || true

# Unmask ipp-usb (earlier setups masked it)
systemctl unmask ipp-usb 2>/dev/null || true
systemctl unmask ipp-usb.socket 2>/dev/null || true

# Remove old custom usblp backend if present
rm -f /usr/lib/cups/backend/usblp

# Restore standard USB backend (earlier setups chmod 000'd it)
for f in /usr/lib/cups/backend/usb; do
    [ -f "$f" ] && chmod 755 "$f"
done

# Listen on all interfaces so both the AP (Pi Zero) and eth0 (management) work
sed -i 's/^Listen localhost:631$/Listen 0.0.0.0:631/' /etc/cups/cupsd.conf

# Allow access from relay subnet and main LAN
if ! grep -q "Allow from 10\.99\." /etc/cups/cupsd.conf; then
    sed -i '/<Location \/>/{n; /Order allow,deny/a\  Allow from 10.99.0.0/24\n  Allow from 192.168.179.0/24
    }' /etc/cups/cupsd.conf
fi

cupsctl --share-printers --remote-any

# Enable LPR (port 515). Debian 13's cups-bsd ships only the cups-lpd binary,
# not a systemd socket unit, so create the socket-activation units ourselves.
# Raw passthrough: the relay forwards the programmer's PostScript/PCL as-is.
# -o raw bypasses the CUPS filter chain entirely — without it, CUPS's auto-typing
# fails to detect PJL+PCL that lacks "@PJL ENTER LANGUAGE" (e.g. Abbott) and the
# "everywhere" queue renders the PJL header as a text page.
cat > /etc/systemd/system/cups-lpd.socket << 'EOF'
[Unit]
Description=CUPS LPD Protocol Listener (relay LPR on 515)

[Socket]
ListenStream=515
Accept=yes

[Install]
WantedBy=sockets.target
EOF

cat > /etc/systemd/system/cups-lpd@.service << 'EOF'
[Unit]
Description=CUPS LPD per-connection server

[Service]
ExecStart=/usr/lib/cups/daemon/cups-lpd -o raw
StandardInput=socket
StandardError=journal
User=lp
Group=lp
EOF

systemctl daemon-reload
systemctl enable --now cups-lpd.socket 2>/dev/null || true

# Guard against CUPS package updates disabling cups-lpd.socket
# cups-lpd-ensure.service checks at every boot and re-enables if needed.
cat > /etc/systemd/system/cups-lpd-ensure.service << 'EOF'
[Unit]
Description=Ensure cups-lpd socket is enabled and active
After=cups.service
Requires=cups.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'systemctl is-active cups-lpd.socket || systemctl enable --now cups-lpd.socket'

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable cups-lpd-ensure.service

# ── chrony — NTP server for Pi Zero relay ────────────────────────────────────
# Pi Zero has no RTC; it syncs time from the Pi3 on every display refresh.
# "local stratum 10" serves the Pi3's own clock even without internet NTP.
if ! grep -q "local stratum 10" /etc/chrony/chrony.conf; then
    cat >> /etc/chrony/chrony.conf << 'EOF'

# Serve local clock to relay subnet even without internet NTP access
local stratum 10
allow 10.99.0.0/24
EOF
fi

# ── USB printer auto-configuration ───────────────────────────────────────────
# udev fires cups-autoconf-printer.service whenever a USB printer is plugged in.
# The service runs cups-autoconf-printer.sh which updates the LaserPrinter queue
# to match whatever printer is connected — no manual lpadmin needed per site.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SCRIPT_DIR/cups-autoconf-printer.sh"    /usr/local/sbin/
install -m 755 "$SCRIPT_DIR/printer-config-server.py"    /usr/local/sbin/
install -m 755 "$SCRIPT_DIR/relay-print-watchdog.sh"     /usr/local/sbin/
install -m 644 "$SCRIPT_DIR/cups-autoconf-printer.service" /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/printer-config.service"      /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/relay-print-watchdog.service" /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/relay-print-watchdog.timer"  /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/99-usb-printer.rules"        /etc/udev/rules.d/
udevadm control --reload-rules
systemctl daemon-reload
# Self-heal a stopped print queue (ipp backend "stop printer") unattended.
systemctl enable --now relay-print-watchdog.timer

# ── Samba share for KardiSynch export (data hub role) ────────────────────────
# When FEATURE_SAMBA_SHARE is on, files deposited via the export relay appear
# in \\PI3\import for KardiSynch to consume. Uses a separate directory from
# /data/pdf (whose watcher deletes imports).
IMPORT_DIR=/var/lib/cied-export/import
mkdir -p "$IMPORT_DIR"
chmod 777 "$IMPORT_DIR"

cat > /etc/samba/smb.conf << 'EOF'
[global]
   workgroup = WORKGROUP
   server string = CIED Hub
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
EXPORT_SRC="$SCRIPT_DIR/../../export-relay/pi3-server"
if [ -f "$EXPORT_SRC/export-config-server.py" ]; then
    install -m 755 "$EXPORT_SRC/export-config-server.py" /usr/local/sbin/
    install -m 644 "$EXPORT_SRC/export-config.service"   /etc/systemd/system/
fi

# ── Import hub (queue between ingest relays and delivery relays) ─────────────
IMPORT_HUB_SRC="$SCRIPT_DIR/../../import-relay/pi3-server"
IMPORT_HUB_INSTALLED=0
if feature_enabled FEATURE_IMPORT_HUB && [ -f "$IMPORT_HUB_SRC/import-server.py" ]; then
    install -m 755 "$IMPORT_HUB_SRC/import-server.py"      /usr/local/sbin/
    install -m 644 "$IMPORT_HUB_SRC/import-server.service" /etc/systemd/system/
    mkdir -p /var/lib/cied-import-hub
    IMPORT_HUB_INSTALLED=1
elif feature_enabled FEATURE_IMPORT_HUB; then
    echo "NOTE: FEATURE_IMPORT_HUB=on but import-relay/pi3-server not found — skipping."
fi

systemctl daemon-reload

# ── Enable and start (per feature manifest) ──────────────────────────────────
if feature_enabled FEATURE_RELAY_AP; then
    systemctl unmask hostapd
    systemctl enable  hostapd dnsmasq chrony >/dev/null 2>&1 || true
    systemctl restart dnsmasq chrony 2>/dev/null || true
    systemctl start   hostapd
    echo "   [FEATURE_RELAY_AP=on]  up:   hostapd dnsmasq chrony"
else
    systemctl disable --now hostapd dnsmasq 2>/dev/null || true
    echo "   [FEATURE_RELAY_AP=off] down: hostapd dnsmasq"
fi
# Printer web UI + CUPS raw queue + LPD listener (+ its boot-guard) + watchdog.
# cups-lpd-ensure re-enables the socket every boot, so it must be in this group
# or an off-toggle would be undone on the next reboot.
apply_feature_service FEATURE_PRINTER_WEBUI \
    "cups printer-config cups-lpd.socket cups-lpd-ensure.service relay-print-watchdog.timer"
apply_feature_service FEATURE_SAMBA_SHARE "smbd nmbd export-config"
if [ "$IMPORT_HUB_INSTALLED" = "1" ]; then
    apply_feature_service FEATURE_IMPORT_HUB "import-server"
else
    systemctl disable --now import-server 2>/dev/null || true
fi

# Add the default user to lpadmin so they can manage CUPS
if $IS_RPI; then
    usermod -aG lpadmin pi 2>/dev/null || true
else
    SUDO_USER="${SUDO_USER:-}"
    [ -n "$SUDO_USER" ] && usermod -aG lpadmin "$SUDO_USER" 2>/dev/null || true
fi

# ── LaserPrinter queue — via ipp-usb ─────────────────────────────────────────
# ipp-usb exposes the printer as ipp://localhost:60000/ipp/print.
# The 'everywhere' model auto-discovers capabilities via IPP; CUPS filters
# the programmer's PostScript/PCL into the printer's preferred format.
IPP_URI="ipp://localhost:60000/ipp/print"
if lpstat -p LaserPrinter >/dev/null 2>&1; then
    CURRENT_URI=$(lpstat -v LaserPrinter 2>/dev/null | awk '{print $NF}')
    if [ "$CURRENT_URI" != "$IPP_URI" ]; then
        lpadmin -p LaserPrinter -v "$IPP_URI" -m everywhere -E
        echo "LaserPrinter queue switched to $IPP_URI (was $CURRENT_URI)"
    else
        echo "LaserPrinter queue OK: $IPP_URI"
    fi
else
    lpadmin -p LaserPrinter -E -v "$IPP_URI" -m everywhere
    echo "LaserPrinter queue created: $IPP_URI (IPP Everywhere)."
fi

PLATFORM=$($IS_RPI && echo "Raspberry Pi" || echo "$(uname -m) server")
echo ""
echo "Platform: $PLATFORM ($(hostname))"
echo "Relay AP:  ssid=${AP_SSID} $([ "$AP_HIDE" = 2 ] && echo '(hidden)' || echo '(broadcast)')  passphrase=${AP_PASS}"
echo "Hub ${AP_IFACE}: ${AP_IP}"
LAN_IFACE=$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')
LAN_IFACE="${LAN_IFACE:-eth0}"
echo "LAN ($LAN_IFACE):  $(ip -4 addr show "$LAN_IFACE" 2>/dev/null | awk '/inet /{print $2}' || echo 'check manually')"
echo ""
echo "Features:"
echo "  RELAY_AP=$FEATURE_RELAY_AP  PRINTER_WEBUI=$FEATURE_PRINTER_WEBUI"
echo "  SAMBA_SHARE=$FEATURE_SAMBA_SHARE  IMPORT_HUB=$FEATURE_IMPORT_HUB"
if feature_enabled FEATURE_SAMBA_SHARE; then
    echo "  Samba: \\\\\\\\$(hostname)\\import -> $IMPORT_DIR"
fi
if [ "$IMPORT_HUB_INSTALLED" = "1" ]; then
    echo "  Import hub: http://${AP_IP}:8090/"
fi
echo ""
if $IS_RPI; then
    echo "USB driver: dwc2 (mainline, handles NAK retries correctly)"
fi
echo "Printer backend: ipp-usb (IPP-over-USB, protocol 4)"
echo "Test: plug in a USB printer, then check: lpstat -v LaserPrinter"
if [ "${NEEDS_REBOOT:-0}" = "1" ]; then
    echo ""
    echo "*** REBOOT REQUIRED — dtoverlay=dwc2 was added to config.txt ***"
fi
