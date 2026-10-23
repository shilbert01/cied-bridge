#!/bin/sh
# Brings up the USB mass-storage gadget the HOST computer reads from.
#
# Same configfs plumbing as export-relay, with two import-specific details:
#   * nofua=1  — hint the host not to use Force-Unit-Access, so reads come from
#                the host's cache normally; we control freshness via media-change.
#   * The daemon (import_daemon.py) does the eject/inject/reattach cycle at
#     runtime by toggling functions/mass_storage.0/lun.0/file. This script only
#     brings the gadget up with the image already attached.
set -eu

CONF=/etc/cied-import/import.conf
[ -f "$CONF" ] || { echo "Missing $CONF"; exit 1; }
# shellcheck source=/dev/null
. "$CONF"

GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME:-import_gadget}"

# Idempotent teardown if already present
if [ -d "$GADGET_DIR" ]; then
    UDC_CURRENT=$(cat "$GADGET_DIR/UDC" 2>/dev/null || true)
    if [ -n "$UDC_CURRENT" ]; then
        echo "" > "$GADGET_DIR/UDC"
        sleep 0.5
    fi
    rm -f "$GADGET_DIR/configs/c.1/mass_storage.0" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409"  2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1"                 2>/dev/null || true
    rmdir "$GADGET_DIR/functions/mass_storage.0"    2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409"               2>/dev/null || true
    rmdir "$GADGET_DIR"                             2>/dev/null || true
fi

if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config
fi

mkdir -p "$GADGET_DIR"

echo "$GADGET_VID" > "$GADGET_DIR/idVendor"
echo "$GADGET_PID" > "$GADGET_DIR/idProduct"
echo "0x0200"      > "$GADGET_DIR/bcdUSB"
echo "0x0200"      > "$GADGET_DIR/bcdDevice"
echo "0x00"        > "$GADGET_DIR/bDeviceClass"
echo "0x00"        > "$GADGET_DIR/bDeviceSubClass"
echo "0x00"        > "$GADGET_DIR/bDeviceProtocol"

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
echo 1              > "$GADGET_DIR/functions/mass_storage.0/lun.0/nofua" 2>/dev/null || true
echo "$DISK_IMAGE"  > "$GADGET_DIR/functions/mass_storage.0/lun.0/file"

# Neutral SCSI INQUIRY identity so embedded hosts don't reject the gadget.
printf "%-8s%-16s%4s" "USB" "Flash Drive" "0100" \
    > "$GADGET_DIR/functions/mass_storage.0/lun.0/inquiry_string" 2>/dev/null || true

mkdir -p "$GADGET_DIR/configs/c.1/strings/0x409"
echo "Import Drive Config" > "$GADGET_DIR/configs/c.1/strings/0x409/configuration"
echo 250                   > "$GADGET_DIR/configs/c.1/MaxPower"

ln -s "$GADGET_DIR/functions/mass_storage.0" "$GADGET_DIR/configs/c.1/"

UDC=$(ls /sys/class/udc | head -n1)
[ -n "$UDC" ] || { echo "No UDC found — is dtoverlay=dwc2 set in config.txt?"; exit 1; }
echo "$UDC" > "$GADGET_DIR/UDC"

echo "Gadget active on UDC: $UDC  image: $DISK_IMAGE"
