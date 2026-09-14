"""
Boston Scientific parser.

CRT-D (LATITUDE programmer): text-based PDF DeviceFollowUpReport.
  Three-column layout (Implantation / Vorh. Sitzung / Letzte); always use Letzte (rightmost).

S-ICD EMBLEM A209 (EMBLEM programmer): HL7 v2.6 ORU^R01 — handled by hl7.py.
"""

import re
from .base import BaseParser, Word


class BostonScientificParser(BaseParser):
    vendor = "boston_scientific"

    @staticmethod
    def _infer_device_type(model: str) -> str:
        m = model.upper()
        if "CRT-D" in m:
            return "CRT-D"
        if "CRT-P" in m:
            return "CRT-P"
        if any(k in m for k in ("S-ICD", "EMBLEM", "SQ-RX")):
            return "S-ICD"
        if any(k in m for k in ("ICD", "DYNAGEN", "ORIGEN", "AUTOGEN")):
            return "ICD"
        if any(k in m for k in ("ACCOLADE", "FORMIO", "PROPONENT")):
            return "Pacemaker"
        return "Unknown"
    ROW_TOLERANCE = 3.0
    LETZTE_X = 480.0  # X threshold for the "Letzte" (current session) column

    def extract_fields(self, words: list[Word]) -> dict:
        result = {}

        # Patient name: top-left of page (~y=66, x~200)
        name_words = sorted(
            [w for w in words if 60 < w.y0 < 72 and 190 < w.x0 < 320],
            key=lambda w: w.x0,
        )
        if name_words:
            result["patient_name"] = " ".join(w.text for w in name_words)

        # DOB — pdfplumber sometimes concatenates "20Mai1955" without spaces
        dob_lbl = self.find_label(words, "Geburtsdatum")
        if dob_lbl:
            raw = self.value_right_of(words, dob_lbl)
            if raw:
                result["patient_dob"] = self._split_date_str(raw)

        # Session date: below "Praxis" (end of "Letzte Abfrage in der Praxis")
        # The date "DD Mon YYYY" is at x>420; "Dat." and "Implant." are also >420
        # but must be excluded. The Aggregat row (CRT-DG141/ etc) is at x<400.
        praxis_lbl = self.find_label(words, "Praxis")
        if praxis_lbl:
            date_words = sorted(
                [w for w in words
                 if w.page == praxis_lbl.page
                 and praxis_lbl.y1 < w.y0 < praxis_lbl.y1 + 18
                 and w.x0 > 420
                 and w.text not in ("Dat.", "Implant.")],
                key=lambda w: w.x0,
            )
            if date_words:
                result["interrogation_date"] = " ".join(w.text for w in date_words)

        # Device model and serial from "Aggregat" row; stop before right-side labels
        agg_lbl = self.find_label(words, "Aggregat")
        if agg_lbl:
            row = self.words_on_same_line(words, agg_lbl)
            dev_words = [w for w in row if w.x0 > agg_lbl.x1 + 2 and w.x0 < 380]
            for w in reversed(dev_words):
                if w.text.isdigit():
                    result["device_serial"] = w.text
                    model_parts = [x.text.rstrip("/") for x in dev_words if x is not w]
                    result["device_model"] = " ".join(p for p in model_parts if p)
                    result["device_type"] = self._infer_device_type(result["device_model"])
                    break

        # Implant date: below "Implant." label (right side header)
        implant_lbl = self.find_label(words, "Implant.")
        if implant_lbl:
            date_words = sorted(
                [w for w in words
                 if w.page == implant_lbl.page
                 and implant_lbl.y1 < w.y0 < implant_lbl.y1 + 18
                 and w.x0 > 420],
                key=lambda w: w.x0,
            )
            if date_words:
                result["implant_date"] = " ".join(w.text for w in date_words)

        # Battery
        batt_lbl = self.find_label(words, "Batterie")
        if batt_lbl:
            result["battery_status"] = self.value_right_of(words, batt_lbl) or "OK"

        explant_lbl = self.find_label(words, "Explantation:")
        if explant_lbl:
            row = self.words_on_same_line(words, explant_lbl)
            vals = [w.text for w in row if w.x0 > explant_lbl.x1 + 2]
            if vals:
                result["battery_longevity"] = " ".join(vals)

        ladezeit_lbl = self.find_label(words, "Ladezeit")
        if ladezeit_lbl:
            result["charge_time"] = self.value_right_of(words, ladezeit_lbl)

        # --- Electrode sections ---
        atrial_lbl   = self.find_label(words, "Atrial")
        rv_lbl        = self.find_label(words, "Rechtsventrikulär")
        lv_lbl        = self.find_label(words, "Linksventrikulär")
        shock_lbl_hdr = self.find_label(words, "Schockvektor")

        sections = [
            ("ra",  atrial_lbl,  rv_lbl),
            ("rv",  rv_lbl,      lv_lbl),
            ("lv",  lv_lbl,      shock_lbl_hdr),
        ]

        for key, sec_lbl, next_lbl in sections:
            if sec_lbl is None:
                continue
            y_min = sec_lbl.y0
            y_max = next_lbl.y0 if next_lbl else sec_lbl.y0 + 120
            pg    = sec_lbl.page

            sense_lbl  = self._in_range(words, "Intrins.Ampl.",      y_min, y_max, pg)
            imp_lbl    = self._in_range(words, "Stimulation-Impedanz", y_min, y_max, pg)
            thresh_lbl = self._in_range(words, "Stim.-Reizschwelle",  y_min, y_max, pg)

            if sense_lbl:
                v = self._letzte_sensing(words, sense_lbl)
                if v:
                    result[f"sensing_{key}"] = self.as_float(v)

            if imp_lbl:
                v = self._letzte_impedance(words, imp_lbl)
                if v:
                    result[f"impedance_{key}"] = self.as_float(v)

            if thresh_lbl:
                v, pw = self._letzte_threshold(words, thresh_lbl)
                if v:
                    result[f"threshold_{key}_v"] = self.as_float(v)
                if pw and key in ("rv", "lv"):
                    result[f"threshold_{key}_pw"] = self.as_float(pw)

        # Shock impedance
        shock_imp_lbl = self.find_label(words, "Schock-Impedanz")
        if shock_imp_lbl:
            v = self._letzte_impedance(words, shock_imp_lbl)
            if v:
                result["impedance_shock"] = self.as_float(v)

        # Pacing mode and base rate
        modus_lbl = self.find_label(words, "Modus")
        if modus_lbl:
            v = self.value_right_of(words, modus_lbl)
            if v:
                result["pacing_mode"] = v

        grenz_lbl = self.find_label(words, "Grenzfrequenz")
        if grenz_lbl:
            raw = self.value_right_of(words, grenz_lbl)
            if raw:
                # "60min¯¹" → strip suffix → 60
                result["base_rate_bpm"] = self.as_float(raw.split("m")[0])

        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _split_date_str(s: str) -> str:
        """Insert spaces in pdfplumber-concatenated dates like '20Mai1955' → '20 Mai 1955'."""
        s = re.sub(r"(\d)([A-Za-zäöüÄÖÜ])", r"\1 \2", s)
        s = re.sub(r"([A-Za-zäöüÄÖÜ])(\d)", r"\1 \2", s)
        return s

    def _in_range(self, words: list[Word], label: str,
                  y_min: float, y_max: float, page: int) -> Word | None:
        label_l = label.lower()
        for w in words:
            if w.page == page and y_min <= w.y0 <= y_max and w.text.lower() == label_l:
                return w
        return None

    def _letzte_group(self, words: list[Word], row_anchor: Word) -> list[Word]:
        row = self.words_on_same_line(words, row_anchor)
        return [w for w in row if w.x0 >= self.LETZTE_X]

    def _letzte_sensing(self, words: list[Word], row_anchor: Word) -> str | None:
        """First numeric word in Letzte column (e.g. '1,9mV')."""
        group = self._letzte_group(words, row_anchor)
        for w in group:
            if any(c.isdigit() for c in w.text):
                return w.text
        return None

    def _letzte_impedance(self, words: list[Word], row_anchor: Word) -> str | None:
        """Rightmost numeric word in Letzte column (e.g. '435Ω')."""
        group = self._letzte_group(words, row_anchor)
        vals = [w for w in group if any(c.isdigit() for c in w.text)]
        return vals[-1].text if vals else None

    def _letzte_threshold(self, words: list[Word],
                           row_anchor: Word) -> tuple[str | None, str | None]:
        """Voltage and pulse width from Letzte column. Pattern: V_val 'V' '@' PW_val 'ms'."""
        group = self._letzte_group(words, row_anchor)
        texts = [w.text for w in group]
        if "V" in texts:
            vi = texts.index("V")
            voltage = texts[vi - 1] if vi > 0 else None
            pw = None
            if "@" in texts:
                ai = texts.index("@")
                pw = texts[ai + 1] if ai + 1 < len(texts) else None
            return voltage, pw
        return None, None
