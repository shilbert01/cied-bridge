#!/bin/bash
# station-health-monitor.sh — actively pings WiFi stations, deauths dead ones.
#
# When an ESP32 relay is unplugged from one programmer and moved to another,
# hostapd may hold the stale session for up to ap_max_inactivity seconds
# (default 300s). During that time the ESP's reconnect attempt can fail.
#
# This script pings each connected station every INTERVAL seconds. After
# FAIL_THRESHOLD consecutive failures it deauthenticates the station,
# clearing the stale session so the ESP can reconnect immediately.

set +e

IFACE=wlan0
FAIL_THRESHOLD=3
INTERVAL=2
TAG=station-health

declare -A fail_count

while true; do
    mapfile -t stations < <(hostapd_cli -i "$IFACE" list_sta 2>/dev/null)

    for mac in "${stations[@]}"; do
        [ -z "$mac" ] && continue

        ip=$(ip neigh show dev "$IFACE" 2>/dev/null \
             | grep -i "$mac" | awk '{print $1}' | head -1)
        [ -z "$ip" ] && continue

        if ping -c1 -W1 -I "$IFACE" "$ip" >/dev/null 2>&1; then
            fail_count[$mac]=0
        else
            fails=$(( ${fail_count[$mac]:-0} + 1 ))
            fail_count[$mac]=$fails

            if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
                logger -t "$TAG" "deauth $mac ($ip) — $fails pings failed"
                hostapd_cli -i "$IFACE" deauthenticate "$mac" >/dev/null 2>&1
                ip neigh flush dev "$IFACE" to "$ip" 2>/dev/null
                unset "fail_count[$mac]"
            fi
        fi
    done

    # drop tracking for stations no longer listed
    for mac in "${!fail_count[@]}"; do
        found=0
        for s in "${stations[@]}"; do
            [[ "${s,,}" == "${mac,,}" ]] && found=1 && break
        done
        [ "$found" = 0 ] && unset "fail_count[$mac]"
    done

    sleep "$INTERVAL"
done
