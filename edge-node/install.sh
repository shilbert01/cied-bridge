#!/bin/bash
# Run on the Pi as root: sudo bash install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing CIED Bridge edge node"

# 1. Node config
install -d -m 750 /etc/cied-edge
if [[ ! -f /etc/cied-edge/node.conf ]]; then
    install -m 640 "$SCRIPT_DIR/config/node.conf.example" /etc/cied-edge/node.conf
    echo "    CREATED /etc/cied-edge/node.conf — edit before rebooting!"
fi

# 2. Kernel module autoload
install -m 644 "$SCRIPT_DIR/boot-config/usb-gadget.conf" /etc/modules-load.d/usb-gadget.conf

# 3. Boot config overlay
CONFIG_TXT=/boot/firmware/config.txt
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt
if ! grep -q "dtoverlay=dwc2" "$CONFIG_TXT"; then
    cat "$SCRIPT_DIR/boot-config/config.txt.append" >> "$CONFIG_TXT"
    echo "    Appended dwc2 overlay to $CONFIG_TXT"
else
    echo "    dwc2 overlay already present in $CONFIG_TXT"
fi

# 4a. Disable services not needed on edge node (speeds up boot)
# hciuart/bluetooth depend on BT hardware disabled via dtoverlay=disable-bt
systemctl disable --now bluetooth hciuart 2>/dev/null || true
# cloud-init adds ~26 s on every boot and serves no purpose on a fixed embedded node
touch /etc/cloud/cloud-init.disabled
systemctl disable --now cloud-init cloud-init-local cloud-config cloud-final 2>/dev/null || true
# avahi mDNS not needed; edge is accessed by static DHCP lease / known IP
systemctl disable --now avahi-daemon 2>/dev/null || true
# Nothing waits on full network-online any more (forwarder retries independently)
systemctl mask NetworkManager-wait-online.service

# 4. Hardware watchdog
if ! grep -q "dtparam=watchdog" "$CONFIG_TXT"; then
    echo "dtparam=watchdog=on" >> "$CONFIG_TXT"
fi
# Tell systemd to use it
if ! grep -q "^RuntimeWatchdogSec" /etc/systemd/system.conf 2>/dev/null; then
    echo "RuntimeWatchdogSec=15" >> /etc/systemd/system.conf
fi

# 5. Gadget scripts
install -m 755 "$SCRIPT_DIR/gadget/setup-gadget.sh"        /usr/local/sbin/setup-gadget.sh
install -m 755 "$SCRIPT_DIR/gadget/create-storage-image.sh" /usr/local/sbin/create-storage-image.sh

# 6. Daemon scripts
install -m 755 "$SCRIPT_DIR/daemon/capture_daemon.py"  /usr/local/bin/capture_daemon.py
install -m 755 "$SCRIPT_DIR/daemon/forwarder.py"       /usr/local/bin/forwarder.py
install -m 755 "$SCRIPT_DIR/daemon/storage_daemon.py"  /usr/local/bin/storage_daemon.py

# 7. Systemd units
install -m 644 "$SCRIPT_DIR/systemd/usb-printer-gadget.service" /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/systemd/capture-daemon.service"     /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/systemd/forwarder.service"          /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/systemd/storage-daemon.service"     /etc/systemd/system/

systemctl daemon-reload
systemctl enable usb-printer-gadget.service capture-daemon.service forwarder.service storage-daemon.service

# 8. Data directories
install -d -m 750 /var/lib/cied-edge/queue
install -d -m 750 /var/lib/cied-edge/sent
install -d -m 750 /var/lib/cied-edge/exports

echo ""
echo "==> Done. Next steps:"
echo "    1. Edit /etc/cied-edge/node.conf (set NODE_ID, VENDOR, CENTRAL_URL, CENTRAL_TOKEN,"
echo "       STORAGE_IMG, EXPORT_DIR, CENTRAL_EXPORT_URL)"
echo "    2. Run: sudo /usr/local/sbin/create-storage-image.sh"
echo "    3. Reboot to activate dwc2 overlay and watchdog"
echo "    4. After reboot: journalctl -fu capture-daemon  to verify gadget is up"
