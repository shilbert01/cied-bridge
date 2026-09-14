#!/bin/sh
# feature-lib.sh — sourceable helpers for the per-clinic feature manifest.
#
# The manifest (/etc/pacemaker/clinic.conf, written by configure-clinic.sh) is a
# plain KEY=value file of FEATURE_* toggles plus shared identity (AP creds etc.).
# Box setup scripts source it, default-fill anything unset to "on" (so a box with
# no manifest behaves like the pre-feature-flag scripts did), then use these
# helpers to enable/disable services per feature.
#
# POSIX sh — works under dash/BusyBox on the Pi3 and Buildroot nodes.

CLINIC_CONF="${CLINIC_CONF:-/etc/pacemaker/clinic.conf}"

# load_clinic_conf: source the manifest if present. Returns 0 if loaded, 1 if not.
load_clinic_conf() {
    if [ -f "$CLINIC_CONF" ]; then
        # shellcheck source=/dev/null
        . "$CLINIC_CONF"
        return 0
    fi
    return 1
}

# feature_enabled FEATURE_X  -> exit 0 (true) when the var is on/yes/1/true.
feature_enabled() {
    eval "_v=\${$1:-}"
    case "$(printf '%s' "$_v" | tr 'A-Z' 'a-z')" in
        on|yes|1|true|enabled) return 0 ;;
        *) return 1 ;;
    esac
}

# apply_feature_service FEATURE_X "svc1 svc2 ..."
#   on  -> systemctl enable + restart   (bring the feature up)
#   off -> systemctl disable --now      (active teardown — a true toggle)
apply_feature_service() {
    _flag="$1"; shift
    _svcs="$*"
    [ -n "$_svcs" ] || return 0
    if feature_enabled "$_flag"; then
        systemctl enable  $_svcs >/dev/null 2>&1 || true
        systemctl restart $_svcs >/dev/null 2>&1 || true
        echo "   [$_flag=on]  up:   $_svcs"
    else
        systemctl disable --now $_svcs >/dev/null 2>&1 || true
        echo "   [$_flag=off] down: $_svcs"
    fi
}

# hostapd_hide_value: 2 when SSID hidden, 0 when broadcast (the fleet default —
# hidden SSID broke the Pico 2W join path).
hostapd_hide_value() {
    feature_enabled AP_HIDDEN && echo 2 || echo 0
}
