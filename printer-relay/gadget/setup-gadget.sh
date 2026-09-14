#!/bin/sh
set -eu

RELAY_CONF=/etc/pacemaker-relay/relay.conf
EXPORT_CONF=/etc/pacemaker-export/export.conf

[ -f "$RELAY_CONF" ] || { echo "Missing $RELAY_CONF"; exit 1; }
# shellcheck source=/dev/null
. "$RELAY_CONF"

GADGET_DIR=/sys/kernel/config/usb_gadget/relay_gadget

# Idempotent: tear down existing gadget if UDC is already bound
if [ -d "$GADGET_DIR" ]; then
    UDC_CURRENT=$(cat "$GADGET_DIR/UDC" 2>/dev/null || true)
    if [ -n "$UDC_CURRENT" ]; then
        echo "" > "$GADGET_DIR/UDC"
        sleep 0.5
    fi
    rm -f "$GADGET_DIR/configs/c.1/printer.usb0"     2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/mass_storage.0"   2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409"    2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1"                   2>/dev/null || true
    rmdir "$GADGET_DIR/functions/printer.usb0"       2>/dev/null || true
    rmdir "$GADGET_DIR/functions/mass_storage.0"     2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409"                2>/dev/null || true
    rmdir "$GADGET_DIR"                              2>/dev/null || true
fi

if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config
fi

mkdir -p "$GADGET_DIR"

# Composite device: IAD (Interface Association Descriptor) so the host
# enumerates both the printer and mass storage interfaces correctly.
echo "$GADGET_VID" > "$GADGET_DIR/idVendor"
echo "$GADGET_PID" > "$GADGET_DIR/idProduct"
echo "0x0200"      > "$GADGET_DIR/bcdUSB"
echo "0x0100"      > "$GADGET_DIR/bcdDevice"
echo "0xEF"        > "$GADGET_DIR/bDeviceClass"
echo "0x02"        > "$GADGET_DIR/bDeviceSubClass"
echo "0x01"        > "$GADGET_DIR/bDeviceProtocol"

mkdir -p "$GADGET_DIR/strings/0x409"
echo "$GADGET_SERIAL"       > "$GADGET_DIR/strings/0x409/serialnumber"
echo "$GADGET_MANUFACTURER" > "$GADGET_DIR/strings/0x409/manufacturer"
echo "$GADGET_PRODUCT"      > "$GADGET_DIR/strings/0x409/product"

# --- Function 1: USB Printer ---
mkdir -p "$GADGET_DIR/functions/printer.usb0"
echo "$PNP_STRING" > "$GADGET_DIR/functions/printer.usb0/pnp_string"
echo 10            > "$GADGET_DIR/functions/printer.usb0/q_len"

# --- Function 2: USB Mass Storage (optional — only if export.conf exists) ---
HAS_STORAGE=0
if [ -f "$EXPORT_CONF" ]; then
    # shellcheck source=/dev/null
    . "$EXPORT_CONF"
    if [ -n "${DISK_IMAGE:-}" ] && [ -f "$DISK_IMAGE" ]; then
        mkdir -p "$GADGET_DIR/functions/mass_storage.0"
        echo 0              > "$GADGET_DIR/functions/mass_storage.0/lun.0/cdrom"
        echo 0              > "$GADGET_DIR/functions/mass_storage.0/lun.0/ro"
        echo 1              > "$GADGET_DIR/functions/mass_storage.0/lun.0/removable"
        echo 1              > "$GADGET_DIR/functions/mass_storage.0/lun.0/nofua" 2>/dev/null || true
        echo "$DISK_IMAGE"  > "$GADGET_DIR/functions/mass_storage.0/lun.0/file"
        printf "%-8s%-16s%4s" "USB" "Flash Drive" "0100" \
            > "$GADGET_DIR/functions/mass_storage.0/lun.0/inquiry_string" 2>/dev/null || true
        HAS_STORAGE=1
    fi
fi

# --- Configuration descriptor ---
mkdir -p "$GADGET_DIR/configs/c.1/strings/0x409"
echo "Relay Config" > "$GADGET_DIR/configs/c.1/strings/0x409/configuration"
echo 250            > "$GADGET_DIR/configs/c.1/MaxPower"

ln -s "$GADGET_DIR/functions/printer.usb0" "$GADGET_DIR/configs/c.1/"
[ "$HAS_STORAGE" = "1" ] && \
    ln -s "$GADGET_DIR/functions/mass_storage.0" "$GADGET_DIR/configs/c.1/"

# --- Activate ---
UDC=$(ls /sys/class/udc | head -n1)
[ -n "$UDC" ] || { echo "No UDC found — is dtoverlay=dwc2 set in config.txt?"; exit 1; }
echo "$UDC" > "$GADGET_DIR/UDC"

if [ "$HAS_STORAGE" = "1" ]; then
    echo "Gadget active on UDC: $UDC  printer: ${GADGET_DEVICE}  storage: ${DISK_IMAGE}"
else
    echo "Gadget active on UDC: $UDC  printer: ${GADGET_DEVICE}  (no storage)"
fi
