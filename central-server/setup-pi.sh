#!/bin/bash
# Full setup script for the central server Pi.
# Run as: sudo bash ~/cied-central/setup-pi.sh
set -euo pipefail

DEPLOY_DIR=/opt/cied-central
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Clinic feature manifest ──────────────────────────────────────────────────
# The central web UI is a clinic feature; gate it on FEATURE_CENTRAL_WEBUI.
# Unset (or no manifest) defaults to "on" so existing central boxes are unaffected.
CLINIC_CONF="${CLINIC_CONF:-/etc/cied-bridge/clinic.conf}"
[ -f "$SRC_DIR/../tools/feature-lib.sh" ] && . "$SRC_DIR/../tools/feature-lib.sh"
[ -f "$CLINIC_CONF" ] && . "$CLINIC_CONF"
: "${FEATURE_CENTRAL_WEBUI:=on}"
type feature_enabled >/dev/null 2>&1 || feature_enabled() { eval "_v=\${$1:-}"; case "$(printf %s "$_v"|tr A-Z a-z)" in on|yes|1|true) return 0;; *) return 1;; esac; }

echo "==> [1/6] Installing system dependencies"
apt-get update -qq
apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    bluez bluez-tools \
    dnsmasq

echo "==> [2/6] Installing Docker"
if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    usermod -aG docker basti
    systemctl enable docker
else
    echo "    Docker already installed: $(docker --version)"
fi

echo "==> [3/6] Deploying application"
install -d -m 755 "$DEPLOY_DIR"
cp -r "$SRC_DIR/api"                     "$DEPLOY_DIR/"
cp    "$SRC_DIR/Dockerfile"              "$DEPLOY_DIR/"
cp    "$SRC_DIR/docker-entrypoint.sh"   "$DEPLOY_DIR/"
cp    "$SRC_DIR/requirements.txt"        "$DEPLOY_DIR/"
cp    "$SRC_DIR/compose.yaml"            "$DEPLOY_DIR/"

if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
    cp "$SRC_DIR/.env.example" "$DEPLOY_DIR/.env"
    # Generate a random token so it's not left as 'changeme'
    GENERATED_TOKEN=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
    sed -i "s/INGEST_TOKEN=changeme/INGEST_TOKEN=${GENERATED_TOKEN}/" "$DEPLOY_DIR/.env"
    echo ""
    echo "    *** Generated INGEST_TOKEN: ${GENERATED_TOKEN} ***"
    echo "    Set this as CENTRAL_TOKEN in /etc/cied-edge/node.conf on each edge node."
    echo ""
    # Generate a random SMB password for the KardiSynch outbox share
    GENERATED_SMB_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
    sed -i "s/KARDISYNCH_SMB_PASSWORD=changeme/KARDISYNCH_SMB_PASSWORD=${GENERATED_SMB_PASSWORD}/" "$DEPLOY_DIR/.env"
    echo ""
    echo "    *** Generated KARDISYNCH_SMB_PASSWORD: ${GENERATED_SMB_PASSWORD} ***"
    echo "    Use this (user 'kardisynch') to mount //<CENTRAL_LAN_IP>/kardisynch from the KardiSynch workstation."
    echo "    Also set CENTRAL_LAN_IP in $DEPLOY_DIR/.env before starting — it defaults to a placeholder."
    echo ""
fi

echo "==> [4/6] Configuring Bluetooth PAN"
cp "$SRC_DIR/bluetooth/pan-setup.sh"   /usr/local/sbin/pan-setup.sh
chmod 755 /usr/local/sbin/pan-setup.sh
cp "$SRC_DIR/bluetooth/dnsmasq-bt.conf" /etc/dnsmasq.d/bt-pan.conf
cp "$SRC_DIR/systemd/bt-pan.service"   /etc/systemd/system/

echo "==> [5/6] Installing systemd unit for Docker Compose stack"
cat > /etc/systemd/system/central-api.service <<'EOF'
[Unit]
Description=CIED Central API (Docker Compose)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/cied-central
ExecStart=/usr/bin/docker compose up --build
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
# bt-pan + dnsmasq are foundation (edge nodes reach central over BT PAN); the API
# web UI is the toggled feature.
systemctl enable bt-pan.service dnsmasq

echo "==> [6/6] Starting services"
systemctl start dnsmasq || true
systemctl start bt-pan.service || true
if feature_enabled FEATURE_CENTRAL_WEBUI; then
    systemctl enable central-api.service
    systemctl start  central-api.service
    echo "   [FEATURE_CENTRAL_WEBUI=on]  central-api up"
else
    systemctl disable --now central-api.service 2>/dev/null || true
    echo "   [FEATURE_CENTRAL_WEBUI=off] central-api down"
fi

echo ""
echo "============================================================"
echo " Setup complete!"
echo ""
echo " Check status:  systemctl status central-api"
echo " Follow logs:   journalctl -fu central-api"
echo "                docker compose -C /opt/cied-central logs -f"
echo ""
echo " Once the container is up, extract the TLS cert for edge nodes:"
echo "   docker compose -C /opt/cied-central cp api:/certs/server.crt /tmp/server.crt"
echo "   # then copy /tmp/server.crt to each edge node"
echo "============================================================"
