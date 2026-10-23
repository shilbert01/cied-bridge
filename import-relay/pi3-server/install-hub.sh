#!/usr/bin/env bash
#
# Install the import hub on the Pi3 (or wherever senders POST and presenters
# pull). Run ON that box, as root, from a copy of the import-relay dir:
#
#     sudo bash import-relay/pi3-server/install-hub.sh
#
# Installs import-server.py + its systemd unit and enables it. Idempotent.
#
# Config (env-overridable): PORT (8090), LEASE_SECS (120),
#     QUEUE_ROOT (/var/lib/cied-import-hub)
set -euo pipefail

PORT="${PORT:-8090}"
LEASE_SECS="${LEASE_SECS:-120}"
QUEUE_ROOT="${QUEUE_ROOT:-/var/lib/cied-import-hub}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # import-relay/pi3-server/

log() { echo -e "\033[1;36m==>\033[0m $*"; }
die() { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash $0"
command -v systemctl >/dev/null || die "no systemd here"
command -v python3   >/dev/null || die "python3 missing"
[ -f "$SRC_DIR/import-server.py" ] || die "import-server.py not next to this script ($SRC_DIR)"

log "Installing import-server.py -> /usr/local/sbin…"
install -Dm755 "$SRC_DIR/import-server.py" /usr/local/sbin/import-server.py

log "Installing systemd unit (port=$PORT lease=${LEASE_SECS}s queue=$QUEUE_ROOT)…"
install -Dm644 "$SRC_DIR/import-server.service" /etc/systemd/system/import-server.service
# Apply any non-default config into the unit's Environment lines.
sed -i "s|^Environment=IMPORT_LISTEN_PORT=.*|Environment=IMPORT_LISTEN_PORT=${PORT}|; \
        s|^Environment=IMPORT_LEASE_SECS=.*|Environment=IMPORT_LEASE_SECS=${LEASE_SECS}|; \
        s|^Environment=IMPORT_QUEUE_ROOT=.*|Environment=IMPORT_QUEUE_ROOT=${QUEUE_ROOT}|" \
    /etc/systemd/system/import-server.service

mkdir -p "$QUEUE_ROOT"

systemctl daemon-reload
systemctl enable --now import-server
sleep 1
systemctl --no-pager --lines=0 status import-server || true

echo
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "Import hub up on :${PORT}"
cat <<DONE

  Health:   curl http://127.0.0.1:${PORT}/health          # -> ok
  Status:   curl http://127.0.0.1:${PORT}/                 # -> pending=N inflight=M
  Senders POST to:  http://${IP:-<pi3-ip>}:${PORT}/submit

  A presenter (e.g. the HID receiver, node hid-leipzig-01) will now be able to
  claim/fetch/ack. Test-inject a file from any box on the network:
    curl -sS -X POST http://${IP:-<pi3-ip>}:${PORT}/submit \\
         -H "X-File-Name: test.pdf" -H "X-Target-Node: hid-leipzig-01" \\
         --data-binary @test.pdf
DONE
