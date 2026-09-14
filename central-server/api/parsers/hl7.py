"""
BSC S-ICD EMBLEM A209 HL7 v2.6 ORU^R01 parser (IHE PCD-09 profile).

The programmer writes one .hl7 file per session folder. A file may contain
multiple concatenated messages (one per interrogation). Each message starts
with MSH and ends before the next MSH (or EOF).

Returns a list[dict] — one dict per message — so the caller can insert
multiple interrogation records from a single file.
"""

import re
from .base import ParseError


# MDC observation codes → db field (numeric observations only)
_OBX_NUMERIC: dict[str, str] = {
    "721536": "battery_remaining_pct",    # % battery
    "731840": "vf_detection_interval_ms", # VF/VT zone detection interval (ms)
    "732225": "vf_shock_energy_j",        # shock energy (J)
    "737824": "shocks_recent",
    "737840": "shocks_total",
    "738000": "episodes_recent",
    "738032": "episodes_total",
}

# MDC codes with string values
_OBX_STRING: dict[str, str] = {
    "720898": "device_model",    # e.g. "A209"
    "720899": "device_serial",
    "720901": "implant_date",    # YYYYMMDD
    "721025": "interrogation_date",  # YYYYMMDDHHmm+TZ
    "720961": "lead_model",
    "720962": "lead_serial",
}

# Zone type enum codes → human-readable
_ZONE_TYPE: dict[str, str] = {
    "754945": "VF",
    "754946": "VT",
}


def parse_hl7_file(file_path: str) -> list[dict]:
    """
    Parse all HL7 messages in a file and return one dict per message.
    Raises ParseError if the file cannot be read or contains no valid messages.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        raise ParseError(f"Cannot read HL7 file: {e}") from e

    # Split into individual messages at each MSH segment
    raw_messages = re.split(r"(?=\bMSH\|)", content)
    results = []
    for raw in raw_messages:
        raw = raw.strip()
        if not raw.startswith("MSH|"):
            continue
        record = _parse_message(raw)
        if record:
            results.append(record)

    if not results:
        raise ParseError("No valid HL7 messages found in file")
    return results


def _parse_message(raw: str) -> dict | None:
    segments: dict[str, list[list[str]]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("|")
        seg_type = fields[0]
        segments.setdefault(seg_type, []).append(fields)

    result: dict = {}

    # --- PID: patient name, device identity in PID-3 ---
    for pid in segments.get("PID", []):
        if len(pid) > 3:
            # PID-3: "model:A209/serial:306839^^^BSX^U"
            m = re.search(r"model:([^/|^]+)/serial:(\d+)", pid[3])
            if m:
                result.setdefault("device_model",  m.group(1))
                result.setdefault("device_serial", m.group(2))
        if len(pid) > 5 and pid[5]:
            # PID-5: "Given^Family," or "Family,Given" (data quality varies)
            name_raw = pid[5].rstrip(",").strip()
            if "^" in name_raw:
                parts = name_raw.split("^")
                # HL7 order: family^given
                result.setdefault("patient_name",
                                   f"{parts[0].strip()}, {parts[1].strip()}" if parts[1] else parts[0].strip())
            else:
                result.setdefault("patient_name", name_raw)

    # --- OBX segments ---
    # Build zone type map first (zone_num → "VF"/"VT")
    zone_types: dict[str, str] = {}
    for obx in segments.get("OBX", []):
        if len(obx) < 6:
            continue
        code = obx[3].split("^")[0]
        sub_id = obx[4] if len(obx) > 4 else ""
        value = obx[5]
        if code == "731648" and sub_id and value:
            enum_code = value.split("^")[0]
            zone_types[sub_id] = _ZONE_TYPE.get(enum_code, "")

    # Now extract values
    vf_interval: dict[str, float] = {}
    vf_energy:   dict[str, float] = {}

    for obx in segments.get("OBX", []):
        if len(obx) < 6:
            continue
        code   = obx[3].split("^")[0]
        sub_id = obx[4] if len(obx) > 4 else ""
        value  = obx[5]
        unit   = obx[6] if len(obx) > 6 else ""

        if code in _OBX_NUMERIC:
            try:
                fval = float(value)
            except (ValueError, TypeError):
                continue
            field = _OBX_NUMERIC[code]
            if code == "731840":
                vf_interval[sub_id] = fval
            elif code == "732225":
                vf_energy[sub_id] = fval
            else:
                result[field] = fval

        elif code in _OBX_STRING:
            if value:
                result[_OBX_STRING[code]] = value

    # Map zone intervals/energies using the zone type lookup
    for sub_id, zone in zone_types.items():
        if zone == "VF":
            if sub_id in vf_interval:
                result["vf_detection_interval_ms"] = vf_interval[sub_id]
            if sub_id in vf_energy:
                result["vf_shock_energy_j"] = vf_energy[sub_id]
        elif zone == "VT":
            if sub_id in vf_interval:
                result["vt_detection_interval_ms"] = vf_interval[sub_id]
            if sub_id in vf_energy:
                result["vt_shock_energy_j"] = vf_energy[sub_id]

    # Normalise implant date YYYYMMDD → YYYY-MM-DD
    if "implant_date" in result:
        d = result["implant_date"]
        if re.match(r"^\d{8}$", d):
            result["implant_date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"

    # Require at least a device serial to consider this a valid record
    if not result.get("device_serial"):
        return None

    return result
