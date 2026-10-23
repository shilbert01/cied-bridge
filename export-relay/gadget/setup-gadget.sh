#!/bin/sh
set -eu

CONF=/etc/cied-export/export.conf
[ -f "$CONF" ] || { echo "Missing $CONF"; exit 1; }
# shellcheck source=/dev/null
. "$CONF"

GADGET_DIR=/sys/kernel/config/usb_gadget/export_gadget

# Idempotent: tear down existing gadget if UDC is already bound
if [ -d "$GADGET_DIR" ]; then
    UDC_CURRENT=$(cat "$GADGET_DIR/UDC" 2>/dev/null || true)
    if [ -n "$UDC_CURRENT" ]; then
        echo "" > "$GADGET_DIR/UDC"
        sleep 0.5
    fi
    rm -f "$GADGET_DIR/configs/c.1/mass_storage.0"  2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409"   2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1"                  2>/dev/null || true
    rmdir "$GADGET_DIR/functions/mass_storage.0"     2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409"                2>/dev/null || true
    rmdir "$GADGET_DIR"                              2>/dev/null || true
fi

# Mount configfs if not already mounted
if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config
fi

mkdir -p "$GADGET_DIR"

# USB device descriptor — standard device class (mass storage only, no IAD needed)
echo "$GADGET_VID" > "$GADGET_DIR/idVendor"
echo "$GADGET_PID" > "$GADGET_DIR/idProduct"
echo "0x0200"      > "$GADGET_DIR/bcdUSB"
echo "0x0200"      > "$GADGET_DIR/bcdDevice"
echo "0x00"        > "$GADGET_DIR/bDeviceClass"
echo "0x00"        > "$GADGET_DIR/bDeviceSubClass"
echo "0x00"        > "$GADGET_DIR/bDeviceProtocol"

# English strings
mkdir -p "$GADGET_DIR/strings/0x409"
echo "$GADGET_SERIAL"       > "$GADGET_DIR/strings/0x409/serialnumber"
echo "$GADGET_MANUFACTURER" > "$GADGET_DIR/strings/0x409/manufacturer"
echo "$GADGET_PRODUCT"      > "$GADGET_DIR/strings/0x409/product"

# --- USB Mass Storage ---
if [ -z "${DISK_IMAGE:-}" ] || [ ! -f "$DISK_IMAGE" ]; then
    echo "DISK_IMAGE not set or file missing — run create-storage-image.sh first"
    exit 1
fi

mkdir -p "$GADGET_DIR/functions/mass_storage.0"
echo 0              > "$GADGET_DIR/functions/mass_storage.0/lun.0/cdrom"
echo 0              > "$GADGET_DIR/functions/mass_storage.0/lun.0/ro"
echo 1              > "$GADGET_DIR/functions/mass_storage.0/lun.0/removable"
echo "$DISK_IMAGE"  > "$GADGET_DIR/functions/mass_storage.0/lun.0/file"

# Neutral SCSI INQUIRY identity — avoids "Linux File-Stor Gadget" rejection by
# some embedded USB hosts (Abbott programmers etc.).
printf "%-8s%-16s%4s" "USB" "Flash Drive" "0100" \
    > "$GADGET_DIR/functions/mass_storage.0/lun.0/inquiry_string" 2>/dev/null || true

# --- Configuration descriptor ---
mkdir -p "$GADGET_DIR/configs/c.1/strings/0x409"
echo "Export Drive Config" > "$GADGET_DIR/configs/c.1/strings/0x409/configuration"
echo 250                   > "$GADGET_DIR/configs/c.1/MaxPower"

ln -s "$GADGET_DIR/functions/mass_storage.0" "$GADGET_DIR/configs/c.1/"

# --- Activate ---
UDC=$(ls /sys/class/udc | head -n1)
[ -n "$UDC" ] || { echo "No UDC found — is dtoverlay=dwc2 set in config.txt?"; exit 1; }
echo "$UDC" > "$GADGET_DIR/UDC"

echo "Gadget active on UDC: $UDC  image: $DISK_IMAGE"
