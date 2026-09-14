#!/bin/bash
# usb-reset-printer.sh — hard USB port reset (USBDEVFS_RESET ioctl).
# Equivalent to a cable replug. Clears endpoint stalls that survive unbind/rebind.
#
# Usage: usb-reset-printer.sh [quiet]
#   quiet — suppress output (for watchdog use)

set -e
QUIET="${1:-}"

PRINTER=$(lsusb 2>/dev/null | grep -iE 'printer|hp|laser|canon|epson|brother' | head -1)
if [ -z "$PRINTER" ]; then
    [ "$QUIET" = "quiet" ] || echo "No USB printer found" >&2
    exit 1
fi

BUS=$(echo "$PRINTER" | grep -oP 'Bus \K\d+')
DEV=$(echo "$PRINTER" | grep -oP 'Device \K\d+')
DEVPATH="/dev/bus/usb/$BUS/$DEV"

rmmod usblp 2>/dev/null || true

python3 -c "
import fcntl
with open('$DEVPATH', 'wb') as f:
    fcntl.ioctl(f.fileno(), 21780)
" 2>/dev/null || {
    [ "$QUIET" = "quiet" ] || echo "USB reset ioctl failed on $DEVPATH" >&2
    exit 1
}

sleep 2
rmmod usblp 2>/dev/null || true
[ "$QUIET" = "quiet" ] || echo "USB port reset: $DEVPATH"
