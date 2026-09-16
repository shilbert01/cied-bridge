#!/bin/bash
# Auto-configure the LaserPrinter CUPS queue for whatever USB printer is connected.
# Called by cups-autoconf-printer.service on boot and USB printer plug-in (udev).
#
# Transport selection:
#   - If the printer supports IPP-over-USB (protocol 4) → ipp-usb + everywhere
#   - If only protocols 1/2/3                           → usblp + raw pass-through
#   - The decision is cached per printer serial so reboots are instant.

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

QUEUE=LaserPrinter
STATE_DIR=/var/lib/printer-autoconf
CACHE="$STATE_DIR/decision"
TAG=printer-autoconf
IPP_URI="ipp://localhost:60000/ipp/print"

mkdir -p "$STATE_DIR"
log() { logger -t "$TAG" "$*"; }

# ── Check if printer supports IPP-over-USB (protocol 4) ─────────────────────
has_proto4() {
    local d i proto
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] || continue
        for i in "$d":*; do
            [ -f "$i/bInterfaceClass" ] || continue
            if [ "$(cat "$i/bInterfaceClass" 2>/dev/null)" = "07" ]; then
                proto=$(cat "$i/bInterfaceProtocol" 2>/dev/null)
                [ "$proto" = "04" ] && return 0
            fi
        done
    done
    return 1
}

# ── Switch to ipp-usb transport ──────────────────────────────────────────────
setup_ippusb() {
    log "printer supports IPP-over-USB — using ipp-usb transport"
    systemctl unmask ipp-usb ipp-usb.socket 2>/dev/null || true
    rmmod usblp 2>/dev/null || true
    systemctl restart ipp-usb 2>/dev/null || true
    # Wait for ipp-usb to start listening
    local i
    for i in $(seq 1 10); do
        curl -s -o /dev/null http://localhost:60000/ipp/print 2>/dev/null && break
        sleep 1
    done
    lpadmin -p "$QUEUE" -E -v "$IPP_URI" -m everywhere
    cupsenable  "$QUEUE" 2>/dev/null || true
    cupsaccept  "$QUEUE" 2>/dev/null || true
    lpoptions -d "$QUEUE" >/dev/null 2>&1
}

# ── Switch to usblp transport ────────────────────────────────────────────────
setup_usblp() {
    log "printer uses protocols 1/2/3 — using usblp transport"
    if systemctl is-active --quiet ipp-usb 2>/dev/null; then
        systemctl stop ipp-usb 2>/dev/null
        sleep 2
    fi
    local uri
    uri=$(find_usb_uri)
    if [ -z "$uri" ]; then
        log "no USB printer URI found — giving up"
        return 1
    fi
    lpadmin -p "$QUEUE" -E -v "$uri" -m raw
    cupsenable  "$QUEUE" 2>/dev/null || true
    cupsaccept  "$QUEUE" 2>/dev/null || true
    lpoptions -d "$QUEUE" >/dev/null 2>&1
}

# ── Find the USB printer URI ────────────────────────────────────────────────
find_usb_uri() {
    local i uri
    for i in $(seq 1 15); do
        uri=$(lpinfo -v 2>/dev/null | awk '/^direct usb:\/\// { print $2; exit }')
        [ -n "$uri" ] && { echo "$uri"; return 0; }
        sleep 2
    done
    return 1
}

# ── Printer identity for cache keying ────────────────────────────────────────
printer_key() {
    local d i
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] || continue
        for i in "$d":*; do
            [ -f "$i/bInterfaceClass" ] || continue
            if [ "$(cat "$i/bInterfaceClass" 2>/dev/null)" = "07" ]; then
                echo "$(cat "$d/idVendor" 2>/dev/null):$(cat "$d/idProduct" 2>/dev/null):$(cat "$d/serial" 2>/dev/null)"
                return
            fi
        done
    done
}

# ── Main ─────────────────────────────────────────────────────────────────────

# Wait for the USB device to settle (udev fires before everything is enumerated).
sleep 2

KEY=$(printer_key)
usb_info=$(lsusb 2>/dev/null | grep -iE "class 00|printer|03f0|0482" | head -1)
log "printer key: ${KEY:-none}"
[ -n "$usb_info" ] && log "lsusb: $usb_info"

# Determine transport based on USB interface protocols.
if has_proto4; then
    TRANSPORT=ippusb
else
    TRANSPORT=usblp
fi
log "selected transport: $TRANSPORT"

# ── Cache: if same printer + same transport, just ensure enabled ─────────────
if [ -f "$CACHE" ] && [ -n "$KEY" ]; then
    C_KEY=$(sed -n '1p' "$CACHE")
    C_URI=$(sed -n '2p' "$CACHE")
    C_TRANSPORT=$(sed -n '3p' "$CACHE")
    if [ "$KEY" = "$C_KEY" ] && [ "$TRANSPORT" = "$C_TRANSPORT" ] && [ -n "$C_URI" ]; then
        current=$(LANG=C lpstat -v "$QUEUE" 2>/dev/null | awk '{print $NF}')
        if [ "$current" = "$C_URI" ]; then
            log "cached: queue already bound to $C_URI ($C_TRANSPORT) — ensuring enabled"
            if [ "$TRANSPORT" = "ippusb" ]; then
                systemctl unmask ipp-usb ipp-usb.socket 2>/dev/null || true
                systemctl is-active --quiet ipp-usb 2>/dev/null || systemctl start ipp-usb 2>/dev/null
            fi
            cupsenable  "$QUEUE" 2>/dev/null || true
            cupsaccept  "$QUEUE" 2>/dev/null || true
            exit 0
        fi
        log "cached key matches but URI stale ($C_URI vs $current) — re-discovering"
    fi
fi

# ── Configure ────────────────────────────────────────────────────────────────
if [ "$TRANSPORT" = "ippusb" ]; then
    setup_ippusb
    FINAL_URI="$IPP_URI"
else
    setup_usblp
    FINAL_URI=$(LANG=C lpstat -v "$QUEUE" 2>/dev/null | awk '{print $NF}')
fi

# Cache the decision.
if [ -n "$KEY" ] && [ -n "$FINAL_URI" ]; then
    printf '%s\n%s\n%s\n' "$KEY" "$FINAL_URI" "$TRANSPORT" > "$CACHE"
fi

log "done: $QUEUE -> $FINAL_URI ($TRANSPORT)"
