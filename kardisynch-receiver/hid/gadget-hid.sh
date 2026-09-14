#!/bin/bash
#
# Bring up a vendor-defined USB HID gadget on the Pi Zero's OTG port.
#
# HID is chosen because locked-down clinical PCs (e.g. Herzzentrum Leipzig) block
# USB mass-storage and USB-NIC at the endpoint but must allow HID (keyboards/mice
# always work). We abuse a generic 64-byte HID pipe as a one-way file transport;
# a small Windows companion app reassembles the reports (see ../windows/).
#
# Report descriptor: a single vendor-usage-page application collection with
#   - a 64-byte INPUT report  (device -> host): file data stream
#   - a 64-byte OUTPUT report (host -> device): the companion app's ACK
# No HID Report ID is declared, so /dev/hidg0 exchanges exactly 64-byte reports:
# write() sends an input report, read() returns the host's output (ACK) report.
#
# Platform: Raspberry Pi OS Lite (usb_f_hid present). Requires setup-otg.sh +
# reboot first (dwc2 + libcomposite). Idempotent: re-running rebuilds the gadget.
set -euo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/kardisynch_hid"
SERIAL="KS$(cat /etc/machine-id 2>/dev/null | head -c 8 || hostname | md5sum | head -c 8)"

# VID/PID kept identical to the companion app's filter (../windows/.../Program.cs).
# 0x1d6b = Linux Foundation; 0x0104 = its composite-gadget example PID.
VID="0x1d6b"
PID="0x0104"

# Vendor-defined usage page 0xFF00, usage 0x01; 64-byte IN (usage 0x20) + OUT (0x21).
# IMPORTANT: must be a vendor page (0xFF00-0xFFFF). The original 0xF1D0 is the FIDO
# usage page, which browsers' WebHID BLOCK — the companion web page would never see
# the device. 0xFF00 is unblocked. (A native HID app doesn't care, but WebHID does.)
REPORT_DESC_HEX="0600ff0901a101150026ff00750895400920810209219102c0"

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
echo "$SERIAL"    > strings/0x409/serialnumber
echo "Konsilado"  > strings/0x409/manufacturer
echo "KardiSynch" > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "HID Transfer" > configs/c.1/strings/0x409/configuration
echo 250            > configs/c.1/MaxPower

mkdir -p functions/hid.usb0
echo 0  > functions/hid.usb0/protocol      # 0 = none (vendor pipe, not boot kbd/mouse)
echo 0  > functions/hid.usb0/subclass
echo 64 > functions/hid.usb0/report_length
# hex -> binary report descriptor. Use python3 (always in RPi OS Lite); fall back
# to xxd if present. Avoids a vim-common dependency on minimal images.
if command -v xxd >/dev/null 2>&1; then
    echo "$REPORT_DESC_HEX" | xxd -r -p > functions/hid.usb0/report_desc
else
    python3 -c "import sys,binascii; sys.stdout.buffer.write(binascii.unhexlify('$REPORT_DESC_HEX'))" \
        > functions/hid.usb0/report_desc
fi

ln -s functions/hid.usb0 configs/c.1/

# Bind to the (single) UDC. dwc2 registers it a few seconds into boot, so wait
# rather than racing the driver on an early service start.
udc=""
for _ in $(seq 1 30); do
    udc="$(ls /sys/class/udc 2>/dev/null | head -1)"
    [ -n "$udc" ] && break
    sleep 0.5
done
if [ -z "$udc" ]; then
    echo "no UDC after ~15s — dwc2 peripheral isn't active." >&2
    echo "check: 'dtoverlay=dwc2,dr_mode=peripheral' under [all] in config.txt," >&2
    echo "       otg_mode not forcing dwc_otg, and 'dtoverlay -l' shows it loaded." >&2
    exit 1
fi
echo "$udc" > UDC

echo "HID gadget active on /dev/hidg0 (udc=$udc, serial=$SERIAL)"
