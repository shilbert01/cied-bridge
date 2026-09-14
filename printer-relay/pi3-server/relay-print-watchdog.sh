#!/bin/bash
# relay-print-watchdog.sh — self-heal a wedged print queue + stale AP clients.
#
# With ipp-usb, the main failure mode is CUPS disabling the queue after a
# transient IPP error. Fix: re-enable the queue.
#
# Also flushes FAILED ARP entries on wlan0 so reconnecting edge nodes
# (ESP32, Pi Zero) aren't blocked by stale neighbour cache entries.
#
# Runs from a systemd timer every 30s.

set +e
QUEUE=LaserPrinter
STAMP=/run/relay-print-watchdog.last-heal
MIN_INTERVAL=120
PATH=/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

needs_heal=0
reason=""

# ── Flush stale ARP entries on the relay AP interface ────────────────────
# When an ESP32/Pi Zero reboots, stale neighbour + hostapd state can block
# reassociation. Flush FAILED/STALE/INCOMPLETE ARP and deauth MACs that
# hostapd still thinks are associated but aren't responding.
ip neigh flush dev wlan0 nud failed 2>/dev/null
ip neigh flush dev wlan0 nud stale 2>/dev/null
ip neigh flush dev wlan0 nud incomplete 2>/dev/null
hostapd_cli all_sta 2>/dev/null | awk '/^[0-9a-f]{2}:/{print $1}' | while read mac; do
    if ip neigh show dev wlan0 2>/dev/null | grep -q "$mac"; then
        :
    else
        hostapd_cli deauthenticate "$mac" 2>/dev/null
    fi
done

# ── Check: queue disabled ──────────────────────────────────────────────────
state=$(LANG=C lpstat -p "$QUEUE" 2>/dev/null | head -1)
if echo "$state" | grep -qi "disabled"; then
    needs_heal=1
    reason="queue disabled: $state"
fi

# ── Check: jobs stuck in "printing" for too long ──────────────────────────
if [ "$needs_heal" = "0" ]; then
    printing=$(LANG=C lpstat -o "$QUEUE" 2>/dev/null | head -1)
    if [ -n "$printing" ]; then
        job_id=$(echo "$printing" | awk '{print $1}')
        if [ -n "$job_id" ] && [ -f "/var/spool/cups/c${job_id##*-}" ]; then
            age=$(( $(date +%s) - $(stat -c %Y "/var/spool/cups/c${job_id##*-}" 2>/dev/null || echo $(date +%s)) ))
            if [ "$age" -gt 120 ]; then
                needs_heal=1
                reason="job $job_id stuck for ${age}s"
            fi
        fi
    fi
fi

# ── Check: ipp-usb not running ────────────────────────────────────────────
if ! systemctl is-active --quiet ipp-usb 2>/dev/null; then
    systemctl start ipp-usb 2>/dev/null || true
    logger -t relay-print-watchdog "ipp-usb was not running — restarted"
fi

[ "$needs_heal" = "0" ] && exit 0

# ── Rate limit ─────────────────────────────────────────────────────────────
now=$(date +%s)
last=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$MIN_INTERVAL" ]; then
    logger -t relay-print-watchdog "needs heal ($reason) but healed <${MIN_INTERVAL}s ago — waiting"
    exit 0
fi
echo "$now" > "$STAMP"

# ── Heal ────────────────────────────────────────────────────────────────────
logger -t relay-print-watchdog "$reason — healing: restart ipp-usb + re-enable queue"

systemctl restart ipp-usb 2>/dev/null || true
sleep 2

cupsenable "$QUEUE" 2>/dev/null
cupsaccept "$QUEUE" 2>/dev/null

if echo "$reason" | grep -q "stuck"; then
    cancel -a "$QUEUE" 2>/dev/null
    logger -t relay-print-watchdog "cancelled stuck jobs"
fi

logger -t relay-print-watchdog "healed: $(LANG=C lpstat -p "$QUEUE" 2>/dev/null | head -1)"
