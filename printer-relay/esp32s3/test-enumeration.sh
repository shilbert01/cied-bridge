#!/usr/bin/env bash
# Step-2 gate check: confirm the flashed S3 enumerates as the HP LaserJet clone,
# binds usblp, returns the cloned IEEE-1284 ID, and accepts a test print on
# bulk-OUT. Run this on the HOST the S3's printer port is plugged into.
#
# Expected identity (printer_descriptors.c): 03f0:152a "HP LaserJet 400 M401dn".
set -uo pipefail

VID_PID="03f0:152a"
EXPECT_MDL="HP LaserJet 400 M401dn"

echo "== 1. lsusb =="
if lsusb | grep -i "$VID_PID"; then
  echo "  OK: device present"
else
  echo "  FAIL: $VID_PID not found. Is the S3 printer port (native USB) plugged in?" >&2
  exit 1
fi

echo "== 2. usblp / lp node =="
LP=""
for n in /dev/usb/lp0 /dev/usb/lp1 /dev/usb/lp2; do
  [ -e "$n" ] && LP="$n" && break
done
if [ -n "$LP" ]; then
  echo "  OK: bound $LP"
else
  echo "  FAIL: no /dev/usb/lp* node (usblp didn't bind). dmesg | tail" >&2
  exit 1
fi

echo "== 3. IEEE-1284 device ID =="
SYSLP="/sys/class/usbmisc/$(basename "$LP")/device/ieee1284_id"
if [ -r "$SYSLP" ]; then
  ID="$(cat "$SYSLP")"
  echo "  $ID"
  echo "$ID" | grep -q "$EXPECT_MDL" && echo "  OK: MDL matches" || { echo "  FAIL: MDL mismatch" >&2; exit 1; }
else
  echo "  (ieee1284_id sysfs node not readable at $SYSLP — check manually)"
fi

echo "== 4. test print (watch the debug CDC log for 'job complete') =="
if [ -w "$LP" ]; then
  printf '\033%%-12345X@PJL\r\n@PJL ENTER LANGUAGE=PostScript\r\n%%!PS\nStep2 relay test\nshowpage\n\033%%-12345X' > "$LP" \
    && echo "  sent test job to $LP" || echo "  FAIL: write to $LP failed (need 'lp' group?)" >&2
else
  echo "  $LP not writable — add yourself to the 'lp' group or run as root, then:"
  echo "    printf 'Step2 test\\n' > $LP"
fi
echo "Done. Confirm the debug log printed 'job complete: N bytes'."
