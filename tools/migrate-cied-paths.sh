#!/bin/bash
# migrate-cied-paths.sh — one-shot migration of old pacemaker-* paths to cied-*.
#
# Run ON the target hub as root:
#   sudo bash tools/migrate-cied-paths.sh
#
# Idempotent: skips any path that already exists at the new location.
# Does NOT delete old paths — leaves symlinks so in-flight services survive a
# restart gap between migration and redeploy.
set -euo pipefail

migrate() {
    local old="$1" new="$2"
    if [ -d "$old" ] && [ ! -d "$new" ] && [ ! -L "$old" ]; then
        mv "$old" "$new"
        ln -s "$new" "$old"
        echo "   $old → $new (symlink left behind)"
    elif [ -d "$new" ]; then
        echo "   $new already exists — skipped"
    elif [ -L "$old" ]; then
        echo "   $old is already a symlink — skipped"
    else
        echo "   $old does not exist — skipped"
    fi
}

echo "==> Migrating config + data paths from pacemaker-* to cied-*"

migrate /etc/pacemaker           /etc/cied-bridge
migrate /var/lib/pacemaker-export      /var/lib/cied-export
migrate /var/lib/pacemaker-import-hub  /var/lib/cied-import-hub

echo ""
echo "==> Done. Now redeploy to update all services to the new paths:"
echo "   PI=ciedserver01.fritz.box bash tools/clinic-hub-redeploy.sh"
echo ""
echo "After redeploy is confirmed working, remove the old symlinks:"
echo "   sudo rm /etc/pacemaker /var/lib/pacemaker-export /var/lib/pacemaker-import-hub"
