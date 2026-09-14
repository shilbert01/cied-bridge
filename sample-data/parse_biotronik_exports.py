#!/usr/bin/env python3
"""
Example: parse Biotronik XML exports from an ics3000 USB folder.

Handles both formats that Biotronik programmers (ICS-3000 etc.) export:

  BIOIEEE_<serial>_<name>_<date>.xml  — IEEE 11073 MDC format
  BIOSTD_<date>_<name>_<serial>.xml   — carddas InterfaceData (GE Centricity Xi2)

Each session produces both files; the BIOIEEE file has richer structured data
(per-channel thresholds, detection zones, episode statistics).

Usage:
  python3 parse_biotronik_exports.py /media/basti/4A38-3DB9/ics3000
  python3 parse_biotronik_exports.py path/to/single.xml
"""

import argparse
import json
import sys
from pathlib import Path

# Allow running from this directory without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent / "central-server"))

from api.parsers.biotronik_xml import (
    detect_biotronik_xml_format,
    parse_biotronik_xml_auto,
)


def parse_file(path: Path) -> dict | None:
    data = path.read_bytes()
    fmt = detect_biotronik_xml_format(data)
    result = parse_biotronik_xml_auto(data)
    result["_source_file"] = str(path.name)
    result["_format"] = fmt
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Directory of .xml files or a single .xml file")
    ap.add_argument("--format", choices=["json", "table"], default="table")
    args = ap.parse_args()

    target = Path(args.path)
    if target.is_dir():
        xml_files = sorted(target.glob("*.xml"))
    elif target.is_file():
        xml_files = [target]
    else:
        print(f"Error: {target} not found", file=sys.stderr)
        sys.exit(1)

    records = []
    for f in xml_files:
        try:
            rec = parse_file(f)
            records.append(rec)
        except Exception as e:
            print(f"SKIP {f.name}: {e}", file=sys.stderr)

    if args.format == "json":
        print(json.dumps(records, ensure_ascii=False, indent=2, default=str))
        return

    # Table output
    COLS = [
        ("_format",            "fmt",        8),
        ("device_serial",      "serial",     12),
        ("device_model",       "model",      22),
        ("device_type",        "type",       11),
        ("interrogation_date", "date",       12),
        ("battery_voltage",    "batt_V",      7),
        ("sensing_rv",         "s_RV_mV",    9),
        ("threshold_rv_v",     "thr_RV_V",   9),
        ("threshold_rv_pw",    "thr_RV_ms",  10),
        ("impedance_rv",       "imp_RV_Ω",   10),
        ("impedance_hv",       "hv_imp_Ω",   10),
    ]

    header = "  ".join(label.ljust(w) for _, label, w in COLS)
    print(header)
    print("-" * len(header))

    for rec in records:
        row = "  ".join(
            str(rec.get(key, "")).ljust(w)[:w]
            for key, _, w in COLS
        )
        print(row)

    print(f"\n{len(records)} records parsed from {len(xml_files)} files")


if __name__ == "__main__":
    main()
