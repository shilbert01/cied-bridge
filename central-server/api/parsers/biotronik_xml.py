"""
Biotronik native XML export parsers — two formats:

  1. BIOSTD (carddas InterfaceData): parse_biotronik_xml()
     <carddas:InterfaceData> root, GE Centricity Xi2 target, flat TableEntry key-value rows.

  2. BIOIEEE (IEEE 11073): parse_biotronik_ieee11073()
     <biotronik-ieee11073-export> root, nested <section name="...">/<value name="..."> tree,
     IEEE 11073 MDC_IDC_* code types.  Produced by BioICSConverter, Home Monitoring, and
     the same programmer that generates the BIOSTD file (both files are always exported in
     pairs for the same session).

Use detect_biotronik_xml_format() to decide which parser to call, or call
parse_biotronik_xml_auto() which does both steps.

Distinct from biotronik.py which parses Ghostscript-rendered PS printouts via coordinate
layout — that path is used for PS/PDF print captures, not structured exports.

XML structure:
  InterfaceData
    Patient / PersonalData  — name, DOB
    Examination
      ExaminationDate
      Measurements / Table[]    — device summary (SERHSM, ACTBATTERYVOLTAGE, FU_RA_*, ...)
      AdditionalMeasurements / Table[]  — lead settings, arrhythmia stats (table 9473)

Known table attribute names (Biotronik internal, with German/English variants):
  Device summary table (found via MANUFACTURERDESCR or CATAGGREGATDESCR attribute):
    MANUFACTURERDESCR, CATAGGREGATDESCR, SERHSM
    ACTBATTERYVOLTAGE, FU1BATTERYSTATUS
    FU_RA_IMPED, FU_RV_IMPED, FU_LV_IMPED
    FU_RA_SENSING, FU_RV_SENSING
    A_AMPLITUDE, A_IMPDAUER  (atrial pacing output)
    V_AMPLITUDE, V_IMPDAUER  (RV pacing output)
    LV_AMPLITUDE, LV_IMPDAUER (LV pacing output)

  Settings / lead table (found via Elektrodenmodell or LeadModel):
    Kanäle / Channels            — installed lead channel names (RA, RV, LV)
    Hersteller / Manufacturer    — per-channel lead manufacturer
    Elektrodenmodell / LeadModel — per-channel lead model
    Seriennummer / SerialNumber  — per-channel lead serial
    Batterie-Restkapazität / BatteryRemainingCapacity

  Arrhythmia stats table (TableName = "9473"):
    Atriale Arrhythmielast / Atrial Arrhythmia Burden  — AT/AF burden %

Channel alignment:
  Biotronik XMLs can contain multiple historical Kanäle blocks but only one current
  Elektrodenmodell block. Naively zipping channels[i] with models[i] silently maps
  the wrong model to the wrong lead. _align_channels() scores each candidate block
  and picks the one where model-presence and channel-presence best correspond.
"""

import re
import xml.etree.ElementTree as ET

from .base import ParseError


def parse_biotronik_xml(xml_data: str | bytes) -> dict:
    """Parse a Biotronik InterfaceData XML. Returns a fields dict for insert_interrogation."""
    if isinstance(xml_data, bytes):
        xml_data = xml_data.decode("utf-8", errors="replace")

    # Strip namespace declarations and prefixes so tag names are bare.
    # Mirrors KardiSynch's XMLParser transformTagName which strips "prefix:" from tags.
    xml_data = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', "", xml_data)
    xml_data = re.sub(r"<(/?)\w+:", r"<\1", xml_data)
    # Also strip namespace prefix from attribute names: carddas:Source="x" → Source="x"
    # Needed for BIOSTD files whose root element has prefixed attribute names.
    xml_data = re.sub(r"([ \t])(\w+):(\w+)(=)", r"\1\3\4", xml_data)

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        raise ParseError(f"Biotronik XML: parse error — {e}") from e

    examination = root.find("Examination")
    if examination is None:
        raise ParseError("Biotronik XML: no <Examination> element found")

    personal_data = root.find("Patient/PersonalData")

    # ── Table helpers ─────────────────────────────────────────────────────────

    def _gather_tables() -> list[ET.Element]:
        tables: list[ET.Element] = []
        for section in ("Measurements", "AdditionalMeasurements"):
            sec = examination.find(section)  # type: ignore[union-attr]
            if sec is not None:
                tables.extend(sec.findall("Table"))
        return tables

    def _entry_value(entry: ET.Element) -> str | None:
        for tag in ("CharValue", "DecimalValue", "SmallIntValue", "DateValue"):
            el = entry.find(tag)
            if el is not None and el.text:
                return el.text.strip()
        return None

    def _find_table_by_name(name: str) -> list[ET.Element]:
        for table in _gather_tables():
            if (table.findtext("TableName") or "").strip() == name:
                return table.findall("TableEntry")
        return []

    def _find_table_by_attr(attr_name: str) -> list[ET.Element]:
        attr_lower = attr_name.lower()
        for table in _gather_tables():
            entries = table.findall("TableEntry")
            if any((e.findtext("AttributeName") or "").lower() == attr_lower for e in entries):
                return entries
        return []

    def _find(entries: list[ET.Element], *names: str) -> str | None:
        """Return first non-empty value matching any of the given attribute names."""
        for name in names:
            nl = name.lower()
            for e in entries:
                if (e.findtext("AttributeName") or "").lower() == nl:
                    v = _entry_value(e)
                    if v:
                        return v
        return None

    def _find_all(entries: list[ET.Element], *names: str) -> list[str]:
        """Return all values for the first attribute name that yields any results."""
        for name in names:
            nl = name.lower()
            vals = [
                v for e in entries
                if (e.findtext("AttributeName") or "").lower() == nl
                if (v := _entry_value(e))
            ]
            if vals:
                return vals
        return []

    # ── Locate key tables ────────────────────────────────────────────────────

    summary = _find_table_by_attr("MANUFACTURERDESCR") or _find_table_by_attr("CATAGGREGATDESCR")
    settings = (
        _find_table_by_attr("Elektrodenmodell")
        or _find_table_by_attr("LeadModel")
        or _find_table_by_attr("Kanäle")
        or _find_table_by_attr("Channels")
    )
    stats = _find_table_by_name("9473")

    # ── nsT (non-sustained VT) episode count ─────────────────────────────────

    nst_count = 0
    for table in _gather_tables():
        if (table.findtext("TableName") or "") == "TBU_EPISODE_LIST":
            for fk in table.findall("ForeignKey"):
                if any(e.findtext("CharValue") == "nsT" for e in fk.findall("TableEntry")):
                    nst_count += 1

    # ── Patient ───────────────────────────────────────────────────────────────

    first_name = last_name = dob = ""
    if personal_data is not None:
        def _pd(tag: str) -> str:
            el = personal_data.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        first_name = _pd("FirstName") or _pd("Vorname")
        last_name  = _pd("Name") or _pd("LastName") or _pd("Nachname")
        dob        = _pd("DOB") or _pd("DateOfBirth") or _pd("Geburtsdatum") or _pd("BirthDate")

    patient_name = f"{last_name}, {first_name}".strip(", ") or None

    # ── Device ────────────────────────────────────────────────────────────────

    device_model  = _find(summary, "CATAGGREGATDESCR") or ""
    device_serial = _find(summary, "SERHSM") or ""
    manufacturer  = _find(summary, "MANUFACTURERDESCR") or "Biotronik"

    model_upper = device_model.upper()
    if "CRT-D" in model_upper or "HF-T" in model_upper:
        device_type = "CRT-D"
    elif "CRT-P" in model_upper or "HF-P" in model_upper:
        device_type = "CRT-P"
    elif any(k in model_upper for k in ("ICD", "DEFI", "LUMAX", "IFORIA", "ILIVIA")):
        device_type = "ICD"
    elif any(k in model_upper for k in ("HSM", "ENTOVIS", "EDORA", "EFFECTA")):
        device_type = "Pacemaker"
    else:
        device_type = "Unknown"

    # ── Battery ───────────────────────────────────────────────────────────────

    battery_voltage   = _as_float(_find(summary, "ACTBATTERYVOLTAGE"))
    battery_status    = _find(summary, "FU1BATTERYSTATUS")
    battery_longevity = _find(settings, "Batterie-Restkapazität", "BatteryRemainingCapacity")

    # ── Leads with channel alignment ──────────────────────────────────────────

    channels      = _find_all(settings, "Kanäle", "Channels")
    manufacturers = _find_all(settings, "Hersteller", "Manufacturer")
    models        = _find_all(settings, "Elektrodenmodell", "LeadModel")
    serials       = _find_all(settings, "Seriennummer", "SerialNumber")

    # Pacemaker XMLs sometimes use numbered keys "Kanal 1" … "Kanal 4" instead
    # of repeated "Kanäle" entries.
    if not channels and settings:
        numbered = []
        for k in range(1, 5):
            v = _find(settings, f"Kanal {k}")
            if v and v not in (".", "Unknown"):
                numbered.append(v)
        channels = numbered

    channels = _align_channels(channels, models)

    # Map per-channel measurements to flat DB columns and a leads[] list for extra.
    _IMPED_ATTR   = {"RA": "FU_RA_IMPED",   "RV": "FU_RV_IMPED",   "LV": "FU_LV_IMPED"}
    _SENSE_ATTR   = {"RA": "FU_RA_SENSING",  "RV": "FU_RV_SENSING"}
    _AMP_ATTR     = {"RA": "A_AMPLITUDE",    "RV": "V_AMPLITUDE",   "LV": "LV_AMPLITUDE"}
    _PW_ATTR      = {"RA": "A_IMPDAUER",     "RV": "V_IMPDAUER",    "LV": "LV_IMPDAUER"}

    flat: dict = {}
    leads_detail: list[dict] = []

    for i, channel in enumerate(channels):
        if not channel or channel in (".", "Unknown"):
            continue
        ch = channel.upper()

        lead: dict = {"channel": ch}
        if i < len(models) and models[i] not in ("", "."):
            lead["model"] = models[i]
        if i < len(serials) and serials[i] not in ("", "."):
            lead["serial"] = serials[i]
        if i < len(manufacturers) and manufacturers[i] not in ("", "."):
            lead["manufacturer"] = manufacturers[i]

        col = ch.lower()

        imp = _as_float(_find(summary, _IMPED_ATTR.get(ch, "")))
        if imp is not None:
            flat[f"impedance_{col}"] = imp
            lead["impedance"] = imp

        sense = _as_float(_find(summary, _SENSE_ATTR.get(ch, "")))
        if sense is not None:
            flat[f"sensing_{col}"] = sense
            lead["sensing"] = sense

        amp = _as_float(_find(summary, _AMP_ATTR.get(ch, "")))
        pw  = _as_float(_find(summary, _PW_ATTR.get(ch, "")))
        if amp is not None:
            flat[f"threshold_{col}_v"] = amp
            lead["amplitude_v"] = amp
        if pw is not None:
            lead["pulse_width_ms"] = pw

        leads_detail.append(lead)

    # ── Arrhythmia ────────────────────────────────────────────────────────────

    ataf_burden = _as_float(_find(stats, "Atriale Arrhythmielast", "Atrial Arrhythmia Burden"))

    # ── Assemble ──────────────────────────────────────────────────────────────

    result: dict = {
        "patient_id":          patient_name,
        "patient_name":        patient_name,
        "patient_dob":         dob or None,
        "device_serial":       device_serial or None,
        "device_model":        device_model or None,
        "device_type":         device_type,
        "manufacturer":        manufacturer,
        "interrogation_date":  (examination.findtext("ExaminationDate") or "").strip() or None,
        "battery_voltage":     battery_voltage,
        "battery_longevity":   battery_longevity or battery_status or None,
        "ataf_burden_pct":     ataf_burden,
        "nst_count":           nst_count if nst_count else None,
        "leads":               leads_detail if leads_detail else None,
    }
    result.update(flat)
    return {k: v for k, v in result.items() if v is not None}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _as_float(value: str | None) -> float | None:
    if not value:
        return None
    clean = value.replace(",", ".").strip().rstrip("VvΩωkKmM%")
    try:
        return float(clean)
    except ValueError:
        return None


def _align_channels(channels: list[str], models: list[str]) -> list[str]:
    """Return the channel block that best aligns with the models list.

    Biotronik XMLs can repeat the Kanäle list for historical records while the
    Elektrodenmodell list contains only the current lead set. When channels count
    is a multiple of models count, score each candidate block:
      +5 per position where both model and channel are valid (strong match)
      -5 per position where model is valid but channel is placeholder (mismatch)
      +1 if the block has at least one valid channel (tie-break)
    Pick the highest-scoring block; if no models are present, return channels as-is.
    """
    if not models or not channels:
        return channels
    if len(channels) <= len(models) or len(channels) % len(models) != 0:
        return channels

    block_size  = len(models)
    block_count = len(channels) // block_size
    best_block  = 0
    best_score  = -999

    for b in range(block_count):
        chunk = channels[b * block_size : (b + 1) * block_size]
        score = 0
        has_valid = False

        for i in range(block_size):
            model_valid = i < len(models) and models[i] not in ("", ".", "Unknown")
            chan_valid  = chunk[i] not in ("", ".", "Unknown")

            if model_valid and chan_valid:
                score += 5
            elif model_valid and not chan_valid:
                score -= 5
            elif chan_valid:
                has_valid = True

        if has_valid:
            score += 1
        if score > best_score:
            best_score = score
            best_block = b

    return channels[best_block * block_size : (best_block + 1) * block_size]


# ── Format detection and dispatch ─────────────────────────────────────────────

def detect_biotronik_xml_format(xml_data: str | bytes) -> str:
    """Return 'ieee11073' or 'biostd' based on the root element."""
    sniff = xml_data[:512] if isinstance(xml_data, bytes) else xml_data[:512].encode()
    if b"biotronik-ieee11073-export" in sniff:
        return "ieee11073"
    return "biostd"


def parse_biotronik_xml_auto(xml_data: str | bytes) -> dict:
    """Auto-detect format and parse. Preferred entry point for .xml uploads."""
    fmt = detect_biotronik_xml_format(xml_data)
    if fmt == "ieee11073":
        return parse_biotronik_ieee11073(xml_data)
    return parse_biotronik_xml(xml_data)


# ── IEEE 11073 parser ─────────────────────────────────────────────────────────

def parse_biotronik_ieee11073(xml_data: str | bytes) -> dict:
    """Parse a Biotronik IEEE 11073 export (biotronik-ieee11073-export format).

    Structure:
      biotronik-ieee11073-export > dataset > section[MDC] > section[IDC] > ...
        DEV, LEAD(s), SESS, MSMT > BATTERY / LEADCHNL_RA / LEADCHNL_RV / LEADHVCHNL
        SET > BRADY / LEADCHNL_* / TACHYTHERAPY / ZONE(s)
        STAT > BRADY / AT / TACHYTHERAPY / EPISODE(s)
        EPISODE(s)    [detailed episode list]
        ATTR > PT     [patient data]
    """
    if isinstance(xml_data, bytes):
        xml_data = xml_data.decode("utf-8", errors="replace")

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        raise ParseError(f"Biotronik IEEE11073: parse error — {e}") from e

    # ── Navigation helpers ────────────────────────────────────────────────────

    def _sec(parent: ET.Element | None, name: str) -> ET.Element | None:
        if parent is None:
            return None
        for child in parent:
            if child.tag == "section" and child.get("name") == name:
                return child
        return None

    def _secs(parent: ET.Element | None, name: str) -> list[ET.Element]:
        if parent is None:
            return []
        return [c for c in parent if c.tag == "section" and c.get("name") == name]

    def _val(section: ET.Element | None, name: str) -> str | None:
        if section is None:
            return None
        for child in section:
            if child.tag == "value" and child.get("name") == name:
                t = child.text
                return t.strip() if t else None
        return None

    def _date(raw: str | None) -> str | None:
        """Normalize 'YYYYMMDDThhmmss[+ZZ]' or 'YYYYMMDD' to 'YYYY-MM-DD'."""
        if not raw:
            return None
        m = re.match(r"(\d{4})(\d{2})(\d{2})", raw)
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

    # ── Tree navigation ───────────────────────────────────────────────────────

    dataset = root.find("dataset")
    if dataset is None:
        raise ParseError("Biotronik IEEE11073: no <dataset> element")

    mdc = _sec(dataset, "MDC")
    if mdc is None:
        raise ParseError("Biotronik IEEE11073: no MDC section")

    idc = _sec(mdc, "IDC")
    if idc is None:
        raise ParseError("Biotronik IEEE11073: no IDC section")

    # ── Device ────────────────────────────────────────────────────────────────

    dev = _sec(idc, "DEV")
    raw_type      = _val(dev, "TYPE") or ""
    device_model  = _val(dev, "MODEL") or ""
    device_serial = _val(dev, "SERIAL") or ""
    implant_date  = _date(_val(dev, "IMPLANT_DT"))

    _TYPE_MAP = {"ICD": "ICD",
                 "CRT-D": "CRT-D", "CRT_D": "CRT-D",
                 "CRT-P": "CRT-P", "CRT_P": "CRT-P",
                 "IPG": "Pacemaker", "PM": "Pacemaker"}
    device_type = _TYPE_MAP.get(raw_type.upper(), raw_type or "Unknown")

    # ── Leads ─────────────────────────────────────────────────────────────────

    # Build map channel→lead-info for merging measured values later
    leads_by_ch: dict[str, dict] = {}
    for lead_sec in _secs(idc, "LEAD"):
        loc = (_val(lead_sec, "LOCATION") or "").upper()
        entry: dict = {"channel": loc or "Unknown"}
        m_str = _val(lead_sec, "MODEL")
        s_str = _val(lead_sec, "SERIAL")
        d_str = _date(_val(lead_sec, "IMPLANT_DT"))
        if m_str:
            entry["model"] = m_str
        if s_str:
            entry["serial"] = s_str
        if d_str:
            entry["implant_date"] = d_str
        leads_by_ch[loc or "Unknown"] = entry

    # ── Session ───────────────────────────────────────────────────────────────

    sess = _sec(idc, "SESS")
    interrogation_date = _date(_val(sess, "DTM"))
    session_type       = _val(sess, "TYPE")

    # ── Measurements ──────────────────────────────────────────────────────────

    msmt = _sec(idc, "MSMT")

    bat       = _sec(msmt, "BATTERY")
    batt_v    = _as_float(_val(bat, "VOLTAGE"))
    batt_pct  = _val(bat, "REMAINING_PERCENTAGE")
    batt_stat = _val(bat, "STATUS")
    # Compose longevity string: "BOS 100%" or just the status
    if batt_pct:
        batt_longevity = f"{batt_stat} {batt_pct}%".strip() if batt_stat else f"{batt_pct}%"
    else:
        batt_longevity = batt_stat

    flat: dict = {}

    for ch in ("RA", "RV", "LV"):
        chnl = _sec(msmt, f"LEADCHNL_{ch}")
        if chnl is None:
            continue
        col  = ch.lower()
        lead = leads_by_ch.setdefault(ch, {"channel": ch})

        sense_sec = _sec(chnl, "SENSING")
        imp_sec   = _sec(chnl, "IMPEDANCE")
        thr_sec   = _sec(chnl, "PACING_THRESHOLD")

        # Intrinsic amplitude (sensing); prefer MEAN, fall back to MAX
        sense_v = _as_float(_val(sense_sec, "INTR_AMPL_MEAN") or _val(sense_sec, "INTR_AMPL_MAX"))
        if sense_v is not None:
            flat[f"sensing_{col}"] = sense_v
            lead["sensing_mv"] = sense_v

        imp_v = _as_float(_val(imp_sec, "VALUE"))
        if imp_v is not None:
            flat[f"impedance_{col}"] = imp_v
            lead["impedance_ohm"] = imp_v

        if thr_sec is not None:
            thr_amp = _as_float(_val(thr_sec, "AMPLITUDE"))
            thr_pw  = _as_float(_val(thr_sec, "PULSEWIDTH"))
            if thr_amp is not None:
                flat[f"threshold_{col}_v"] = thr_amp
                lead["threshold_v"] = thr_amp
            if thr_pw is not None:
                flat[f"threshold_{col}_pw"] = thr_pw
                lead["threshold_pw_ms"] = thr_pw

    # HV impedance (ICD shock-coil, defibrillation impedance)
    hv = _sec(msmt, "LEADHVCHNL")
    hv_imp = _as_float(_val(hv, "IMPEDANCE"))
    if hv_imp is not None:
        flat["impedance_hv"] = hv_imp

    # ── Programming settings ──────────────────────────────────────────────────

    set_sec    = _sec(idc, "SET")
    brady      = _sec(set_sec, "BRADY")
    pacing_mode = _val(brady, "MODE") or _val(brady, "VENDOR_MODE")
    base_rate   = _val(brady, "LOWRATE")
    upper_rate  = _val(brady, "MAX_TRACKING_RATE")

    # Programmed pacing output per channel
    for ch in ("RA", "RV", "LV"):
        chnl = _sec(set_sec, f"LEADCHNL_{ch}")
        pac  = _sec(chnl, "PACING")
        amp  = _as_float(_val(pac, "AMPLITUDE"))
        pw   = _as_float(_val(pac, "PULSEWIDTH"))
        if amp is not None or pw is not None:
            lead = leads_by_ch.setdefault(ch, {"channel": ch})
            if amp is not None:
                lead["programmed_amplitude_v"] = amp
            if pw is not None:
                lead["programmed_pw_ms"] = pw

    # ICD zones: VT1/VT2/VF detection intervals
    zones = []
    for z in _secs(set_sec, "ZONE"):
        zone_type   = _val(z, "VENDOR_TYPE") or _val(z, "TYPE")
        zone_status = _val(z, "STATUS")
        det_ms      = _as_float(_val(z, "DETECTION_INTERVAL"))
        energy_j    = _as_float(_val(z, "SHOCK_ENERGY_1"))
        entry: dict = {}
        if zone_type:
            entry["type"] = zone_type
        if zone_status:
            entry["status"] = zone_status
        if det_ms is not None:
            entry["detection_interval_ms"] = det_ms
            # bpm = 60000 / ms
            entry["detection_rate_bpm"] = round(60000 / det_ms)
        if energy_j is not None:
            entry["shock_energy_j"] = energy_j
        if entry:
            zones.append(entry)

    # ── Statistics ────────────────────────────────────────────────────────────

    stat_sec   = _sec(idc, "STAT")
    at_stat    = _sec(stat_sec, "AT")
    brady_stat = _sec(stat_sec, "BRADY")
    tach_stat  = _sec(stat_sec, "TACHYTHERAPY")

    ataf_burden      = _as_float(_val(at_stat, "BURDEN_PERCENT"))
    mode_sw_count    = _val(at_stat, "MODE_SW_COUNT")
    rv_paced_pct     = _val(brady_stat, "RV_PERCENT_PACED")
    ra_paced_pct     = _val(brady_stat, "RA_PERCENT_PACED")
    shocks_recent    = _val(tach_stat, "SHOCKS_DELIVERED_RECENT")
    shocks_total     = _val(tach_stat, "SHOCKS_DELIVERED_TOTAL")
    atp_total        = _val(tach_stat, "ATP_DELIVERED_TOTAL")

    # ── Patient ───────────────────────────────────────────────────────────────

    # ATTR is a direct child of IDC (not MDC)
    attr = _sec(idc, "ATTR")
    pt   = _sec(attr, "PT")
    first_name   = _val(pt, "NAME_GIVEN") or ""
    last_name    = _val(pt, "NAME_FAMILY") or ""
    dob          = _date(_val(pt, "DOB"))
    patient_name = f"{last_name}, {first_name}".strip(", ") or None

    # ── Assemble ──────────────────────────────────────────────────────────────

    leads_detail = [v for v in leads_by_ch.values() if len(v) > 1]  # skip stubs

    extra: dict = {
        "device_type":      device_type,
        "manufacturer":     "Biotronik",
        "patient_name":     patient_name,
        "patient_dob":      dob,
        "session_type":     session_type,
        "pacing_mode":      pacing_mode,
        "base_rate_bpm":    base_rate,
        "upper_rate_bpm":   upper_rate,
        "ataf_burden_pct":  ataf_burden,
        "mode_switches":    mode_sw_count,
        "rv_paced_pct":     rv_paced_pct,
        "ra_paced_pct":     ra_paced_pct,
        "shocks_recent":    shocks_recent,
        "shocks_total":     shocks_total,
        "atp_total":        atp_total,
        "zones":            zones if zones else None,
        "leads":            leads_detail if leads_detail else None,
    }

    result: dict = {
        "patient_id":          patient_name,
        "device_serial":       device_serial or None,
        "device_model":        device_model or None,
        "interrogation_date":  interrogation_date,
        "implant_date":        implant_date,
        "battery_voltage":     batt_v,
        "battery_longevity":   batt_longevity or None,
    }
    result.update(flat)
    result.update({k: v for k, v in extra.items() if v is not None})
    return {k: v for k, v in result.items() if v is not None}

    return channels[best_block * block_size : (best_block + 1) * block_size]
