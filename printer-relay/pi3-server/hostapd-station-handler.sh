#!/bin/bash
# Called by hostapd_cli -a for each station event on the relay AP.
# Flushes stale ARP/neighbor entries so that a reconnecting ESP32 (or Pi Zero)
# gets a clean layer-3 path immediately — no manual "flush WiFi" needed.
#
# Arguments (set by hostapd_cli -a):
#   $1 = interface (e.g. wlan0)
#   $2 = event     (AP-STA-CONNECTED | AP-STA-DISCONNECTED | ...)
#   $3 = MAC       (e.g. aa:bb:cc:dd:ee:ff)

IFACE="$1"
EVENT="$2"
MAC="$3"
TAG="hostapd-handler"

[ -z "$MAC" ] && exit 0

flush_arp_for_mac() {
    local ip
    ip=$(ip neigh show dev "$IFACE" 2>/dev/null \
         | grep -i "$MAC" | awk '{print $1}' | head -1)
    [ -z "$ip" ] && return
    ip neigh flush dev "$IFACE" to "$ip" 2>/dev/null
    logger -t "$TAG" "$EVENT $MAC — flushed ARP for $ip on $IFACE"
}

case "$EVENT" in
    AP-STA-DISCONNECTED)
        flush_arp_for_mac
        ;;
    AP-STA-CONNECTED)
        flush_arp_for_mac
        ;;
esac
