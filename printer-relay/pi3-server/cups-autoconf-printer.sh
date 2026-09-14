#!/bin/bash
# Auto-configure the LaserPrinter CUPS queue for whatever USB printer is connected.
# Called by cups-autoconf-printer.service on boot and USB printer plug-in (udev).
#
# Design for the relay use case:
#   - The queue is ALWAYS raw pass-through (-m raw). The relay forwards the
#     programmer's native PCL/PostScript byte-for-byte; CUPS must not filter.
#   - Always use the direct usb:// backend — it's the simplest, most reliable
#     path for raw queues. ipp-usb adds a middleman that wedges under load
#     and buys nothing when CUPS isn't rendering.
#   - If ipp-usb is running (auto-started by its own udev rule for IPP-USB
#     printers), stop it so it releases the device for the usb:// backend.
#   - The decision is cached per printer serial so reboots are instant.

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

QUEUE=LaserPrinter
STATE_DIR=/var/lib/printer-autoconf
CACHE="$STATE_DIR/decision"
TAG=printer-autoconf

mkdir -p "$STATE_DIR"
log() { logger -t "$TAG" "$*"; }

# ── Stop ipp-usb if it's holding the device ─────────────────────────────────
release_ippusb() {
    if systemctl is-active --quiet ipp-usb 2>/dev/null; then
        log "stopping ipp-usb (not needed for raw pass-through)"
        systemctl stop ipp-usb 2>/dev/null
        sleep 2
    fi
    systemctl disable ipp-usb 2>/dev/null || true
    systemctl mask ipp-usb 2>/dev/null || true
    systemctl mask ipp-usb.socket 2>/dev/null || true
}

# ── Find the USB printer ────────────────────────────────────────────────────
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

bind_queue() {
    lpadmin -p "$QUEUE" -E -v "$1" -m raw
    cupsenable  "$QUEUE" 2>/dev/null || true
    cupsaccept  "$QUEUE" 2>/dev/null || true
    lpoptions -d "$QUEUE" >/dev/null 2>&1
}

# ── Main ─────────────────────────────────────────────────────────────────────

# Wait for the USB device to settle (udev fires before everything is enumerated).
sleep 2

release_ippusb

KEY=$(printer_key)
usb_info=$(lsusb 2>/dev/null | grep -iE "class 00|printer|03f0|0482" | head -1)
log "printer key: ${KEY:-none}"
[ -n "$usb_info" ] && log "lsusb: $usb_info"

# ── Cache: if the same printer, just ensure the queue is bound + enabled ─────
if [ -f "$CACHE" ] && [ -n "$KEY" ]; then
    C_KEY=$(sed -n '1p' "$CACHE")
    C_URI=$(sed -n '2p' "$CACHE")
    if [ "$KEY" = "$C_KEY" ] && [ -n "$C_URI" ]; then
        current=$(LANG=C lpstat -v "$QUEUE" 2>/dev/null | grep -oE 'usb://[^ ]+')
        if [ "$current" = "$C_URI" ]; then
            log "cached: queue already bound to $C_URI — ensuring enabled"
            cupsenable  "$QUEUE" 2>/dev/null || true
            cupsaccept  "$QUEUE" 2>/dev/null || true
            exit 0
        fi
        # Same printer but URI changed (maybe different USB port) — re-discover.
        log "cached key matches but URI stale ($C_URI vs $current) — re-discovering"
    fi
fi

# ── Discover and bind ────────────────────────────────────────────────────────
URI=$(find_usb_uri)
if [ -z "$URI" ]; then
    log "no USB printer found after 30s — giving up"
    exit 1
fi

log "configuring $QUEUE -> $URI (raw pass-through)"
bind_queue "$URI"

# Cache the decision so reboots are instant.
if [ -n "$KEY" ]; then
    printf '%s\n%s\n' "$KEY" "$URI" > "$CACHE"
fi

log "done: $QUEUE -> $URI"
