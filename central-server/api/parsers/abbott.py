"""
Abbott (formerly St. Jude Medical) parser.

Print path: Abbott Merlin programmer PDFs are RASTER (image-only, no text layer).
  PS → PDF via Ghostscript → pdfplumber returns zero words → ParseError raised.
  OCR support deferred until confirmed necessary on a real device.

Export path: .log files come in two formats depending on programmer software:
  - SJM 0x1C-delimited: code\x1cname\x1cvalue\x1cunit\x1c  (older Merlin)
  - Abbott coded text:  {code}{label text}{value}  (newer Accent/Quadra/Assurity)
  Both share the same numeric field codes; parse_export() auto-detects the format.
"""

import re

from .base import BaseParser, Word, ParseError


# Field codes shared across both export formats.
# Codes validated against KardiSynch (TypeScript) + original SJM log samples.
# Where two codes map to the same db_field they come from different programmer
# software generations — the last one encountered in the file wins.
_FIELD_CODES: dict[str, tuple[str, bool]] = {
    # Patient
    "2430": ("patient_name",       False),
    "2431": ("patient_dob",        False),
    "204":  ("patient_id",         False),
    # Device
    "200":  ("device_model",       False),
    "201":  ("device_model_num",   False),
    "202":  ("device_serial",      False),
    "2442": ("implant_date",       False),
    # Session / interrogation date
    "105":  ("session_timestamp",  False),
    "203":  ("interrogation_date", False),
    # Battery
    "519":  ("battery_voltage",    True),
    "533":  ("battery_longevity",  False),
    # Pacing
    "101":  ("programmer_model",   False),
    "301":  ("pacing_mode",        False),
    "2024": ("max_rate_bpm",       True),
    "306":  ("amplitude_rv_v",     True),
    "305":  ("pulse_width_rv_ms",  True),
    "1606": ("threshold_rv_v",     True),
    "1607": ("pulse_width_rv_ms",  True),
    "1610": ("threshold_ra_v",     True),
    "1611": ("pulse_width_ra_ms",  True),
    # Sensing
    "2722": ("sensing_rv",         True),
    "2224": ("sensing_ra",         True),
    "2721": ("sensing_ra",         True),
    # Impedance
    "507":  ("impedance_rv",       True),
    "512":  ("impedance_ra",       True),
    "875":  ("impedance_rv_min",   True),
    # Lead details (stored in extra JSON via insert_interrogation)
    "2468": ("lead_serial_ra",     False),
    "2470": ("lead_serial_rv",     False),
    "2457": ("lead_model_ra",      False),
    "2461": ("lead_model_rv",      False),
    "2459": ("lead_implant_date_ra", False),
    "2463": ("lead_implant_date_rv", False),
    # Diagnostics
    "848":  ("threshold_ra_mv",    True),
    "9048": ("vt_detection_bpm",   True),
    "2440": ("ejection_fraction",  True),
    "2441": ("indications",        False),
    "2682": ("pct_a_paced",        True),
}

# Human-readable label prefixes used in Abbott coded text format.
# Each line is: {code}{label}{value} with no separator between label and value.
_CODED_LABELS: dict[str, str] = {
    "2430": "Patient Name",
    "2431": "Patient Date of Birth",
    "204":  "Patient ID",
    "200":  "Device Model Name",
    "201":  "Device Model Number",
    "202":  "Device Serial Number",
    "105":  "Session Timestamp",
    "203":  "Device Last Interrogation Date and Time",
    "519":  "Unloaded Battery Voltage",
    "533":  "Longevity Estimate",
    "520":  "Battery Current",
    "507":  "RV Pacing Lead Impedance",
    "512":  "Atrial Pacing Lead Impedance",
    "2722": "Ventricular Signal Amplitude",
    "2721": "Atrial Signal Amplitude",
    "2468": "Atrial Lead Serial Number",
    "2470": "RV Lead Serial Number",
    "2457": "Model Number: SJM Atrial Lead",
    "2461": "Model Number: SJM RV Pace/Sense Lead",
    "2459": "Implant Date: Atrial Lead",
    "2463": "Implant Date: RV Lead",
    "2442": "Implant Date: Device",
    "301":  "Mode",
    "1610": "A. Capture Test Threshold Amplitude",
    "1606": "RV. Capture Test Threshold Amplitude",
    "1611": "A. Capture Test Pulse Width",
    "1607": "RV. Capture Test Pulse Width",
    "2440": "Ejection Fraction",
    "2441": "Indications for Implant: List",
}


class AbbottParser(BaseParser):
    vendor = "abbott"

    def extract_fields(self, words: list[Word]) -> dict:
        # Abbott PDFs are raster — pdfplumber extracts zero words.
        raise ParseError(
            "Abbott Merlin PDFs are raster-only (no text layer). "
            "Use the export path: obtain a .log file from the programmer via USB."
        )

    def parse_export(self, file_path: str) -> dict:
        """Parse a SJM/Abbott .log file; auto-detects delimiter format."""
        try:
            with open(file_path, "r", encoding="latin-1") as f:
                content = f.read()
        except OSError as e:
            raise ParseError(f"Cannot read export file: {e}") from e

        if "\x1c" in content:
            result = self._parse_sjm_delimited(content)
        elif self._is_coded_format(content):
            result = self._parse_coded_format(content)
        else:
            result = {}

        self._post_process(result)
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _is_coded_format(text: str) -> bool:
        """True when >70% of non-empty lines start with a numeric code + uppercase letter."""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 10:
            return False
        coded = sum(1 for l in lines if re.match(r"^\d{2,5}[A-Z]", l))
        return coded / len(lines) > 0.7

    def _parse_sjm_delimited(self, content: str) -> dict:
        """Parse 0x1C-delimited SJM format: code\x1cname\x1cvalue\x1cunit\x1c"""
        result: dict = {}
        for line in content.split("\n"):
            parts = line.split("\x1c")
            if len(parts) < 3:
                continue
            code  = parts[0].strip()
            value = parts[2].strip()
            mapping = _FIELD_CODES.get(code)
            if not mapping or not value:
                continue
            db_field, is_numeric = mapping
            if is_numeric:
                parsed = self.as_float(value)
                if parsed is not None:
                    result[db_field] = parsed
            else:
                result[db_field] = value
        return result

    def _parse_coded_format(self, content: str) -> dict:
        """Parse Abbott coded text format: {code}{label text}{value} per line."""
        result: dict = {}
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            for code, label in _CODED_LABELS.items():
                prefix = code + label
                if stripped.startswith(prefix):
                    raw_value = stripped[len(prefix):].strip()
                    mapping = _FIELD_CODES.get(code)
                    if not mapping or not raw_value:
                        break
                    db_field, is_numeric = mapping
                    if is_numeric:
                        parsed = self.as_float(raw_value)
                        if parsed is not None:
                            result[db_field] = parsed
                    elif db_field not in result:
                        result[db_field] = raw_value
                    break
        return result

    @staticmethod
    def _post_process(result: dict) -> None:
        """Promote session_timestamp → interrogation_date if no explicit date found.
        Infer device_type from model string."""
        if "session_timestamp" in result and "interrogation_date" not in result:
            result["interrogation_date"] = result["session_timestamp"]

        model = (result.get("device_model") or result.get("device_model_num") or "").upper()
        if model:
            if "CRT-D" in model or "QUADRA" in model:
                result["device_type"] = "CRT-D"
            elif "CRT-P" in model:
                result["device_type"] = "CRT-P"
            elif any(k in model for k in ("ICD", "FORTIFY", "ELLIPSE", "UNIFY")):
                result["device_type"] = "ICD"
            else:
                result["device_type"] = "Pacemaker"
