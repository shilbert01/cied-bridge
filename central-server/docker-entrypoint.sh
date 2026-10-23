#!/bin/bash
set -euo pipefail

CERT_DIR=/certs

# Generate a self-signed cert on first run if none exists.
# In production replace with a proper CA-signed cert by mounting /certs from host.
if [[ ! -f "$CERT_DIR/server.crt" ]]; then
    echo "==> No TLS cert found — generating self-signed cert"
    mkdir -p "$CERT_DIR"
    SANS="IP:${BT_PAN_IP:-10.44.0.1},IP:127.0.0.1"
    if [[ -n "${CENTRAL_LAN_IP:-}" ]]; then
        SANS="$SANS,IP:$CENTRAL_LAN_IP"
    fi
    if [[ -n "${CENTRAL_AP_IP:-}" ]]; then
        SANS="$SANS,IP:$CENTRAL_AP_IP"
    fi
    openssl req -x509 -newkey rsa:4096 -days 3650 -nodes \
        -keyout "$CERT_DIR/server.key" \
        -out    "$CERT_DIR/server.crt" \
        -subj   "/CN=cied-central" \
        -addext "subjectAltName=$SANS"
    chmod 600 "$CERT_DIR/server.key"
    echo "==> Cert generated. Copy $CERT_DIR/server.crt to edge nodes as trusted CA."
fi

exec "$@"
