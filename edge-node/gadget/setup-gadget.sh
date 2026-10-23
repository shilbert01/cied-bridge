#!/bin/sh
set -eu

CONF=/etc/cied-edge/node.conf
[ -f "$CONF" ] || { echo "Missing $CONF"; exit 1; }
# shellcheck source=/dev/null
. "$CONF"

GADGET_DIR=/sys/kernel/config/usb_gadget/printer_gadget

# Idempotent: tear down existing gadget if UDC is already bound
if [ -d "$GADGET_DIR" ]; then
    UDC_CURRENT=$(cat "$GADGET_DIR/UDC" 2>/dev/null || true)
    if [ -n "$UDC_CURRENT" ]; then
        echo "" > "$GADGET_DIR/UDC"
        sleep 0.5
    fi
    rm -f "$GADGET_DIR/configs/c.1/printer.usb0"    2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/mass_storage.0"  2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409"   2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1"                  2>/dev/null || true
    rmdir "$GADGET_DIR/functions/printer.usb0"       2>/dev/null || true
    rmdir "$GADGET_DIR/functions/mass_storage.0"     2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409"                2>/dev/null || true
    rmdir "$GADGET_DIR"                              2>/dev/null || true
fi

# Mount configfs if not already mounted
if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config
fi

mkdir -p "$GADGET_DIR"

# USB device descriptor
echo "$GADGET_VID" > "$GADGET_DIR/idVendor"
echo "$GADGET_PID" > "$GADGET_DIR/idProduct"
echo "0x0200"      > "$GADGET_DIR/bcdUSB"
echo "0x0200"      > "$GADGET_DIR/bcdDevice"

# Composite device — class/subclass/protocol deferred to interfaces
echo "0xEF" > "$GADGET_DIR/bDeviceClass"       # Miscellaneous
echo "0x02" > "$GADGET_DIR/bDeviceSubClass"     # Common Class
echo "0x01" > "$GADGET_DIR/bDeviceProtocol"     # IAD (Interface Association Descriptor)

# English strings
mkdir -p "$GADGET_DIR/strings/0x409"
echo "$GADGET_SERIAL"       > "$GADGET_DIR/strings/0x409/serialnumber"
echo "$GADGET_MANUFACTURER" > "$GADGET_DIR/strings/0x409/manufacturer"
echo "$GADGET_PRODUCT"      > "$GADGET_DIR/strings/0x409/product"

# --- Function 1: USB Printer ---
mkdir -p "$GADGET_DIR/functions/printer.usb0"
echo "$PNP_STRING" > "$GADGET_DIR/functions/printer.usb0/pnp_string"
echo 10            > "$GADGET_DIR/functions/printer.usb0/q_len"

# --- Function 2: USB Mass Storage ---
STORAGE_ENABLED=0
if [ -n "${STORAGE_IMG:-}" ] && [ -f "$STORAGE_IMG" ]; then
    mkdir -p "$GADGET_DIR/functions/mass_storage.0"
    echo 0               > "$GADGET_DIR/functions/mass_storage.0/lun.0/cdrom"
    echo 0               > "$GADGET_DIR/functions/mass_storage.0/lun.0/ro"
    echo 1               > "$GADGET_DIR/functions/mass_storage.0/lun.0/removable"
    echo "$STORAGE_IMG"  > "$GADGET_DIR/functions/mass_storage.0/lun.0/file"
    # SCSI INQUIRY vendor/product — avoid "Linux File-Stor Gadget" which some
    # embedded USB hosts (Abbott programmers) reject; use a neutral drive identity.
    printf "%-8s%-16s%4s" "USB" "Flash Drive" "0100" \
        > "$GADGET_DIR/functions/mass_storage.0/lun.0/inquiry_string" 2>/dev/null || true
    STORAGE_ENABLED=1
else
    echo "STORAGE_IMG not set or file missing — mass storage function skipped"
fi

# --- Configuration descriptor ---
mkdir -p "$GADGET_DIR/configs/c.1/strings/0x409"
echo "Printer+Storage Config" > "$GADGET_DIR/configs/c.1/strings/0x409/configuration"
echo 250                      > "$GADGET_DIR/configs/c.1/MaxPower"

# Bind printer function
ln -s "$GADGET_DIR/functions/printer.usb0" "$GADGET_DIR/configs/c.1/"

# Bind mass storage function (if enabled)
if [ "$STORAGE_ENABLED" -eq 1 ]; then
    ln -s "$GADGET_DIR/functions/mass_storage.0" "$GADGET_DIR/configs/c.1/"
fi

# --- Activate ---
UDC=$(ls /sys/class/udc | head -n1)
[ -n "$UDC" ] || { echo "No UDC found — is dtoverlay=dwc2 set in config.txt?"; exit 1; }
echo "$UDC" > "$GADGET_DIR/UDC"

echo "Gadget active on UDC: $UDC  printer: ${GADGET_DEVICE}  storage: $STORAGE_ENABLED"
