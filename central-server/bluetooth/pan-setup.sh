#!/bin/bash
# Configures BlueZ as a Network Access Point (NAP) so a tablet can connect
# over Bluetooth and reach the REST API at BT_PAN_IP (default 10.44.0.1).
# Run once at boot by bt-pan.service.
set -euo pipefail

CONF=/etc/pacemaker-central/server.conf
[[ -f "$CONF" ]] && source "$CONF"

BT_PAN_IP="${BT_PAN_IP:-10.44.0.1}"
IFACE=pan0

# Create the bridge interface BlueZ NAP will use
if ! ip link show "$IFACE" &>/dev/null; then
    ip link add name "$IFACE" type bridge
fi
ip addr flush dev "$IFACE" 2>/dev/null || true
ip addr add "${BT_PAN_IP}/24" dev "$IFACE"
ip link set "$IFACE" up

# Register NAP service with BlueZ
bt-network-register() {
    dbus-send --system --print-reply \
        --dest=org.bluez \
        /org/bluez \
        org.bluez.NetworkServer1.Register \
        string:nap string:"$IFACE" 2>/dev/null || true
}

# Fallback: use bluetoothctl if bt-network-register unavailable
if command -v bt-network &>/dev/null; then
    bt-network -s nap "$IFACE" &
else
    bt-network-register
fi

# Make discoverable and pairable
bluetoothctl <<'EOF'
power on
discoverable on
pairable on
agent NoInputNoOutput
default-agent
EOF

echo "BT PAN ready — server IP: ${BT_PAN_IP} on bridge: ${IFACE}"
