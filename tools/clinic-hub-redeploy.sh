#!/bin/bash
# clinic-hub-redeploy.sh — idempotent hub provisioner.
#
# Works on Raspberry Pi OS AND Debian/Ubuntu x86 servers.
# Fresh: installs packages, configures AP + CUPS + Samba, deploys all code.
# Existing: updates code + configs, skips what's already in place.
#
# Usage:
#   PI=192.168.179.110 REMOTE_USER=pi bash tools/clinic-hub-redeploy.sh
#   PI=ciedserver01.fritz.box REMOTE_USER=basti bash tools/clinic-hub-redeploy.sh
#   PI=10.99.0.1 bash tools/clinic-hub-redeploy.sh       # defaults: REMOTE_USER=basti
#   bash tools/clinic-hub-redeploy.sh                     # PI=10.99.0.1 REMOTE_USER=basti
#
# Environment:
#   PI           — target IP or hostname (default 10.99.0.1)
#   REMOTE_USER  — SSH user (default basti)
#   AP_SSID      — relay AP SSID (default relayap)
#   AP_PASS      — relay AP passphrase (default RelayPrint#1)
#   AP_CHANNEL   — WiFi channel (default 6)
#   AP_HIDDEN    — on/off (default off)
#   CLINIC_FLAGS — extra flags for configure-clinic.sh

set -euo pipefail

PI="${PI:-10.99.0.1}"
REMOTE_USER="${REMOTE_USER:-basti}"
KEY="$HOME/.ssh/id_ed25519"
SSH_OPTS=(-o IdentitiesOnly=yes -i "$KEY")
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/printer-relay/pi3-server"
EXPORT_SRC="$REPO/export-relay/pi3-server"
IMPORT_SRC="$REPO/import-relay/pi3-server"
TOOLS="$REPO/tools"
SW_VERSION="$(cat "$REPO/VERSION" 2>/dev/null | tr -d '[:space:]')"
SW_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"

: "${AP_SSID:=relayap}"
: "${AP_PASS:=RelayPrint#1}"
: "${AP_CHANNEL:=6}"
: "${AP_HIDDEN:=off}"
: "${WIFI_COUNTRY:=DE}"
CLINIC_FLAGS="${CLINIC_FLAGS:-}"

AP_IP=10.99.0.1
AP_DHCP_START=10.99.0.10
AP_DHCP_END=10.99.0.50
AP_HIDE=$([ "$(printf %s "$AP_HIDDEN" | tr A-Z a-z)" = on ] && echo 2 || echo 0)

_ssh()  { ssh  "${SSH_OPTS[@]}" "$REMOTE_USER@$PI" "$@"; }
_ssht() { ssh -t "${SSH_OPTS[@]}" "$REMOTE_USER@$PI" "$@"; }
_scp()  { scp  "${SSH_OPTS[@]}" "$@" "$REMOTE_USER@$PI":/tmp/; }

echo ">> checking reachability of hub ($PI as $REMOTE_USER)"
ping -c1 -W2 "$PI" >/dev/null 2>&1 || { echo "!! $PI not reachable"; exit 1; }
_ssh 'hostname' || { echo "!! SSH failed"; exit 1; }

# ── Copy files ───────────────────────────────────────────────────────────────
echo ">> copying files"
_scp \
    "$SRC/printer-config-server.py" \
    "$SRC/printer-config.service" \
    "$SRC/ipp-usb-ensure.service" \
    "$SRC/cups-autoconf-printer.sh" \
    "$SRC/relay-print-watchdog.sh" \
    "$SRC/relay-print-watchdog.service" \
    "$SRC/relay-print-watchdog.timer" \
    "$SRC/usblp-backend" \
    "$SRC/usb-reset-printer.sh" \
    "$SRC/hostapd-station-handler.sh" \
    "$SRC/hostapd-station-handler.service" \
    "$SRC/station-health-monitor.sh" \
    "$SRC/station-health-monitor.service" \
    "$SRC/99-usb-printer.rules" \
    "$TOOLS/feature-lib.sh" \
    "$TOOLS/configure-clinic.sh" \
    "$TOOLS/clinic.conf.example"

[ -f "$SRC/deploy-printer-config" ] && _scp "$SRC/deploy-printer-config"
[ -f "$SRC/gpcl6" ] && _scp "$SRC/gpcl6" && echo "   + gpcl6 (PCL→PDF converter)"

[ -f "$EXPORT_SRC/export-config-server.py" ] && _scp \
    "$EXPORT_SRC/export-config-server.py" \
    "$EXPORT_SRC/export-config.service" && echo "   + export server"
[ -f "$IMPORT_SRC/import-server.py" ] && _scp \
    "$IMPORT_SRC/import-server.py" \
    "$IMPORT_SRC/import-server.service" && echo "   + import hub"

# ESP firmware binary (from the esp32-pendrive build, if present)
ESP_FW="$(cd "$(dirname "$0")/../../esp32-pendrive-s3-wifi-dongle/build" 2>/dev/null && pwd)/usb_dongle.bin"
[ -f "$ESP_FW" ] && _scp "$ESP_FW" && echo "   + ESP firmware binary"

# ── Generate remote setup script with config values baked in ─────────────────
REMOTE_SCRIPT=$(mktemp)
trap 'rm -f "$REMOTE_SCRIPT"' EXIT

cat > "$REMOTE_SCRIPT" << ENDSCRIPT
#!/bin/bash
set -e

AP_SSID="$AP_SSID"
AP_PASS="$AP_PASS"
AP_CHANNEL="$AP_CHANNEL"
AP_HIDE="$AP_HIDE"
AP_IP="$AP_IP"
AP_DHCP_START="$AP_DHCP_START"
AP_DHCP_END="$AP_DHCP_END"
SW_VERSION="$SW_VERSION"
SW_COMMIT="$SW_COMMIT"
WIFI_COUNTRY="$WIFI_COUNTRY"

# ── Platform detection ─────────────────────────────────────────────────────
IS_RPI=false
if [ -f /proc/device-tree/model ] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_RPI=true
fi
echo "   platform: \$(\$IS_RPI && echo 'Raspberry Pi' || echo "\$(uname -m) server")"

# ── Pin WiFi interface names via udev ─────────────────────────────────────
_pin_wifi_interfaces() {
    local ap_mac="" ap_name="wlan_ap" int_name="wlan_int" onboard_mac="" usb_mac="" usb_name="" rules=""
    # wlan* = Pi/classic naming; wlp* = x86 predictable naming
    for dev in /sys/class/net/wlan* /sys/class/net/wlp*; do
        [ -d "\$dev" ] || continue
        local mac=\$(< "\$dev/address")
        local uevent="\$dev/device/uevent"
        local devlink=\$(readlink -f "\$dev/device" 2>/dev/null)
        if [ -f "\$uevent" ] && grep -q '148F.*5370\|5370.*148F' "\$uevent" 2>/dev/null; then
            ap_mac="\$mac"
        elif [[ "\$mac" == b8:27:eb:* ]] || [[ "\$mac" == dc:a6:32:* ]] || [[ "\$mac" == d8:3a:dd:* ]] || [[ "\$mac" == e4:5f:01:* ]]; then
            onboard_mac="\$mac"
        elif echo "\$devlink" | grep -q "/usb" 2>/dev/null; then
            usb_mac="\$mac"
            usb_name=\$(basename "\$dev")
        fi
    done

    if [ -n "\$ap_mac" ]; then
        rules="SUBSYSTEM==\"net\", ACTION==\"add\", ATTR{address}==\"\$ap_mac\", NAME=\"\$ap_name\""
        [ -n "\$onboard_mac" ] && rules="\$rules\nSUBSYSTEM==\"net\", ACTION==\"add\", ATTR{address}==\"\$onboard_mac\", NAME=\"\$int_name\""
        printf '%b\n' "\$rules" | sudo tee /etc/udev/rules.d/70-wifi-names.rules >/dev/null
        echo "   udev: \$ap_mac → \$ap_name" >&2
        echo "\$ap_name"
    elif [ -n "\$usb_mac" ]; then
        # Non-Pi server with a USB WiFi adapter
        printf 'SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="%s", NAME="wlan_ap"\n' "\$usb_mac" | sudo tee /etc/udev/rules.d/70-wifi-names.rules >/dev/null
        echo "   udev: USB WiFi (\$usb_mac, was \$usb_name) → wlan_ap" >&2
        echo "wlan_ap"
    else
        local first=\$(ls -1d /sys/class/net/wlan* /sys/class/net/wlp* 2>/dev/null | head -1)
        if [ -n "\$first" ]; then
            echo "   using \$(basename "\$first")" >&2
            basename "\$first"
        else
            echo "   WARNING: no wireless interface found" >&2
            echo "wlan0"
        fi
    fi
}

AP_IFACE=\$(_pin_wifi_interfaces)

# ── Detect fresh system ─────────────────────────────────────────────────────
FRESH=0
command -v hostapd >/dev/null 2>&1 || type hostapd >/dev/null 2>&1 || [ -x /usr/sbin/hostapd ] || FRESH=1
command -v cupsd   >/dev/null 2>&1 || type cupsd   >/dev/null 2>&1 || [ -x /usr/sbin/cupsd ]   || FRESH=1

if [ "\$FRESH" = 1 ]; then
    echo ">> [fresh system] installing packages"
    sudo apt-get update -qq
    PKGS="hostapd dnsmasq cups cups-bsd chrony samba ipp-usb rfkill python3-pip util-linux-extra"
    if ! \$IS_RPI; then
        # ghostscript provides 'gs' for PCL→PDF; wireless-tools for iwconfig
        PKGS="\$PKGS ghostscript wireless-tools"
    fi
    sudo apt-get install -y \$PKGS
fi

# ── Feature manifest ────────────────────────────────────────────────────────
sudo install -m 755 /tmp/feature-lib.sh      /usr/local/sbin/feature-lib.sh
sudo install -m 755 /tmp/configure-clinic.sh /usr/local/sbin/configure-clinic.sh
sudo install -d -m 755 /etc/cied-bridge
if [ ! -f /etc/cied-bridge/clinic.conf ]; then
    sudo install -m 600 /tmp/clinic.conf.example /etc/cied-bridge/clinic.conf
    echo '   seeded clinic.conf'
fi

# ── Software version stamp ─────────────────────────────────────────────────
sudo tee /etc/cied-bridge/version >/dev/null << EOF
version=\$SW_VERSION
commit=\$SW_COMMIT
deployed=\$(date -Iseconds)
EOF
echo "   version \$SW_VERSION (\$SW_COMMIT)"

# ── Relay AP (hostapd + dnsmasq + static IP) ────────────────────────────────
if [ "\$FRESH" = 1 ] || [ ! -f /etc/hostapd/hostapd.conf ]; then
    echo ">> [AP] configuring hostapd + dnsmasq"

    sudo rfkill unblock wifi 2>/dev/null || sudo rfkill unblock wlan 2>/dev/null || true
    sudo iw reg set "\$WIFI_COUNTRY" 2>/dev/null || true

    # Prevent NetworkManager from touching the AP interface
    sudo mkdir -p /etc/NetworkManager/conf.d
    printf '[keyfile]\nunmanaged-devices=interface-name:%s\n' "\$AP_IFACE" | sudo tee /etc/NetworkManager/conf.d/99-relay-ap.conf >/dev/null
    sudo systemctl restart NetworkManager 2>/dev/null || true

    sudo tee /etc/hostapd/hostapd.conf >/dev/null << EOF
interface=\$AP_IFACE
driver=nl80211
ctrl_interface=/var/run/hostapd
ctrl_interface_group=0
ssid=\$AP_SSID
ignore_broadcast_ssid=\$AP_HIDE
hw_mode=g
channel=\$AP_CHANNEL
country_code=\$WIFI_COUNTRY
ieee80211d=1
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=\$AP_PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
    sudo chmod 600 /etc/hostapd/hostapd.conf
    sudo sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

    # rfkill unblock before hostapd starts
    sudo mkdir -p /etc/systemd/system/hostapd.service.d
    printf '[Service]\nExecStartPre=/usr/sbin/rfkill unblock wlan\n' | sudo tee /etc/systemd/system/hostapd.service.d/rfkill-unblock.conf >/dev/null

    # Static IP via systemd oneshot
    sudo tee /etc/systemd/system/relay-ap-ip.service >/dev/null << EOF
[Unit]
Description=Static IP for relay AP (\$AP_IFACE)
After=hostapd.service
Requires=hostapd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip addr add \$AP_IP/24 dev \$AP_IFACE

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable relay-ap-ip.service

    sudo ip addr add "\$AP_IP/24" dev "\$AP_IFACE" 2>/dev/null || true
    sudo ip link set "\$AP_IFACE" up 2>/dev/null || true

    # dnsmasq
    [ -f /etc/dnsmasq.conf ] && [ ! -f /etc/dnsmasq.conf.orig ] && sudo mv /etc/dnsmasq.conf /etc/dnsmasq.conf.orig
    sudo tee /etc/dnsmasq.d/relay-ap.conf >/dev/null << EOF
interface=\$AP_IFACE
bind-dynamic
dhcp-range=\$AP_DHCP_START,\$AP_DHCP_END,12h
EOF

    # chrony — NTP server for relay nodes
    if ! grep -q "local stratum 10" /etc/chrony/chrony.conf 2>/dev/null; then
        printf '\nlocal stratum 10\nallow 10.99.0.0/24\n' | sudo tee -a /etc/chrony/chrony.conf >/dev/null
    fi

    sudo systemctl unmask hostapd
    sudo systemctl enable hostapd dnsmasq chrony relay-ap-ip >/dev/null 2>&1 || true
    sudo systemctl restart dnsmasq chrony 2>/dev/null || true
    sudo systemctl start hostapd 2>/dev/null || true
    echo "   AP configured: ssid=\$AP_SSID channel=\$AP_CHANNEL"
else
    echo ">> [AP] already configured, applying tweaks"
    sudo sed -i "/^ap_max_inactivity=5\$/d" /etc/hostapd/hostapd.conf 2>/dev/null || true
    sudo sed -i "/^skip_inactivity_poll=/d" /etc/hostapd/hostapd.conf 2>/dev/null || true
    if ! sudo grep -q "^ctrl_interface=" /etc/hostapd/hostapd.conf 2>/dev/null; then
        printf "ctrl_interface=/var/run/hostapd\nctrl_interface_group=0\n" | sudo tee -a /etc/hostapd/hostapd.conf >/dev/null
    fi
    if ! sudo grep -q "^country_code=" /etc/hostapd/hostapd.conf 2>/dev/null; then
        printf "country_code=\$WIFI_COUNTRY\nieee80211d=1\n" | sudo tee -a /etc/hostapd/hostapd.conf >/dev/null
    fi
fi

# ARP + power save
sudo rm -f /etc/sysctl.d/90-wlan0-arp.conf
sudo sysctl -w "net.ipv4.neigh.\$AP_IFACE.base_reachable_time_ms=30000" 2>/dev/null || true
sudo sysctl -w "net.ipv4.neigh.\$AP_IFACE.gc_stale_time=60" 2>/dev/null || true
sudo iw dev "\$AP_IFACE" set power_save off 2>/dev/null || true

# ── CUPS + LPR ──────────────────────────────────────────────────────────────
if which cups >/dev/null 2>&1; then
    sudo sed -i 's/^Listen localhost:631\$/Listen 0.0.0.0:631/' /etc/cups/cupsd.conf 2>/dev/null || true
    if ! grep -q "Allow from 10\.99\." /etc/cups/cupsd.conf 2>/dev/null; then
        sudo sed -i '/<Location \/>/{n; /Order allow,deny/a\  Allow from 10.99.0.0/24\n  Allow from 192.168.179.0/24
        }' /etc/cups/cupsd.conf 2>/dev/null || true
    fi
    sudo cupsctl --share-printers --remote-any 2>/dev/null || true

    if [ ! -f /etc/systemd/system/cups-lpd.socket ]; then
        sudo tee /etc/systemd/system/cups-lpd.socket >/dev/null << 'EOF'
[Unit]
Description=CUPS LPD Protocol Listener (relay LPR on 515)

[Socket]
ListenStream=515
Accept=yes

[Install]
WantedBy=sockets.target
EOF
    fi
    # Always write the service file — -o raw bypasses CUPS filtering so PJL+PCL
    # from programmers (Abbott) doesn't get rendered as a text page.
    sudo tee /etc/systemd/system/cups-lpd@.service >/dev/null << 'EOF'
[Unit]
Description=CUPS LPD per-connection server

[Service]
ExecStart=/usr/lib/cups/daemon/cups-lpd -o raw
StandardInput=socket
StandardError=journal
User=lp
Group=lp
EOF

    if [ ! -f /etc/systemd/system/cups-lpd-ensure.service ]; then
        sudo tee /etc/systemd/system/cups-lpd-ensure.service >/dev/null << 'EOF'
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
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable --now cups-lpd.socket cups-lpd-ensure.service 2>/dev/null || true
fi

# ── ipp-usb + usblp blacklist ───────────────────────────────────────────────
echo "blacklist usblp" | sudo tee /etc/modprobe.d/blacklist-usblp.conf >/dev/null
sudo rmmod usblp 2>/dev/null || true
sudo systemctl unmask ipp-usb ipp-usb.socket 2>/dev/null || true
sudo rm -f /usr/lib/cups/backend/usblp
[ -f /usr/lib/cups/backend/usb ] && sudo chmod 755 /usr/lib/cups/backend/usb 2>/dev/null || true

# ── Install relay scripts + services ────────────────────────────────────────
sudo install -m 755 /tmp/printer-config-server.py    /usr/local/sbin/
sudo install -m 755 /tmp/cups-autoconf-printer.sh    /usr/local/sbin/
sudo install -m 755 /tmp/relay-print-watchdog.sh     /usr/local/sbin/
sudo install -m 755 /tmp/usb-reset-printer.sh        /usr/local/sbin/
sudo install -m 755 /tmp/hostapd-station-handler.sh  /usr/local/sbin/
sudo install -m 755 /tmp/station-health-monitor.sh   /usr/local/sbin/
sudo install -m 644 /tmp/printer-config.service /tmp/ipp-usb-ensure.service \
    /tmp/relay-print-watchdog.service /tmp/relay-print-watchdog.timer \
    /tmp/hostapd-station-handler.service /tmp/station-health-monitor.service /etc/systemd/system/
sudo install -m 644 /tmp/99-usb-printer.rules /etc/udev/rules.d/ 2>/dev/null || true
[ -f /tmp/deploy-printer-config ] && sudo install -m 755 /tmp/deploy-printer-config /usr/local/sbin/ 2>/dev/null || true
sudo udevadm control --reload-rules 2>/dev/null || true

sudo mkdir -p /var/lib/relay-ui /var/lib/relay-ui/job-archive
sudo mkdir -p /usr/local/share/relay-firmware
# gpcl6 for PCL→PDF archive conversion (Pi: shipped as ARM binary; x86: from ghostpcl package)
if \$IS_RPI && [ -f /tmp/gpcl6 ]; then
    sudo install -m 755 /tmp/gpcl6 /usr/local/bin/gpcl6 && echo "   gpcl6 installed (ARM binary)"
fi
[ -f /tmp/usb_dongle.bin ] && sudo install -m 644 /tmp/usb_dongle.bin /usr/local/share/relay-firmware/
sudo rm -f /var/lib/printer-autoconf/decision

# esptool for USB firmware flashing from the web UI
if ! which esptool.py >/dev/null 2>&1; then
    which pip3 >/dev/null 2>&1 || sudo apt-get install -y python3-pip 2>/dev/null || true
    sudo pip3 install esptool --break-system-packages 2>/dev/null || true
fi

sudo systemctl enable ipp-usb-ensure 2>/dev/null || true
sudo systemctl enable --now hostapd-station-handler station-health-monitor 2>/dev/null || true
sudo systemctl enable printer-config 2>/dev/null || true

# ── Export server ───────────────────────────────────────────────────────────
[ -f /tmp/export-config-server.py ] && {
    sudo install -m 755 /tmp/export-config-server.py /usr/local/sbin/
    sudo install -m 644 /tmp/export-config.service   /etc/systemd/system/
}

# ── Import hub ──────────────────────────────────────────────────────────────
[ -f /tmp/import-server.py ] && {
    sudo install -m 755 /tmp/import-server.py      /usr/local/sbin/
    sudo install -m 644 /tmp/import-server.service /etc/systemd/system/
    sudo mkdir -p /var/lib/cied-import-hub
}

# ── Samba share ─────────────────────────────────────────────────────────────
sudo mkdir -p /var/lib/cied-export/import
sudo chmod 777 /var/lib/cied-export/import
if [ -x /usr/sbin/smbd ] && ! grep -q '\\[import\\]' /etc/samba/smb.conf 2>/dev/null; then
    sudo tee /etc/samba/smb.conf >/dev/null << 'EOF'
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
    echo "   configured Samba [import] share"
fi

# ── Boot config (dwc2 + RTC — Pi only) ────────────────────────────────────
NEEDS_REBOOT=0
if \$IS_RPI; then
    BOOT_CONFIG="/boot/firmware/config.txt"
    if [ -f "\$BOOT_CONFIG" ]; then
        if ! sudo sed -n '/^\[all\]/,/^\[/p' "\$BOOT_CONFIG" 2>/dev/null | grep -q "dtoverlay=dwc2"; then
            sudo cp "\$BOOT_CONFIG" "\${BOOT_CONFIG}.pre-dwc2"
            if grep -q '^\[all\]' "\$BOOT_CONFIG"; then
                sudo sed -i '/^\[all\]/a dtoverlay=dwc2,dr_mode=host' "\$BOOT_CONFIG"
            else
                printf '\n[all]\ndtoverlay=dwc2,dr_mode=host\n' | sudo tee -a "\$BOOT_CONFIG" >/dev/null
            fi
            NEEDS_REBOOT=1
        fi
        if ! grep -q "dtoverlay=i2c-rtc,ds3231" "\$BOOT_CONFIG"; then
            if grep -q '^\[all\]' "\$BOOT_CONFIG"; then
                sudo sed -i '/^\[all\]/a dtoverlay=i2c-rtc,ds3231' "\$BOOT_CONFIG"
            fi
            NEEDS_REBOOT=1
        fi
    fi
    BOOT_CMDLINE="/boot/firmware/cmdline.txt"
    if [ -f "\$BOOT_CMDLINE" ] && grep -q "dwc_otg\.speed=" "\$BOOT_CMDLINE"; then
        sudo sed -i 's/ *dwc_otg\.speed=[0-9]*//' "\$BOOT_CMDLINE"
    fi
fi

# ── RTC sync ───────────────────────────────────────────────────────────
# hwclock is in util-linux-extra (not installed by default on Debian 13)
which hwclock >/dev/null 2>&1 || sudo apt-get install -y util-linux-extra 2>/dev/null || true
if [ -e /dev/rtc0 ] && which hwclock >/dev/null 2>&1; then
    sudo hwclock -w 2>/dev/null && echo "   RTC synced to system clock" || true
fi

# ── lpadmin group ───────────────────────────────────────────────────────────
sudo usermod -aG lpadmin "\$(whoami)" 2>/dev/null || true

# ── Daemon reload + enable + apply manifest ─────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable --now relay-print-watchdog.timer 2>/dev/null || true

sudo sh -c '. /usr/local/sbin/feature-lib.sh; . /etc/cied-bridge/clinic.conf 2>/dev/null || true
    apply_feature_service FEATURE_SAMBA_SHARE   "smbd nmbd export-config"
    apply_feature_service FEATURE_IMPORT_HUB    "import-server"
    apply_feature_service FEATURE_PRINTER_WEBUI "cups printer-config cups-lpd.socket cups-lpd-ensure.service relay-print-watchdog.timer"
    echo "   manifest applied:"; grep -E "^FEATURE_" /etc/cied-bridge/clinic.conf 2>/dev/null | sed "s/^/     /"'

# ── Printer autoconf (background) ──────────────────────────────────────────
sudo /usr/local/sbin/cups-autoconf-printer.sh &

echo ""
[ "\$NEEDS_REBOOT" = 1 ] && echo "   *** dtoverlay changes — REBOOT REQUIRED ***"
echo "   hostapd:   \$(systemctl is-active hostapd 2>/dev/null || echo 'n/a')"
echo "   dnsmasq:   \$(systemctl is-active dnsmasq 2>/dev/null || echo 'n/a')"
echo "   cups:      \$(systemctl is-active cups 2>/dev/null || echo 'n/a')"
echo "   smbd:      \$(systemctl is-active smbd 2>/dev/null || echo 'n/a')"
echo "   export:    \$(systemctl is-active export-config 2>/dev/null || echo 'n/a')"
echo "   import:    \$(systemctl is-active import-server 2>/dev/null || echo 'n/a')"
echo "   watchdog:  \$(systemctl is-active relay-print-watchdog.timer)"
echo "   sta-health:\$(systemctl is-active station-health-monitor)"
ENDSCRIPT

# ── Push and execute ─────────────────────────────────────────────────────────
echo ">> deploying on hub (needs sudo password on fresh system)"
_scp "$REMOTE_SCRIPT"
_ssht "bash /tmp/$(basename "$REMOTE_SCRIPT")"

echo
echo ">> done."
echo "   Printer UI: http://$PI/"
echo "   Export UI:  http://$PI:8080/"
echo "   Samba:      \\\\$PI\\import"
echo "   Import hub: http://$PI:8090/"
