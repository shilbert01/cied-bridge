"""
Base PostScript parser.

Pipeline: .ps → Ghostscript → .pdf → pdfplumber → [(x, y, text), ...] → vendor template.

Vendor subclasses implement extract_fields(words) where words is a list of
Word(text, x0, y0, x1, y1, page) namedtuples sorted top-to-bottom, left-to-right.
"""

import subprocess
import logging
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2


class ParseError(Exception):
    pass


class BaseParser:
    vendor: str = "unknown"

    # Tolerance in PDF points (1 pt = 1/72 inch) for "same line" Y matching.
    # Adjust per vendor once real samples are available.
    ROW_TOLERANCE: float = 4.0

    def parse(self, ps_path: str, pdf_path: str) -> dict:
        """Convert PS → PDF, extract words, run vendor template, return structured dict."""
        self._ps_to_pdf(ps_path, pdf_path)
        words = self._extract_words(pdf_path)
        if not words:
            raise ParseError("No text extracted from PDF — may be a raster-only printout")
        return self.extract_fields(words)

    def extract_fields(self, words: list[Word]) -> dict:
        """Override in vendor subclass. Return dict of structured field values."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Helpers available to subclasses

    def find_label(self, words: list[Word], label: str,
                   partial: bool = False) -> Word | None:
        """Find the first word (or phrase anchor) matching label text."""
        label_lower = label.lower()
        for w in words:
            t = w.text.lower().strip()
            if partial:
                if label_lower in t:
                    return w
            else:
                if t == label_lower:
                    return w
        return None

    def value_right_of(self, words: list[Word], anchor: Word,
                        x_gap: float = 2.0, row_tol: float | None = None) -> str | None:
        """Return text of the nearest word to the right of anchor on the same line."""
        tol = row_tol if row_tol is not None else self.ROW_TOLERANCE
        candidates = [
            w for w in words
            if w.page == anchor.page
            and abs(w.y_center - anchor.y_center) <= tol
            and w.x0 > anchor.x1 + x_gap
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda w: w.x0).text

    def value_below(self, words: list[Word], anchor: Word,
                    x_tol: float = 20.0, max_gap: float = 40.0) -> str | None:
        """Return text of the nearest word below anchor in roughly the same column."""
        candidates = [
            w for w in words
            if w.page == anchor.page
            and w.y0 > anchor.y1
            and w.y0 - anchor.y1 <= max_gap
            and abs(w.x_center - anchor.x_center) <= x_tol
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda w: w.y0).text

    def words_on_same_line(self, words: list[Word], anchor: Word,
                            row_tol: float | None = None) -> list[Word]:
        """All words on the same line as anchor, sorted left to right."""
        tol = row_tol if row_tol is not None else self.ROW_TOLERANCE
        same = [w for w in words
                if w.page == anchor.page
                and abs(w.y_center - anchor.y_center) <= tol]
        return sorted(same, key=lambda w: w.x0)

    @staticmethod
    def as_float(text: str | None) -> float | None:
        if text is None:
            return None
        # U+03A9 (Greek Omega Ω) and U+2126 (Ohm Sign Ω) both appear in PDFs
        clean = text.replace(",", ".").strip().rstrip("VvΩωkKmMΩ")
        try:
            return float(clean)
        except ValueError:
            return None

    # ------------------------------------------------------------------

    @staticmethod
    def _ps_to_pdf(ps_path: str, pdf_path: str):
        result = subprocess.run(
            [
                "gs", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                "-sDEVICE=pdfwrite",
                f"-sOutputFile={pdf_path}",
                ps_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise ParseError(f"Ghostscript failed: {result.stderr[:500]}")

    @staticmethod
    def _extract_words(pdf_path: str) -> list[Word]:
        words = []
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                for w in (page.extract_words() or []):
                    words.append(Word(
                        text=w["text"],
                        x0=w["x0"], y0=w["top"],
                        x1=w["x1"], y1=w["bottom"],
                        page=page_num,
                    ))
        # Sort top-to-bottom, left-to-right globally
        words.sort(key=lambda w: (w.page, w.y0, w.x0))
        return words
