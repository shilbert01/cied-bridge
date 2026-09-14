#!/bin/bash
# CUPS USB backend wrapper — transparent retry on endpoint stall or hang.
#
# The HP MFP 3302 stalls its USB endpoint after printing. Sometimes the
# backend returns an error (caught by rc check), sometimes it hangs
# indefinitely (caught by timeout). This wrapper handles both.
#
# Install: mv /usr/lib/cups/backend/usb → usb.real
#          install this as /usr/lib/cups/backend/usb (mode 700)

REAL=/usr/lib/cups/backend/usb.real
MAX_TRIES=3
TIMEOUT=30

# Discovery mode — pass through
if [ $# -eq 0 ]; then
    exec "$REAL"
fi

# Buffer stdin if no file argument (so we can retry)
FILE="${6:-}"
TMPFILE=""

if [ -z "$FILE" ]; then
    TMPFILE=$(mktemp /tmp/cups-usb-retry.XXXXXX)
    cat > "$TMPFILE"
    set -- "$1" "$2" "$3" "$4" "$5" "$TMPFILE"
    FILE="$TMPFILE"
fi

cleanup() { [ -n "$TMPFILE" ] && rm -f "$TMPFILE"; }
trap cleanup EXIT

usb_port_reset() {
    for d in /sys/bus/usb/devices/1-1.*; do
        [ -f "$d/idVendor" ] && grep -q 03f0 "$d/idVendor" 2>/dev/null && {
            echo 0 > "$d/authorized" 2>/dev/null
            sleep 2
            echo 1 > "$d/authorized" 2>/dev/null
            sleep 4
            break
        }
    done
    rmmod usblp 2>/dev/null || true
}

try=1
while [ "$try" -le "$MAX_TRIES" ]; do
    timeout "$TIMEOUT" "$REAL" "$@"
    rc=$?

    # 0 = success
    [ $rc -eq 0 ] && exit 0

    # 124 = timeout killed it; anything else = backend error
    if [ $rc -eq 124 ]; then
        logger -t usb-wrapper "attempt $try/$MAX_TRIES timed out after ${TIMEOUT}s — resetting USB"
    else
        logger -t usb-wrapper "attempt $try/$MAX_TRIES failed (rc=$rc) — resetting USB"
    fi

    usb_port_reset
    try=$((try + 1))
done

logger -t usb-wrapper "all $MAX_TRIES attempts failed"
exit 1
