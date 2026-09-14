"""
Biotronik parser (NEO programmer, BIOSTD report).

The PS printout (re-rendered by Ghostscript) produces a single-page summary
on the first output page containing the Nachsorge clinical data.
This page is identified dynamically by the presence of "Batterie-Restkapazität".

Coordinate system note: values here are calibrated against Ghostscript-rendered
output (150 DPI pdfwrite device), not the programmer's native export PDF.
"""

import re
from .base import BaseParser, Word, ParseError


class BiotronikParser(BaseParser):
    vendor = "biotronik"
    ROW_TOLERANCE = 3.5

    def extract_fields(self, words: list[Word]) -> dict:
        target_page = self._find_nachsorge_page(words)
        if target_page is None:
            raise ParseError(
                "Biotronik: Nachsorge summary page not found "
                "(missing 'Batterie-Restkapazität')."
            )
        pw = [w for w in words if w.page == target_page]
        return self._parse_nachsorge(pw)

    def _find_nachsorge_page(self, words: list[Word]) -> int | None:
        for w in words:
            if w.text == "Batterie-Restkapazität":
                return w.page
        return None

    @staticmethod
    def _infer_device_type(model: str) -> str:
        m = model.upper()
        if "CRT-D" in m or "HF-T" in m:
            return "CRT-D"
        if "CRT-P" in m or "HF-P" in m:
            return "CRT-P"
        if any(k in m for k in ("ICD", "DEFI", "LUMAX", "IFORIA", "ILIVIA")):
            return "ICD"
        if any(k in m for k in ("HSM", "ENTOVIS", "EDORA", "EFFECTA")):
            return "Pacemaker"
        return "Unknown"

    def _parse_nachsorge(self, words: list[Word]) -> dict:
        result: dict = {}

        # --- Header ---
        sn_lbl = self.find_label(words, "SN:")
        if sn_lbl:
            result["device_serial"] = self.value_right_of(words, sn_lbl)

        pid_lbl = self.find_label(words, "PID:")
        if pid_lbl:
            result["patient_id"] = self.value_right_of(words, pid_lbl)

        if sn_lbl:
            header_row = self.words_on_same_line(words, sn_lbl)
            model_parts = []
            for w in header_row:
                if w.text in ("(", "SN:"):
                    break
                if re.match(r"\d{2}\.\d{2}\.\d{4}", w.text):
                    continue
                if re.match(r"\d{2}:\d{2}", w.text):
                    continue
                if w.x0 > 220:
                    model_parts.append(w.text)
            if model_parts:
                result["device_model"] = " ".join(model_parts)
                result["device_type"] = self._infer_device_type(result["device_model"])

        # Session date: "Aktuelle Nachsorge: DD.MM.YYYY HH:MM"
        aktuell_lbl = self.find_label(words, "Aktuelle")
        if aktuell_lbl:
            row = self.words_on_same_line(words, aktuell_lbl)
            for i, w in enumerate(row):
                if w.text == "Nachsorge:" and i + 1 < len(row):
                    dt = row[i + 1].text
                    if i + 2 < len(row):
                        dt += " " + row[i + 2].text
                    result["interrogation_date"] = dt
                    break

        # --- Patient ---
        name_lbl = self.find_label(words, "Geburtsdatum:")
        if name_lbl:
            # Patient name is on a row above DOB at x<200
            name_row_words = [w for w in words
                              if w.y0 < name_lbl.y0
                              and w.y0 > name_lbl.y0 - 20
                              and w.x0 < 200]
            if name_row_words:
                result["patient_name"] = " ".join(
                    w.text for w in sorted(name_row_words, key=lambda x: x.x0))
            result["patient_dob"] = self.value_right_of(words, name_lbl)

        # Implant date: "Implantation:" in header
        impl_lbl = self.find_label(words, "Implantation:")
        if impl_lbl:
            result["implant_date"] = self.value_right_of(words, impl_lbl)

        # --- Battery ---
        bpct_lbl = self.find_label(words, "Batterie-Restkapazität")
        if bpct_lbl:
            v = self._row_last_value(words, bpct_lbl)
            result["battery_remaining_pct"] = self.as_float(v)

        bv_lbl = self.find_label(words, "Batteriespannung")
        if bv_lbl:
            result["battery_voltage"] = self.as_float(self._row_last_value(words, bv_lbl))

        lade_lbl = self.find_label(words, "Ladezustand")
        if lade_lbl:
            result["charge_state"] = self.value_right_of(words, lade_lbl)

        # --- Pacing mode: "Modus/Ven. Stimulation VVIR/BiV [then pacing cols]" ---
        modus_lbl = self.find_label(words, "Modus/Ven.")
        if modus_lbl:
            row = self.words_on_same_line(words, modus_lbl)
            # Mode value is first word after "Stimulation", before x=260
            candidates = [w for w in row
                          if w.x0 > modus_lbl.x1 + 2
                          and w.text not in ("Stimulation",)
                          and w.x0 < 260]
            if candidates:
                result["pacing_mode"] = min(candidates, key=lambda w: w.x0).text

        # Base rate: "Grund-/Ob. Grenzfr. [bpm] 70/-----" (combined string)
        grund_lbl = self.find_label(words, "Grund-/Ob.")
        if grund_lbl:
            row = self.words_on_same_line(words, grund_lbl)
            for w in row:
                if w.x0 > 200 and w.text[0].isdigit():
                    result["base_rate_bpm"] = self.as_float(w.text.split("/")[0])
                    break

        # --- Column data ---
        # Pacing output columns (before Testergebnisse section)
        pcol_a, pcol_rv, pcol_lv = self._find_col_below(words, "Bradykardie", 20)
        # Test result columns (below Testergebnisse)
        tcol_a, tcol_rv, tcol_lv = self._find_col_below(words, "Testergebnisse", 25)

        # Pacing output amplitudes (same row as Modus/Ven.)
        amp_lbl = self.find_label(words, "Impulsamplitude")
        if amp_lbl:
            v = self._col_value(words, amp_lbl, pcol_rv)
            if v and v != "-----":
                result["amplitude_rv_v"] = self.as_float(v)
            v = self._col_value(words, amp_lbl, pcol_lv)
            if v and v != "-----":
                result["amplitude_lv_v"] = self.as_float(v)

        # Sensing
        sense_lbl = self.find_label(words, "Wahrnehmungsamplitude")
        if sense_lbl:
            v = self._col_value(words, sense_lbl, tcol_a)
            if v:
                result["sensing_ra"] = self.as_float(v)
            v = self._col_value(words, sense_lbl, tcol_rv)
            if v:
                result["sensing_rv"] = self.as_float(v)

        # Threshold
        thresh_lbl = self.find_label(words, "Reizschwelle")
        if thresh_lbl:
            result["threshold_rv_v"] = self.as_float(
                self._col_value(words, thresh_lbl, tcol_rv))
            lv_t = self._col_value(words, thresh_lbl, tcol_lv)
            if lv_t:
                result["threshold_lv_v"] = self.as_float(lv_t)

        # Impedance
        imp_lbl = self.find_label(words, "Stimulationsimpedanz")
        if imp_lbl:
            result["impedance_ra"] = self.as_float(
                self._col_value(words, imp_lbl, tcol_a))
            result["impedance_rv"] = self.as_float(
                self._col_value(words, imp_lbl, tcol_rv))
            result["impedance_lv"] = self.as_float(
                self._col_value(words, imp_lbl, tcol_lv))

        shock_lbl = self.find_label(words, "Schockimpedanz")
        if shock_lbl:
            result["impedance_shock"] = self.as_float(
                self._col_value(words, shock_lbl, tcol_rv))

        # --- VT/VF detection (separate rows, values are above the label rows) ---
        for code, field in (("VT1", "vt1_detection_bpm"),
                            ("VT2", "vt2_detection_bpm"),
                            ("VF",  "vf_detection_bpm")):
            lbl = self.find_label(words, code)
            if lbl and lbl.x0 < 100:
                # Detection bpm is at x ∈ [100, 175], within ±8pt vertically
                nearby = [w for w in words
                          if 100 < w.x0 < 175
                          and abs(w.y_center - lbl.y_center) < 8.0
                          and any(c.isdigit() for c in w.text)
                          and "bpm" not in w.text]
                if nearby:
                    result[field] = self.as_float(
                        min(nearby, key=lambda w: abs(w.y_center - lbl.y_center)).text)

        # --- Pacing %: "Stim. A/LV/BiV/CRT [%]  -----/10/89/99" ---
        stim_lbl = self.find_label(words, "Stim.")
        if stim_lbl:
            row = self.words_on_same_line(words, stim_lbl, row_tol=4.0)
            # Find the slash-delimited token
            pct_word = next((w.text for w in row
                             if "/" in w.text
                             and w.x0 > stim_lbl.x1 + 2
                             and any(c.isdigit() for c in w.text)), None)
            if pct_word:
                parts = pct_word.split("/")  # ["-----", "10", "89", "99"]
                def _pct(s):
                    return self.as_float(s) if s != "-----" else None
                if len(parts) >= 1: result["pct_a_paced"]   = _pct(parts[0])
                if len(parts) >= 2: result["pct_lv_paced"]  = _pct(parts[1])
                if len(parts) >= 3: result["pct_biv_paced"] = _pct(parts[2])
                if len(parts) >= 4: result["pct_crt_paced"] = _pct(parts[3])

        # --- AT/AF burden ---
        ataf_lbl = self.find_label(words, "Atriale")
        if ataf_lbl and "Arrhythmielast" in [w.text for w in self.words_on_same_line(words, ataf_lbl)]:
            result["ataf_burden_pct"] = self.as_float(
                self._row_last_value(words, ataf_lbl))

        return result

    # ------------------------------------------------------------------

    def _row_last_value(self, words: list[Word], label: Word) -> str | None:
        row = self.words_on_same_line(words, label)
        candidates = [w for w in row
                      if w.x0 > label.x1 + 2
                      and not (w.text.startswith("[") and w.text.endswith("]"))]
        if not candidates:
            return None
        return max(candidates, key=lambda w: w.x0).text

    def _find_col_below(self, words: list[Word], section_label: str,
                        max_gap: float) -> tuple[float | None, float | None, float | None]:
        """Find A/RV/LV column X-centres in a row up to max_gap pts below section_label."""
        sect = self.find_label(words, section_label)
        if sect is None:
            return None, None, None
        col_words = [
            w for w in words
            if w.y0 > sect.y1
            and w.y0 < sect.y1 + max_gap
            and w.text in ("A", "RV", "LV")
            and w.x0 > 200
        ]
        col_a  = next((w.x_center for w in col_words if w.text == "A"),  None)
        col_rv = next((w.x_center for w in col_words if w.text == "RV"), None)
        col_lv = next((w.x_center for w in col_words if w.text == "LV"), None)
        return col_a, col_rv, col_lv

    def _col_value(self, words: list[Word], row_anchor: Word,
                   col_x: float | None, col_tol: float = 20.0) -> str | None:
        if col_x is None:
            return None
        same_line = self.words_on_same_line(words, row_anchor)
        candidates = [
            w for w in same_line
            if w.x0 > row_anchor.x1 + 2
            and abs(w.x_center - col_x) <= col_tol
            and not (w.text.startswith("[") and w.text.endswith("]"))
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda w: abs(w.x_center - col_x)).text

    # ------------------------------------------------------------------
    # Native export PDF parser (Biotronik BIOSTD — not GS-rendered)

    def parse_native_export_pdf(self, pdf_path: str) -> dict:
        """Parse a Biotronik BIOSTD native export PDF (multi-page, directly from programmer)."""
        words = self._extract_words(pdf_path)
        if not words:
            raise ParseError("No text extracted from Biotronik export PDF")
        # The Nachsorge detail page has "Nachsorge" as a body section header at x<100, y>80
        # (distinct from page-header "Aktuelle Nachsorge:" at y≈65 and "Letzte Nachsorge:" at y≈75)
        pages_body_nk = {w.page for w in words
                         if w.text == "Nachsorge" and w.x0 < 100 and w.y0 > 80}
        pages_impl    = {w.page for w in words if w.text == "Implantatstatus"}
        pages_test    = {w.page for w in words if w.text == "Testergebnisse"}
        shared = pages_body_nk & pages_impl & pages_test
        if not shared:
            raise ParseError("Biotronik export: Nachsorge detail page not found")
        pg = min(shared)
        pw = [w for w in words if w.page == pg]
        return self._parse_native_nachsorge(pw)

    def _parse_native_nachsorge(self, words: list[Word]) -> dict:
        result: dict = {}

        # --- Header (repeats on every page of the BIOSTD export) ---
        top = sorted([w for w in words if w.y0 < 32 and w.x0 < 300], key=lambda w: w.x0)
        if top:
            result["patient_name"] = " ".join(w.text for w in top)

        dob_lbl = self.find_label(words, "Geburtsdatum:")
        if dob_lbl:
            result["patient_dob"] = self.value_right_of(words, dob_lbl, row_tol=2.0)

        sn_lbl = self.find_label(words, "SN:")
        if sn_lbl:
            sn = self.value_right_of(words, sn_lbl, row_tol=2.0)
            if sn and sn.isdigit():
                result["device_serial"] = sn
            row = self.words_on_same_line(words, sn_lbl, row_tol=2.0)
            model_parts = [w.text for w in row if 230 < w.x0 < 293 and w.text not in ("(",)]
            if model_parts:
                result["device_model"] = " ".join(model_parts)
                result["device_type"] = self._infer_device_type(result["device_model"])

        pid_lbl = self.find_label(words, "PID:")
        if pid_lbl:
            result["patient_id"] = self.value_right_of(words, pid_lbl, row_tol=2.0)

        aktuell = self.find_label(words, "Aktuelle")
        if aktuell:
            row = self.words_on_same_line(words, aktuell, row_tol=2.0)
            in_date, parts = False, []
            for w in row:
                if w.text == "Nachsorge:":
                    in_date = True
                    continue
                if in_date and w.x0 < 230:
                    parts.append(w.text)
                elif in_date and w.x0 >= 230:
                    break
            if parts:
                result["interrogation_date"] = " ".join(parts)

        impl_hdr = next((w for w in words if w.text == "Implantation:" and w.y0 < 85), None)
        if impl_hdr:
            val = self.value_right_of(words, impl_hdr, row_tol=2.0)
            if val:
                result["implant_date"] = val

        # --- Battery ---
        bpct_lbl = self.find_label(words, "Batterie-Restkapazität")
        if bpct_lbl:
            result["battery_remaining_pct"] = self.as_float(
                self._row_last_value(words, bpct_lbl))

        bv_lbl = self.find_label(words, "Batteriespannung")
        if bv_lbl:
            result["battery_voltage"] = self.as_float(
                self._row_last_value(words, bv_lbl))

        # --- Pacing mode and base rate ---
        modus_lbl = self.find_label(words, "Modus/Ven.")
        if modus_lbl:
            row = self.words_on_same_line(words, modus_lbl)
            mode_words = sorted([w for w in row if w.x0 > 400], key=lambda w: w.x0)
            if mode_words:
                result["pacing_mode"] = mode_words[0].text

        grund_lbl = self.find_label(words, "Grund-/Ob.")
        if grund_lbl:
            row = self.words_on_same_line(words, grund_lbl)
            rate_words = [w for w in row
                          if w.x0 > 440 and w.text not in ("/", "-----")
                          and any(c.isdigit() for c in w.text)]
            if rate_words:
                result["base_rate_bpm"] = self.as_float(
                    min(rate_words, key=lambda w: w.x0).text)

        # --- Column X centres: A/RV/LV headers below Implantatstatus ---
        impl_lbl = self.find_label(words, "Implantatstatus")
        pcol_a = pcol_rv = pcol_lv = None
        if impl_lbl:
            ch = [w for w in words
                  if w.text in ("A", "RV", "LV")
                  and w.y0 > impl_lbl.y1 and w.y0 < impl_lbl.y1 + 65
                  and w.x0 > 300]
            pcol_a  = next((w.x_center for w in ch if w.text == "A"),  None)
            pcol_rv = next((w.x_center for w in ch if w.text == "RV"), None)
            pcol_lv = next((w.x_center for w in ch if w.text == "LV"), None)

        # Testergebnisse columns are on the SAME row as the section label
        test_lbl = self.find_label(words, "Testergebnisse")
        tcol_a, tcol_rv, tcol_lv = pcol_a, pcol_rv, pcol_lv
        if test_lbl:
            tr = self.words_on_same_line(words, test_lbl)
            tcol_a  = next((w.x_center for w in tr if w.text == "A"),  None) or pcol_a
            tcol_rv = next((w.x_center for w in tr if w.text == "RV"), None) or pcol_rv
            tcol_lv = next((w.x_center for w in tr if w.text == "LV"), None) or pcol_lv

        # --- Pacing output (Implantatstatus) ---
        amp_lbl = self.find_label(words, "Impulsamplitude")
        if amp_lbl:
            v = self._col_value(words, amp_lbl, pcol_rv)
            if v and v != "-----":
                result["amplitude_rv_v"] = self.as_float(v)
            v = self._col_value(words, amp_lbl, pcol_lv)
            if v and v != "-----":
                result["amplitude_lv_v"] = self.as_float(v)

        # VT1/VT2/VF appears as a single combined label "VT1/VT2/VF [bpm]"
        vt_lbl = next((w for w in words if "VT1" in w.text), None)
        if vt_lbl:
            row = self.words_on_same_line(words, vt_lbl)
            nums = sorted([w for w in row
                           if w.x0 > 400 and any(c.isdigit() for c in w.text)],
                          key=lambda w: w.x0)
            if len(nums) >= 3:
                result["vt1_detection_bpm"] = self.as_float(nums[0].text)
                result["vt_detection_bpm"]  = self.as_float(nums[1].text)
                result["vf_detection_bpm"]  = self.as_float(nums[2].text)
            elif len(nums) == 2:
                result["vt_detection_bpm"] = self.as_float(nums[0].text)
                result["vf_detection_bpm"] = self.as_float(nums[1].text)
            elif len(nums) == 1:
                result["vf_detection_bpm"] = self.as_float(nums[0].text)

        # --- Test results (Testergebnisse) ---
        sense_lbl = self.find_label(words, "Wahrnehmungsamplitude")
        if sense_lbl:
            v = self._col_value(words, sense_lbl, tcol_a)
            if v:
                result["sensing_ra"] = self.as_float(v)
            v = self._col_value(words, sense_lbl, tcol_rv)
            if v:
                result["sensing_rv"] = self.as_float(v)

        thresh_lbl = self.find_label(words, "Reizschwelle")
        if thresh_lbl:
            v = self._col_value(words, thresh_lbl, tcol_rv)
            if v:
                result["threshold_rv_v"] = self.as_float(v)
            v = self._col_value(words, thresh_lbl, tcol_lv)
            if v:
                result["threshold_lv_v"] = self.as_float(v)

        imp_lbl = self.find_label(words, "Stimulationsimpedanz")
        if imp_lbl:
            result["impedance_ra"] = self.as_float(self._col_value(words, imp_lbl, tcol_a))
            result["impedance_rv"] = self.as_float(self._col_value(words, imp_lbl, tcol_rv))
            result["impedance_lv"] = self.as_float(self._col_value(words, imp_lbl, tcol_lv))

        shock_lbl = self.find_label(words, "Schockimpedanz")
        if shock_lbl:
            v = self._col_value(words, shock_lbl, tcol_rv)
            if v:
                result["impedance_shock"] = self.as_float(v)

        # --- Pacing statistics: "Stim. A/LV/BiV/CRT [%]  -----/10/89/99" ---
        stim_lbl = self.find_label(words, "Stim.")
        if stim_lbl:
            row = self.words_on_same_line(words, stim_lbl)
            stat_words = sorted([w for w in row if w.x0 > 400], key=lambda w: w.x0)
            full = "".join(w.text for w in stat_words)
            parts = full.split("/")
            def _pct(s: str) -> float | None:
                return self.as_float(s) if s != "-----" else None
            if len(parts) >= 1: result["pct_a_paced"]   = _pct(parts[0])
            if len(parts) >= 2: result["pct_lv_paced"]  = _pct(parts[1])
            if len(parts) >= 3: result["pct_biv_paced"] = _pct(parts[2])
            if len(parts) >= 4: result["pct_crt_paced"] = _pct(parts[3])

        # --- AT/AF burden ---
        ataf_lbl = self.find_label(words, "Atriale")
        if ataf_lbl:
            result["ataf_burden_pct"] = self.as_float(
                self._row_last_value(words, ataf_lbl))

        return result
