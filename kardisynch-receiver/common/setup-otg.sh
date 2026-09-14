#!/bin/bash
#
# One-time host prep for USB-gadget mode on a Raspberry Pi Zero W.
# Enables the dwc2 OTG controller + libcomposite (configfs gadgets). Idempotent:
# safe to re-run; only rewrites a line if it's actually missing.
#
# Platform: Raspberry Pi OS Lite (Bookworm/Bullseye). A reboot is required after
# the first run for dwc2 to bind the OTG port.
#
# The Buildroot fleet nodes already have dwc2 set for the printer/export/import
# relays — this script only matters on the RPi-OS receiver (see plan: Buildroot
# port is deferred until HID graduates).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (sudo $0)" >&2
    exit 1
fi

# Raspberry Pi OS moved /boot -> /boot/firmware in Bookworm.
if [ -d /boot/firmware ]; then
    BOOT=/boot/firmware
else
    BOOT=/boot
fi
CONFIG_TXT="$BOOT/config.txt"
CMDLINE_TXT="$BOOT/cmdline.txt"

changed=0

# 1. dwc2 overlay in config.txt, in an [all]-scope so it applies to THIS board.
# CRITICAL: Raspberry Pi OS ships dwc2 lines under [cm4]/[cm5]/[pi5] SECTION
# FILTERS that do NOT apply to a Pi Zero — a bare `grep dtoverlay=dwc2` would see
# one and wrongly conclude it's configured. So we append our own under [all],
# guarded by a marker for idempotency. Peripheral mode is required for a gadget
# (a bare dwc2 defaults to OTG and can come up host with no UDC).
MARKER="# kardisynch-receiver: USB gadget mode"
if grep -qF "$MARKER" "$CONFIG_TXT"; then
    echo "gadget overlay already present in $CONFIG_TXT"
elif grep -qE '^\[' "$CONFIG_TXT"; then
    # Insert into the UNCONDITIONAL top block, before the first [section] filter.
    # Pi Zero W firmware was observed NOT to honour a trailing [all] block, so an
    # overlay appended after [cm4]/[cm5]/[pi5] is silently skipped — it must live
    # in the top region (where e.g. vc4-kms-v3d loads).
    sed -i "0,/^\[/s//${MARKER}\ndtoverlay=dwc2,dr_mode=peripheral\n\n&/" "$CONFIG_TXT"
    echo "inserted dwc2 overlay in the top (unconditional) block of $CONFIG_TXT"
    changed=1
else
    printf '\n%s\ndtoverlay=dwc2,dr_mode=peripheral\n' "$MARKER" >> "$CONFIG_TXT"
    echo "added dwc2 overlay to $CONFIG_TXT"
    changed=1
fi

# 1b. `otg_mode=1` (a Pi4/CM4 setting) makes the firmware hand the controller to
# the legacy dwc_otg HOST driver. Disable it wherever it's active (harmless if
# it's already commented or scoped to a section that doesn't apply here).
if grep -qE '^\s*otg_mode=1' "$CONFIG_TXT"; then
    sed -i 's|^\s*otg_mode=1|#otg_mode=1  # disabled: conflicts with dwc2 peripheral gadget|' "$CONFIG_TXT"
    echo "disabled otg_mode=1 in $CONFIG_TXT"
    changed=1
fi

# 2. modules-load=dwc2 in cmdline.txt (single line; append after rootwait)
if ! grep -qE '\bmodules-load=dwc2\b' "$CMDLINE_TXT"; then
    # cmdline.txt is a single space-separated line — never add a newline.
    sed -i 's/\brootwait\b/rootwait modules-load=dwc2/' "$CMDLINE_TXT"
    if ! grep -qE '\bmodules-load=dwc2\b' "$CMDLINE_TXT"; then
        # no 'rootwait' token to anchor on — append to the end of the line
        sed -i 's/[[:space:]]*$/ modules-load=dwc2/' "$CMDLINE_TXT"
    fi
    echo "added modules-load=dwc2 to $CMDLINE_TXT"
    changed=1
else
    echo "modules-load=dwc2 already in $CMDLINE_TXT"
fi

# 3. libcomposite in /etc/modules (loaded at boot; gadget scripts need it)
if ! grep -qE '^\s*libcomposite\s*$' /etc/modules; then
    echo "libcomposite" >> /etc/modules
    echo "added libcomposite to /etc/modules"
    changed=1
else
    echo "libcomposite already in /etc/modules"
fi

if [ "$changed" -eq 1 ]; then
    echo
    echo ">>> Reboot required for dwc2 to bind the OTG port:  sudo reboot"
else
    echo
    echo "OTG already configured. If /sys/class/udc is empty, reboot once."
fi
