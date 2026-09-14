#!/bin/bash
# fix-ac56-wifi.sh
#
# Replace the flaky in-kernel rtw88_8812au driver with the stable out-of-tree
# morrownr 8812au driver (via DKMS), for the ASUS USB-AC56 / RTL8812AU USB WiFi
# adapter used as the relay-subnet (relayap / CIEDPrintserver) leg on the laptop.
#
# SYMPTOM THIS FIXES
#   The relay-subnet WiFi leg "constantly connects and disconnects". dmesg shows:
#       rtw88_8812au ...: failed to download firmware
#       rtw88_8812au ...: failed to validate firmware
#       usb 4-4: USB disconnect, device number NN   (number climbing every few s)
#   i.e. the adapter re-enumerates on USB endlessly (dozens of drops per minute).
#   NOT a signal/AP/power problem: signal was -28 dBm, USB 3.0 port, firmware file
#   present (/lib/firmware/rtw88/rtw8812a_fw.bin.zst). The in-kernel rtw88_8812au
#   driver is simply immature/unstable for this chip; the out-of-tree driver is
#   the long-standing known-good one.
#
# USAGE
#   sudo bash fix-ac56-wifi.sh
#   (needs a real terminal — the driver installer asks a couple of y/n questions;
#    sudo also needs the password prompt, so don't pipe this over a headless ssh.)
#
# UNDO
#   sudo rm /etc/modprobe.d/blacklist-rtw88-8812au.conf
#   sudo ./8812au-20210820/remove-driver.sh    # from the build dir below
#   sudo reboot

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0"; exit 1; }

BUILD_DIR="/opt/8812au-20210820"
REPO="https://github.com/morrownr/8812au-20210820"

echo ">> [1/4] installing build prerequisites"
apt-get update -qq
apt-get install -y dkms git build-essential "linux-headers-$(uname -r)"

echo ">> [2/4] fetching the driver into $BUILD_DIR"
if [ -d "$BUILD_DIR/.git" ]; then
    git -C "$BUILD_DIR" pull --ff-only || true
else
    rm -rf "$BUILD_DIR"
    git clone --depth 1 "$REPO" "$BUILD_DIR"
fi
cd "$BUILD_DIR"

echo ">> [3/4] blacklisting the flaky in-kernel driver so it can't grab the device"
cat > /etc/modprobe.d/blacklist-rtw88-8812au.conf <<'EOF'
# The in-kernel rtw88_8812au fails to download firmware to the ASUS USB-AC56
# (RTL8812AU) and makes it re-enumerate on USB endlessly. Use the out-of-tree
# 8812au (DKMS) driver instead. See tools/fix-ac56-wifi.sh.
blacklist rtw88_8812au
EOF
modprobe -r rtw88_8812au 2>/dev/null || true

echo ">> [4/4] building + installing the out-of-tree driver (DKMS)"
# install-driver.sh handles DKMS registration, per-kernel rebuilds, and (if
# Secure Boot is on) MOK signing. It will ask to edit options / reboot — answer y.
./install-driver.sh

echo
echo "=== done ==="
echo "If it didn't reboot, do it now:   sudo reboot"
echo "After reboot verify the RIGHT driver bound:"
echo "   lsmod | grep 8812au            # want '8812au', NOT 'rtw88_8812au'"
echo "   dmesg | grep -i 8812           # no 'failed to download firmware'"
echo "   nmcli dev status               # the stick's connection should stay up"
