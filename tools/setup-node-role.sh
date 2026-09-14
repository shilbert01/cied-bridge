#!/bin/bash
# setup-node-role.sh — select a systemd Pi Zero node's gadget role.
#
# Enables the chosen role's gadget + daemon units and DISABLES every other role's
# units (active teardown), so one node image can be repurposed by config instead
# of being reflashed. Reads NODE_ROLE from /etc/pacemaker/node-role.conf, or pass
# --role=<role> to set + persist it.
#
#   sudo tools/setup-node-role.sh                     # apply role from config
#   sudo tools/setup-node-role.sh --role=import       # set role, persist, apply
#   sudo tools/setup-node-role.sh --role=edge --install   # also run the role's install.sh
#
# SCOPE: systemd nodes only (edge, import). Buildroot nodes (printer, export) fix
# their role at image-build time and are rejected here.
set -euo pipefail

ROLE_CONF="${NODE_ROLE_CONF:-/etc/pacemaker/node-role.conf}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DO_INSTALL=0
ROLE=""

# Role -> systemd units. Keep in sync with each role's install.sh / systemd dir.
units_for() {
    case "$1" in
        edge)   echo "usb-printer-gadget.service capture-daemon.service forwarder.service storage-daemon.service" ;;
        import) echo "usb-import-gadget.service import-daemon.service" ;;
        *)      echo "" ;;
    esac
}
ALL_ROLES="edge import"

# Map role -> install.sh (for --install). import-relay has no install.sh yet, so
# its units/scripts are installed inline below.
install_role() {
    case "$1" in
        edge)
            [ -f "$REPO_DIR/edge-node/install.sh" ] && bash "$REPO_DIR/edge-node/install.sh"
            ;;
        import)
            local S="$REPO_DIR/import-relay"
            install -d -m 750 /etc/pacemaker-import
            [ -f /etc/pacemaker-import/import.conf ] || \
                install -m 640 "$S/config/import.conf.example" /etc/pacemaker-import/import.conf
            install -m 755 "$S/gadget/create-storage-image.sh" /usr/local/sbin/
            install -m 755 "$S/gadget/setup-gadget.sh"         /usr/local/sbin/setup-gadget-import.sh
            install -m 755 "$S/daemon/import_daemon.py"        /usr/local/sbin/
            install -m 644 "$S"/systemd/*.service              /etc/systemd/system/
            systemctl daemon-reload
            ;;
    esac
}

for arg in "$@"; do
    case "$arg" in
        --role=*)   ROLE="${arg#*=}" ;;
        --install)  DO_INSTALL=1 ;;
        -h|--help)  sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Resolve role: flag wins, else config.
if [ -z "$ROLE" ] && [ -f "$ROLE_CONF" ]; then
    # shellcheck source=/dev/null
    . "$ROLE_CONF"
    ROLE="${NODE_ROLE:-}"
fi
ROLE="$(printf '%s' "$ROLE" | tr 'A-Z' 'a-z')"

case "$ROLE" in
    edge|import) ;;
    printer|export)
        echo "!! '$ROLE' is a Buildroot node role — fixed at image-build time, not selectable here." >&2
        exit 3 ;;
    "") echo "!! no role set. Use --role=edge|import or write $ROLE_CONF" >&2; exit 2 ;;
    *)  echo "!! unknown role '$ROLE' (want: edge|import)" >&2; exit 2 ;;
esac

# Persist role.
install -d -m 755 "$(dirname "$ROLE_CONF")"
printf 'NODE_ROLE=%s\n' "$ROLE" > "$ROLE_CONF"

[ "$DO_INSTALL" = 1 ] && install_role "$ROLE"

# Enable the chosen role; disable every other role's units.
WANTED="$(units_for "$ROLE")"
echo "==> node role: $ROLE"
systemctl enable  $WANTED >/dev/null 2>&1 || true
systemctl restart $WANTED 2>/dev/null || true
echo "   up:   $WANTED"

for other in $ALL_ROLES; do
    [ "$other" = "$ROLE" ] && continue
    OTHER_UNITS="$(units_for "$other")"
    systemctl disable --now $OTHER_UNITS >/dev/null 2>&1 || true
    echo "   down: $OTHER_UNITS"
done

echo "Done. Role '$ROLE' active (persisted in $ROLE_CONF)."
