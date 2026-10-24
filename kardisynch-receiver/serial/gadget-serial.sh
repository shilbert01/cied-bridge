#!/bin/bash
#
# Bring up a USB CDC-ACM (serial port) gadget on the Pi Zero's OTG port.
#
# CDC-ACM is chosen because locked-down clinical PCs
# block USB mass-storage AND vendor-HID at the endpoint protection level, but
# allow serial/COM port devices — medical equipment commonly uses serial.
#
# The Pi appears as a COM port on the Windows host. A companion app on Windows
# opens that COM port and receives framed file data. On the Pi side the gadget
# exposes /dev/ttyGS0 for the sender script.
#
# Platform: Raspberry Pi OS Lite (usb_f_acm present). Requires dwc2 +
# libcomposite. Idempotent: re-running rebuilds the gadget.
set -euo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/kardisynch_serial"
SERIAL="KS$(cat /etc/machine-id 2>/dev/null | head -c 8 || hostname | md5sum | head -c 8)"

VID="0x1d6b"
PID="0x0106"

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (sudo $0)" >&2
    exit 1
fi

modprobe libcomposite 2>/dev/null || true

# ── teardown any previous instance (idempotent) ─────────────────────────────
if [ -d "$GADGET_DIR" ]; then
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    find "$GADGET_DIR/configs" -type l -delete 2>/dev/null || true
    find "$GADGET_DIR" -depth -type d -exec rmdir {} + 2>/dev/null || true
fi

# ── build the gadget ────────────────────────────────────────────────────────
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo "$VID"  > idVendor
echo "$PID"  > idProduct
echo 0x0100  > bcdDevice
echo 0x0200  > bcdUSB

mkdir -p strings/0x409
echo "$SERIAL"       > strings/0x409/serialnumber
echo "Konsilado"     > strings/0x409/manufacturer
echo "KardiSynch"    > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "Serial Transfer" > configs/c.1/strings/0x409/configuration
echo 250               > configs/c.1/MaxPower

mkdir -p functions/acm.usb0
ln -s functions/acm.usb0 configs/c.1/

# Bind to the (single) UDC.
udc=""
for _ in $(seq 1 30); do
    udc="$(ls /sys/class/udc 2>/dev/null | head -1)"
    [ -n "$udc" ] && break
    sleep 0.5
done
if [ -z "$udc" ]; then
    echo "no UDC after ~15s — dwc2 peripheral isn't active." >&2
    exit 1
fi
echo "$udc" > UDC

echo "Serial gadget active on /dev/ttyGS0 (udc=$udc, serial=$SERIAL)"
