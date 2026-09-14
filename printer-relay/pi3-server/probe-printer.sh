#!/bin/sh
# probe-printer.sh [QUEUE]
#
# Detect what a CUPS/IPP printer will accept, to choose the relay queue type:
#   RAW passthrough  — for PCL/PS *language* printers (Kyocera P-5539i, HP
#                      P3015dn): the relay forwards the programmer's PCL/PS
#                      bytes untouched, so the report prints exactly as sent.
#   DRIVERLESS       — for IPP-Everywhere printers with no PCL/PS interpreter:
#                      CUPS must convert to PDF/PWG-raster (raw PCL gets mangled).
#
# Reads IPP capabilities + probes each format with Validate-Job (non-printing).
#
# IMPORTANT: Validate-Job reports CLAIMED acceptance only. A printer can PASS
# validation yet still fail the real job at transport — e.g. the HP Color
# LaserJet Pro MFP 3302 passes octet-stream Validate-Job but its ipp-usb path
# returns CUPS backend "status 7 (retry job immediately)" on the actual
# Send-Document and never prints. Always confirm with a live test (--print).
#
# Usage: probe-printer.sh [QUEUE]     (default queue: LaserPrinter)

QUEUE="${1:-LaserPrinter}"
URI=$(lpstat -v "$QUEUE" 2>/dev/null | sed 's/.*device for [^:]*: //')
[ -n "$URI" ] || { echo "no such CUPS queue: $QUEUE"; exit 1; }

echo "Queue : $QUEUE"
echo "URI   : $URI"

echo "--- capabilities ---"
ipptool -tv "$URI" get-printer-attributes.test 2>/dev/null \
  | grep -iE "printer-state |printer-state-reasons|make-and-model|printer-device-id|document-format-supported|document-format-default" \
  | sed 's/^[[:space:]]*/  /'

# Languages the printer advertises in its device-id CMD: field.
DID=$(ipptool -tv "$URI" get-printer-attributes.test 2>/dev/null | sed -n 's/.*printer-device-id.*= //p')
CMD=$(printf '%s' "$DID" | tr ';' '\n' | sed -n 's/^CMD://p')
echo "--- languages (device-id CMD) ---"
echo "  ${CMD:-<none advertised>}"

echo "--- Validate-Job per format (CLAIMED acceptance, non-printing) ---"
for fmt in application/octet-stream application/pdf image/pwg-raster image/urf \
           application/postscript application/vnd.hp-PCL5 \
           application/vnd.hp-PCLXL application/PCLm ; do
  T=$(mktemp)
  cat > "$T" <<EOF
{
  OPERATION Validate-Job
  GROUP operation-attributes-tag
  ATTR charset attributes-charset utf-8
  ATTR naturalLanguage attributes-natural-language en
  ATTR uri printer-uri \$uri
  ATTR name requesting-user-name probe
  ATTR mimeMediaType document-format $fmt
  STATUS successful-ok
  STATUS client-error-document-format-not-supported
}
EOF
  res=$(ipptool -tv "$URI" "$T" 2>/dev/null | sed -n 's/.*status-code = //p' | head -1)
  case "$res" in
    successful-ok*)   echo "  ACCEPT  $fmt" ;;
    *not-supported*)  echo "  reject  $fmt" ;;
    *)                echo "  ?[$res]  $fmt" ;;
  esac
  rm -f "$T"
done

echo "--- recommendation ---"
if printf '%s' "$CMD" | grep -qiE "PCLXL|PCL5|(^|,)PCL(,|$)|POSTSCRIPT|(^|,)PS(,|$)"; then
  echo "  PCL/PS language present -> RAW passthrough is viable:"
  echo "     lpadmin -p $QUEUE -E -v \"$URI\" -m raw"
  echo "  (relay forwards the programmer's PCL/PS untouched)"
else
  echo "  No PCL/PS language advertised -> raster/driverless only -> DRIVERLESS:"
  echo "     lpadmin -p $QUEUE -E -v \"$URI\" -m everywhere"
  echo "  (CUPS converts to PDF/PWG-raster; a raw PCL passthrough would be mangled)"
fi
echo
echo "  NB: Validate-Job PASS != it will print. Confirm with a real PCL/PS job."
echo "      If CUPS error_log loops 'Backend ipp returned status 7 (retry job"
echo "      immediately)', the raw+ipp-usb path is failing at TRANSPORT for this"
echo "      printer regardless of format (observed on HP Color LaserJet Pro 3302)."

# ── live test: the TRUTH (submits a tiny real job, watches accept vs status-7) ──
# Pass --print as the 2nd arg. This is the only check that catches printers which
# pass Validate-Job but still fail the real Send-Document (the HP 3302 trap).
[ "$2" = "--print" ] || { echo; echo "  (run with '$0 $QUEUE --print' for a live accept/reject test)"; exit 0; }

echo
echo "--- LIVE TEST: submit a minimal PCL job and watch it ---"
/usr/sbin/cupsenable "$QUEUE" 2>/dev/null; /usr/sbin/cupsaccept "$QUEUE" 2>/dev/null
cancel -a "$QUEUE" 2>/dev/null
PCL=$(mktemp)
printf '\033E\033&l0O Konsilado printer-probe %s \014\033E' "$(date +%T)" > "$PCL"
before=$(lpstat -W completed -o "$QUEUE" 2>/dev/null | grep -c .)
lp -d "$QUEUE" -o raw "$PCL" >/dev/null 2>&1
verdict="INCONCLUSIVE"
for i in $(seq 1 8); do
  q=$(lpstat -o "$QUEUE" 2>/dev/null | grep -c .)
  s7=$(tail -6 /var/log/cups/error_log 2>/dev/null | grep -c "status 7")
  na=$(tail -6 /var/log/cups/error_log 2>/dev/null | grep -c "not accepted")
  if [ "$q" -eq 0 ]; then verdict="PRINTS  (job drained/completed — raw works on this printer)"; break; fi
  if [ "$s7" -gt 0 ] || [ "$na" -gt 0 ]; then verdict="FAILS   (backend status-7 / not-accepted — raw+ipp-usb transport broken here)"; fi
  sleep 4
done
cancel -a "$QUEUE" 2>/dev/null
rm -f "$PCL"
echo "  LIVE RESULT: $verdict"
echo "  (check the printer for a physical page to be 100% sure)"
