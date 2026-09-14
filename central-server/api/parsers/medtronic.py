"""
Medtronic parser (Carelink/Merlin family programmers).

Field map stubs must be calibrated against real printout samples.
For each field: find the label word, then call value_right_of() or value_below()
depending on whether the value appears on the same line or one row below the label.
"""

from .base import BaseParser, Word


class MedtronicParser(BaseParser):
    vendor = "medtronic"

    # Map of output field name → label text as it appears on the printout.
    # Populate/correct these strings once you have a real Medtronic sample.
    FIELD_LABELS = {
        "patient_id":         "Patient ID",
        "device_serial":      "Serial Number",
        "device_model":       "Device Model",
        "interrogation_date": "Date",
        "battery_voltage":    "Battery Voltage",
        "battery_longevity":  "Longevity",
        "impedance_rv":       "RV Impedance",
        "impedance_ra":       "RA Impedance",
        "impedance_lv":       "LV Impedance",
        "sensing_rv":         "RV Sense",
        "sensing_ra":         "RA Sense",
        "threshold_rv_v":     "RV Threshold",
        "threshold_lv_v":     "LV Threshold",
    }

    def extract_fields(self, words: list[Word]) -> dict:
        result = {}
        for field, label in self.FIELD_LABELS.items():
            anchor = self.find_label(words, label, partial=True)
            if anchor is None:
                continue
            raw = self.value_right_of(words, anchor) or self.value_below(words, anchor)
            if raw is None:
                continue
            if field in ("battery_voltage", "impedance_rv", "impedance_ra",
                         "impedance_lv", "sensing_rv", "sensing_ra",
                         "threshold_rv_v", "threshold_lv_v"):
                result[field] = self.as_float(raw)
            else:
                result[field] = raw

        return result
