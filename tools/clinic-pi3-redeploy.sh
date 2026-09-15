#!/bin/bash
# clinic-pi3-redeploy.sh
#
# QUEUED redeploy for the CLINIC Pi3 (10.99.0.1) — run from the laptop when you're
# next on the clinic relay network (USB-WiFi stick / relayap). The HOME printrelay
# Pi3 already has all of this; the clinic one is behind.
#
# Brings the clinic Pi3 up to date:
#   WEB UI — tabbed layout (Jobs/Printers/Nodes), custom node names, source column.
#   AUTOCONF — usb:// direct (ipp-usb stopped+masked), raw pass-through, cached.
#   WATCHDOG — re-enables disabled queue, cancels stuck jobs.
#   EXPORT — Samba share (\\PI3\import), export ingest server, import hub.
#   FEATURE FLAGS — clinic.conf toggles for each role.
#
# Usage (interactive — prompts once for the Pi's sudo password):
#   bash tools/clinic-pi3-redeploy.sh

set -euo pipefail

PI=10.99.0.1
KEY="$HOME/.ssh/id_ed25519"
SSH_OPTS=(-o IdentitiesOnly=yes -i "$KEY")
SRC="$(cd "$(dirname "$0")/../printer-relay/pi3-server" && pwd)"
EXPORT_SRC="$(cd "$(dirname "$0")/../export-relay/pi3-server" && pwd)"
IMPORT_SRC="$(cd "$(dirname "$0")/../import-relay/pi3-server" && pwd)"
TOOLS="$(cd "$(dirname "$0")" && pwd)"

# Optional: pass feature toggles to configure-clinic.sh on the Pi3, e.g.
#   CLINIC_FLAGS="--printer-webui=on --samba=off" bash tools/clinic-pi3-redeploy.sh
# With no flags: a manifest is seeded from the example only if none exists (all
# Pi3 features on = legacy behavior), then the current manifest's toggles are applied.
CLINIC_FLAGS="${CLINIC_FLAGS:-}"

echo ">> checking reachability of clinic Pi3 ($PI)"
ping -c1 -W2 "$PI" >/dev/null 2>&1 || {
    echo "!! $PI not reachable — are you on the clinic relay network (relayap via the stick)?"
    exit 1
}

echo ">> copying updated files"
scp "${SSH_OPTS[@]}" \
    "$SRC/printer-config-server.py" \
    "$SRC/printer-config.service" \
    "$SRC/cups-autoconf-printer.sh" \
    "$SRC/relay-print-watchdog.sh" \
    "$SRC/relay-print-watchdog.service" \
    "$SRC/relay-print-watchdog.timer" \
    "$SRC/usblp-backend" \
    "$SRC/usb-reset-printer.sh" \
    "$TOOLS/feature-lib.sh" \
    "$TOOLS/configure-clinic.sh" \
    "$TOOLS/clinic.conf.example" \
    basti@"$PI":/tmp/

# Export + import hub files (optional — deploy script works even if tree is incomplete)
[ -f "$EXPORT_SRC/export-config-server.py" ] && scp "${SSH_OPTS[@]}" \
    "$EXPORT_SRC/export-config-server.py" \
    "$EXPORT_SRC/export-config.service" \
    basti@"$PI":/tmp/ && echo "   + export server"
[ -f "$IMPORT_SRC/import-server.py" ] && scp "${SSH_OPTS[@]}" \
    "$IMPORT_SRC/import-server.py" \
    "$IMPORT_SRC/import-server.service" \
    basti@"$PI":/tmp/ && echo "   + import hub"

echo ">> [manifest] deploy feature machinery + apply clinic.conf toggles (needs sudo password)"
ssh -t "${SSH_OPTS[@]}" basti@"$PI" "
    set -e
    sudo install -m 755 /tmp/feature-lib.sh      /usr/local/sbin/feature-lib.sh
    sudo install -m 755 /tmp/configure-clinic.sh /usr/local/sbin/configure-clinic.sh
    sudo install -d -m 755 /etc/pacemaker

    # Seed a manifest only if none exists (example = all Pi3 features on = legacy).
    if [ ! -f /etc/pacemaker/clinic.conf ]; then
        sudo install -m 600 /tmp/clinic.conf.example /etc/pacemaker/clinic.conf
        echo '   seeded /etc/pacemaker/clinic.conf from example (all Pi3 features on)'
    fi
    # Apply any requested toggles (no-op when CLINIC_FLAGS is empty).
    if [ -n '$CLINIC_FLAGS' ]; then
        sudo /usr/local/sbin/configure-clinic.sh -y $CLINIC_FLAGS
    fi

    # Enable/disable Pi3 feature services per the manifest (active teardown).
    # clinic.conf is root-only (600), so source + apply entirely inside sudo.
    sudo sh -c '. /usr/local/sbin/feature-lib.sh; . /etc/pacemaker/clinic.conf
        apply_feature_service FEATURE_SAMBA_SHARE    \"smbd nmbd export-config\"
        apply_feature_service FEATURE_IMPORT_HUB     \"import-server\"
        apply_feature_service FEATURE_PRINTER_WEBUI  \"cups printer-config cups-lpd.socket cups-lpd-ensure.service relay-print-watchdog.timer\"
        echo \"   manifest applied:\"; grep -E \"^FEATURE_\" /etc/pacemaker/clinic.conf | sed \"s/^/     /\"'
"

echo ">> [1/2] web UI (tabs + node names + source column) via the NOPASSWD deploy hook"
ssh "${SSH_OPTS[@]}" basti@"$PI" 'sudo -n /usr/local/sbin/deploy-printer-config \
    && echo "   deployed; new-UI markers: tabs=$(grep -c "class=\"tabs\"" /usr/local/sbin/printer-config-server.py) names=$(grep -c set_node_name /usr/local/sbin/printer-config-server.py) source=$(grep -c _job_sources /usr/local/sbin/printer-config-server.py)"'

echo ">> [2/2] autoconf + watchdog + export/import (needs sudo password)"
ssh -t "${SSH_OPTS[@]}" basti@"$PI" '
    sudo install -m 755 /tmp/cups-autoconf-printer.sh /usr/local/sbin/
    sudo install -m 755 /tmp/relay-print-watchdog.sh  /usr/local/sbin/
    sudo install -m 755 /tmp/usb-reset-printer.sh     /usr/local/sbin/
    sudo install -m 644 /tmp/printer-config.service /tmp/relay-print-watchdog.service /tmp/relay-print-watchdog.timer /etc/systemd/system/

    # State dir for custom node names.
    sudo mkdir -p /var/lib/relay-ui

    # Clear stale autoconf decision (old cache may reference ipp-usb).
    sudo rm -f /var/lib/printer-autoconf/decision

    # Stop + mask ipp-usb (we use kernel usblp driver, not libusb).
    sudo systemctl stop ipp-usb 2>/dev/null || true
    sudo systemctl disable ipp-usb 2>/dev/null || true
    sudo systemctl mask ipp-usb ipp-usb.socket 2>/dev/null || true

    # ipp-usb: IPP-over-USB (protocol 4) replaces usblp (protocol 2).
    # HP Color LaserJet Pro MFP 3302 firmware permanently NAKs protocol-2
    # bulk-OUT after large PostScript jobs; IPP-USB uses separate endpoints.
    sudo apt-get install -y ipp-usb 2>/dev/null || true
    echo "blacklist usblp" | sudo tee /etc/modprobe.d/blacklist-usblp.conf >/dev/null
    sudo rmmod usblp 2>/dev/null || true
    sudo systemctl unmask ipp-usb 2>/dev/null || true
    sudo systemctl unmask ipp-usb.socket 2>/dev/null || true
    sudo rm -f /usr/lib/cups/backend/usblp
    [ -f /usr/lib/cups/backend/usb ] && sudo chmod 755 /usr/lib/cups/backend/usb 2>/dev/null || true

    # Wait for ipp-usb to detect the printer (udev-activated)
    sudo systemctl start ipp-usb 2>/dev/null || true
    sleep 3

    # Switch queue to ipp-usb
    IPP_URI="ipp://localhost:60000/ipp/print"
    CURRENT_URI=$(lpstat -v LaserPrinter 2>/dev/null | awk "{print \$NF}" || true)
    if [ -n "$CURRENT_URI" ] && [ "$CURRENT_URI" != "$IPP_URI" ]; then
        sudo lpadmin -p LaserPrinter -v "$IPP_URI" -m everywhere -E
        echo "   queue switched to $IPP_URI (was $CURRENT_URI)"
    fi

    # Fast ARP expiry + hostapd inactivity so reconnecting edge nodes
    # (ESP32, Pi Zero) aren't blocked by stale state after a replug.
    cat <<SYSEOF | sudo tee /etc/sysctl.d/90-wlan0-arp.conf >/dev/null
net.ipv4.neigh.wlan0.base_reachable_time_ms = 5000
net.ipv4.neigh.wlan0.gc_stale_time = 5
net.ipv4.neigh.wlan0.delay_first_probe_time = 1
SYSEOF
    sudo sysctl -p /etc/sysctl.d/90-wlan0-arp.conf 2>/dev/null || true
    sudo sed -i "s/^ap_max_inactivity=.*/ap_max_inactivity=5/" /etc/hostapd/hostapd.conf 2>/dev/null || true

    # dwc2 overlay in config.txt (requires reboot to take effect).
    BOOT_CONFIG="/boot/firmware/config.txt"
    if [ -f "$BOOT_CONFIG" ] && ! grep -q "dtoverlay=dwc2" "$BOOT_CONFIG"; then
        sudo cp "$BOOT_CONFIG" "${BOOT_CONFIG}.pre-dwc2"
        if grep -q "^\[all\]" "$BOOT_CONFIG"; then
            sudo sed -i "/^\[all\]/a dtoverlay=dwc2,dr_mode=host" "$BOOT_CONFIG"
        else
            printf "\n[all]\ndtoverlay=dwc2,dr_mode=host\n" | sudo tee -a "$BOOT_CONFIG" >/dev/null
        fi
        echo "   *** dtoverlay=dwc2 added — REBOOT REQUIRED ***"
    fi
    BOOT_CMDLINE="/boot/firmware/cmdline.txt"
    if [ -f "$BOOT_CMDLINE" ] && grep -q "dwc_otg\.speed=" "$BOOT_CMDLINE"; then
        sudo sed -i "s/ *dwc_otg\.speed=[0-9]*//" "$BOOT_CMDLINE"
        echo "   removed dwc_otg.speed from cmdline.txt"
    fi

    # Export server (if files were copied).
    [ -f /tmp/export-config-server.py ] && {
        sudo install -m 755 /tmp/export-config-server.py /usr/local/sbin/
        sudo install -m 644 /tmp/export-config.service   /etc/systemd/system/
    }

    # Import hub (if files were copied).
    [ -f /tmp/import-server.py ] && {
        sudo install -m 755 /tmp/import-server.py      /usr/local/sbin/
        sudo install -m 644 /tmp/import-server.service /etc/systemd/system/
        sudo mkdir -p /var/lib/pacemaker-import-hub
    }

    # Samba import dir.
    sudo mkdir -p /var/lib/pacemaker-export/import
    sudo chmod 777 /var/lib/pacemaker-export/import

    sudo systemctl daemon-reload
    sudo systemctl enable --now relay-print-watchdog.timer

    # Apply feature toggles for the new services.
    sudo sh -c ". /usr/local/sbin/feature-lib.sh; . /etc/pacemaker/clinic.conf 2>/dev/null || true
        apply_feature_service FEATURE_SAMBA_SHARE   \"smbd nmbd export-config\"
        apply_feature_service FEATURE_IMPORT_HUB    \"import-server\"
        apply_feature_service FEATURE_PRINTER_WEBUI \"cups printer-config cups-lpd.socket cups-lpd-ensure.service relay-print-watchdog.timer\""

    echo "   watchdog: $(systemctl is-active relay-print-watchdog.timer)"
    echo "   ipp-usb + dwc2 driver (usblp blacklisted)"
'

echo
echo ">> done. Verify:"
echo "   curl -s http://$PI/ | grep -oE 'class=\"tabs\"|set_node_name'   # web UI"
echo "   ssh ${SSH_OPTS[*]} basti@$PI 'lpstat -v LaserPrinter'            # usblp:///dev/usb/lp0"
echo "   ssh ${SSH_OPTS[*]} basti@$PI 'systemctl is-active smbd'          # samba"
