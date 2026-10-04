#!/bin/bash
#
# Switch the receiver between USB gadget delivery modes.
#
#   set-mode.sh hid            HID pipe + companion app       (vendor-HID allowed)
#   set-mode.sh serial         CDC-ACM serial + companion app (Herzzentrum Leipzig)
#   set-mode.sh mass-storage   fake USB drive (import-relay)  (unrestricted-USB sites)
#
# Only one gadget can own the single OTG port at a time, so switching tears the
# current gadget/services down before bringing the new one up. HID mode uses this
# subproject's units; mass-storage mode hands off to the reused import-relay
# presenter (usb-import-gadget + import-daemon) — see mass-storage/README.md.
set -euo pipefail

MODE="${1:-}"
GADGET_DIR="/sys/kernel/config/usb_gadget"

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (sudo $0 $MODE)" >&2
    exit 1
fi

teardown_gadget() {
    # Unbind + remove any gadget we (or import-relay) may have created.
    local g
    for g in "$GADGET_DIR"/kardisynch_hid "$GADGET_DIR"/kardisynch_serial "$GADGET_DIR"/import_gadget; do
        [ -d "$g" ] || continue
        echo "" > "$g/UDC" 2>/dev/null || true
        find "$g/configs" -type l -delete 2>/dev/null || true
        find "$g" -depth -type d -exec rmdir {} + 2>/dev/null || true
    done
}

stop_all() {
    systemctl disable --now import-daemon usb-import-gadget 2>/dev/null || true
    systemctl disable --now kardisynch-hid-sender kardisynch-gadget-hid 2>/dev/null || true
    systemctl disable --now kardisynch-serial-sender kardisynch-gadget-serial 2>/dev/null || true
    teardown_gadget
}

case "$MODE" in
    hid)
        echo "== switching to HID mode =="
        stop_all
        systemctl enable --now kardisynch-gadget-hid kardisynch-hid-sender
        echo "HID mode active. /dev/hidg0 up; hid_sender polling the hub."
        ;;
    serial)
        echo "== switching to serial mode =="
        stop_all
        systemctl enable --now kardisynch-gadget-serial kardisynch-serial-sender
        echo "Serial mode active. /dev/ttyGS0 up; serial_sender polling the hub."
        ;;
    mass-storage)
        echo "== switching to mass-storage mode =="
        stop_all
        systemctl enable --now usb-import-gadget import-daemon
        echo "Mass-storage mode active (import-relay presenter)."
        ;;
    *)
        echo "usage: $0 [hid|serial|mass-storage]" >&2
        exit 1
        ;;
esac
