#!/bin/bash
# Run on the central Pi 4B as root: sudo bash install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing CIED Central server (Docker)"

# 1. Docker (official install for arm64/Raspberry Pi OS)
if ! command -v docker &>/dev/null; then
    echo "    Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker "$SUDO_USER"
fi

# 2. Host-level BT PAN dependencies (cannot run inside Docker)
apt-get install -y --no-install-recommends bluez bluez-tools dnsmasq

# 3. Bluetooth PAN host service
install -m 755 "$SCRIPT_DIR/bluetooth/pan-setup.sh" /usr/local/sbin/pan-setup.sh
install -m 644 "$SCRIPT_DIR/bluetooth/dnsmasq-bt.conf" /etc/dnsmasq.d/bt-pan.conf
install -m 644 "$SCRIPT_DIR/systemd/bt-pan.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable bt-pan.service

# 4. Deploy compose project
APP_DIR=/opt/cied-central
install -d -m 750 "$APP_DIR"
cp "$SCRIPT_DIR/compose.yaml"    "$APP_DIR/"
cp "$SCRIPT_DIR/Dockerfile"      "$APP_DIR/"
cp "$SCRIPT_DIR/docker-entrypoint.sh" "$APP_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"
cp -r "$SCRIPT_DIR/api"          "$APP_DIR/"

if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$APP_DIR/.env"
    echo "    CREATED $APP_DIR/.env — edit INGEST_TOKEN, KARDISYNCH_SMB_PASSWORD, and CENTRAL_LAN_IP before starting!"
fi

# 5. Systemd unit to manage the Docker Compose stack
cat > /etc/systemd/system/central-api.service <<'EOF'
[Unit]
Description=CIED Central API (Docker Compose)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/cied-central
ExecStart=docker compose up --build
ExecStop=docker compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable central-api.service

echo ""
echo "==> Done. Next steps:"
echo "    1. Edit $APP_DIR/.env  — set INGEST_TOKEN (must match edge nodes)"
echo "    2. sudo systemctl start bt-pan central-api"
echo "    3. Wait for first-run cert generation, then:"
echo "       docker compose -f $APP_DIR/compose.yaml cp api:/certs/server.crt /tmp/"
echo "       Copy /tmp/server.crt to each edge node at /etc/cied-edge/central-ca.crt"
echo "    4. Check logs: journalctl -fu central-api"
echo "                   docker compose -C $APP_DIR logs -f"
