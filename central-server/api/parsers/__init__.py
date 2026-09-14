import re
from pathlib import Path

from .medtronic        import MedtronicParser
from .boston_scientific import BostonScientificParser
from .abbott           import AbbottParser
from .biotronik        import BiotronikParser
from .biotronik_xml    import parse_biotronik_xml, parse_biotronik_ieee11073, parse_biotronik_xml_auto
from .base             import BaseParser, ParseError

REGISTRY: dict[str, BaseParser] = {
    "medtronic":         MedtronicParser(),
    "boston_scientific": BostonScientificParser(),
    "abbott":            AbbottParser(),
    "biotronik":         BiotronikParser(),
}

# Export file routing: (vendor, extension) → callable(file_path) -> list[dict] | dict
def _abbott_export(path: str):
    return REGISTRY["abbott"].parse_export(path)

def _bsc_hl7(path: str):
    from .hl7 import parse_hl7_file
    return parse_hl7_file(path)

def _biotronik_pdf_export(path: str):
    return REGISTRY["biotronik"].parse_native_export_pdf(path)

def _biotronik_xml_export(path: str):
    with open(path, "rb") as f:
        data = f.read()
    return parse_biotronik_xml_auto(data)

EXPORT_PARSERS: dict[tuple[str, str], object] = {
    ("abbott",            ".log"): _abbott_export,
    ("boston_scientific", ".hl7"): _bsc_hl7,
    ("biotronik",         ".pdf"): _biotronik_pdf_export,
    ("biotronik",         ".xml"): _biotronik_xml_export,
}

# Filename patterns that identify a vendor regardless of what the edge node declared.
# Checked in order; first match wins.
_VENDOR_FILENAME_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'^BIOIEEE_', re.IGNORECASE), "biotronik"),
    (re.compile(r'^BIOSTD_', re.IGNORECASE), "biotronik"),
    (re.compile(r'^InterfaceData', re.IGNORECASE), "biotronik"),
]


def detect_export_vendor(filename: str, declared_vendor: str) -> str:
    """Return vendor inferred from filename, falling back to declared_vendor."""
    basename = Path(filename).name
    for pattern, vendor in _VENDOR_FILENAME_PATTERNS:
        if pattern.match(basename):
            return vendor
    return declared_vendor


def get_parser(vendor: str) -> BaseParser:
    try:
        return REGISTRY[vendor.lower()]
    except KeyError:
        raise ValueError(f"Unknown vendor '{vendor}'. Known: {list(REGISTRY)}")


def get_export_parser(vendor: str, ext: str):
    """Return callable(file_path)->list[dict]|dict, or None if not supported."""
    return EXPORT_PARSERS.get((vendor.lower(), ext.lower()))
