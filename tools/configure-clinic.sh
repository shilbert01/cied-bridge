#!/bin/bash
# configure-clinic.sh — write the per-clinic feature manifest (/etc/cied-bridge/clinic.conf).
#
# Interactive wizard by default; flags for non-interactive / redeploy use
# (e.g. clinic-hub-redeploy.sh). Existing values are preserved: the current
# manifest is loaded first, flags override, then the wizard prompts using the
# current value as the default.
#
# Examples:
#   sudo tools/configure-clinic.sh                       # wizard
#   sudo tools/configure-clinic.sh -y --samba=on --import-hub=on --printer-webui=off
#   sudo tools/configure-clinic.sh --out=./clinic.conf   # write elsewhere (testing)
set -euo pipefail

OUT="${CLINIC_CONF:-/etc/cied-bridge/clinic.conf}"
INTERACTIVE=1

# ── defaults (overridden by existing manifest, then flags, then prompts) ──────
CLINIC_NAME="clinic"
FEATURE_RELAY_AP=on
FEATURE_SAMBA_SHARE=off
FEATURE_IMPORT_HUB=off
FEATURE_PRINTER_WEBUI=off
FEATURE_CENTRAL_WEBUI=off
AP_SSID="relayap"
AP_PASS="RelayPrint#1"
AP_CHANNEL=6
AP_HIDDEN=off
WIFI_COUNTRY=DE
CENTRAL_URL="https://central.local:8443"

norm() { case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in on|yes|1|true) echo on;; *) echo off;; esac; }

usage() {
    sed -n '2,12p' "$0"
    cat <<'EOF'

Flags: --clinic-name= --relay-ap=on/off --samba= --import-hub= --printer-webui=
       --central-webui= --ap-ssid= --ap-pass= --ap-channel= --hidden=on/off
       --country= --central-url= --out=PATH  -y/--non-interactive
EOF
}

# Load existing manifest so a re-run preserves current settings.
if [ -f "$OUT" ]; then . "$OUT"; fi

for arg in "$@"; do
    case "$arg" in
        --clinic-name=*)    CLINIC_NAME="${arg#*=}" ;;
        --relay-ap=*)       FEATURE_RELAY_AP=$(norm "${arg#*=}") ;;
        --samba=*)          FEATURE_SAMBA_SHARE=$(norm "${arg#*=}") ;;
        --import-hub=*)     FEATURE_IMPORT_HUB=$(norm "${arg#*=}") ;;
        --printer-webui=*)  FEATURE_PRINTER_WEBUI=$(norm "${arg#*=}") ;;
        --central-webui=*)  FEATURE_CENTRAL_WEBUI=$(norm "${arg#*=}") ;;
        --ap-ssid=*)        AP_SSID="${arg#*=}" ;;
        --ap-pass=*)        AP_PASS="${arg#*=}" ;;
        --ap-channel=*)     AP_CHANNEL="${arg#*=}" ;;
        --hidden=*)         AP_HIDDEN=$(norm "${arg#*=}") ;;
        --country=*)        WIFI_COUNTRY="${arg#*=}" ;;
        --central-url=*)    CENTRAL_URL="${arg#*=}" ;;
        --out=*)            OUT="${arg#*=}" ;;
        -y|--non-interactive|--yes) INTERACTIVE=0 ;;
        -h|--help)          usage; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; usage; exit 2 ;;
    esac
done

if [ "$INTERACTIVE" = 1 ]; then
    ask_toggle() {  # ask_toggle VAR "prompt"
        local var=$1 prompt=$2 cur; eval "cur=\$$var"
        local hint; [ "$cur" = on ] && hint="Y/n" || hint="y/N"
        local a; read -rp "  $prompt [$hint]: " a || a=""
        if [ -z "$a" ]; then return; fi
        case "$(printf '%s' "$a" | tr 'A-Z' 'a-z')" in
            y|yes|on) eval "$var=on" ;; *) eval "$var=off" ;;
        esac
    }
    ask_value() {  # ask_value VAR "prompt"
        local var=$1 prompt=$2 cur; eval "cur=\$$var"
        local a; read -rp "  $prompt [$cur]: " a || a=""
        [ -n "$a" ] && eval "$var=\$a"
    }

    echo "== CIED Bridge clinic configuration =="
    ask_value  CLINIC_NAME           "Clinic name"
    echo "-- Features --"
    ask_toggle FEATURE_RELAY_AP      "Relay AP (WiFi subnet the nodes join)"
    ask_toggle FEATURE_SAMBA_SHARE   "Samba export share"
    ask_toggle FEATURE_IMPORT_HUB    "Import hub (USB presenter drive)"
    ask_toggle FEATURE_PRINTER_WEBUI "Printer web interface"
    ask_toggle FEATURE_CENTRAL_WEBUI "CIED Central web UI (Pi4)"
    if [ "$FEATURE_RELAY_AP" = on ]; then
        echo "-- Relay AP settings --"
        ask_value  AP_SSID  "AP SSID"
        ask_value  AP_PASS  "AP passphrase"
        ask_toggle AP_HIDDEN "Hide SSID (No recommended — hidden breaks Pico 2W)"
    fi
fi

umask 077
mkdir -p "$(dirname "$OUT")"
tmp="$(mktemp "${OUT}.XXXXXX")"
cat > "$tmp" <<EOF
# CIED Bridge clinic feature manifest — written by configure-clinic.sh $(date -u +%FT%TZ)
CLINIC_NAME="$CLINIC_NAME"

# Relay subnet / AP
FEATURE_RELAY_AP=$FEATURE_RELAY_AP
AP_SSID="$AP_SSID"
AP_PASS="$AP_PASS"
AP_CHANNEL=$AP_CHANNEL
AP_HIDDEN=$AP_HIDDEN
WIFI_COUNTRY=$WIFI_COUNTRY

# Pi3 features
FEATURE_SAMBA_SHARE=$FEATURE_SAMBA_SHARE
FEATURE_IMPORT_HUB=$FEATURE_IMPORT_HUB
FEATURE_PRINTER_WEBUI=$FEATURE_PRINTER_WEBUI

# Pi4 central features
FEATURE_CENTRAL_WEBUI=$FEATURE_CENTRAL_WEBUI
CENTRAL_URL="$CENTRAL_URL"
EOF
mv "$tmp" "$OUT"

echo ""
echo "Wrote $OUT:"
grep -E '^(CLINIC_NAME|FEATURE_)' "$OUT" | sed 's/^/   /'
echo ""
echo "Next: run the box setup script(s) — they read this manifest and enable/"
echo "disable each feature's services accordingly:"
echo "   export-relay/pi3-server/setup-export.sh   (samba, import hub, AP)"
echo "   printer-relay/pi3-server/setup-cups.sh    (printer web UI, AP)"
