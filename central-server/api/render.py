"""
Local PS/PCL→PDF rendering.

Every ingested print job gets a viewable PDF *independent of extraction*, so the
dashboard can always show the document even when CIEDOcr is unreachable.
Export-route jobs already arrive as PDF and skip this.

Format detection:
  - PostScript: starts with %!PS-Adobe- → use gs (Ghostscript)
  - PCL/PJL:    starts with ESC%-12345X → use gpdl (GhostPDL) if available

GhostPDL (gpdl) is not included in Debian Bookworm.  To enable PCL rendering:
  1. Install ghostpdl on the host: sudo apt install ghostpdl  (Debian testing / Pi OS)
  2. Bind-mount into the container:
       volumes: ["/usr/bin/gpdl:/usr/local/bin/gpdl:ro"]
  Without gpdl, PCL jobs are stored as "pcl_only" and can still be downloaded raw.
"""
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_PCL_MAGIC = (
    b"\x1b%-12345X",   # Universal Exit Language (UEL) prefix — PCL5 / PCL6 / PJL
    b"%-12345X",        # UEL without leading ESC (some firmware omits it)
)


def is_pcl(path: str) -> bool:
    """Return True if the file looks like PCL/PJL rather than PostScript/PDF."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        return any(header.startswith(m) for m in _PCL_MAGIC)
    except OSError:
        return False


def _gpdl_available() -> bool:
    return shutil.which("gpdl") is not None or shutil.which("gpcl6") is not None


def _gpdl_cmd() -> str:
    return "gpdl" if shutil.which("gpdl") else "gpcl6"


def ps_to_pdf(ps_path: str, pdf_path: str) -> bool:
    """Render PostScript or PCL to PDF. Returns True on success."""
    Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)

    pcl = is_pcl(ps_path)
    if pcl:
        if not _gpdl_available():
            log.warning("PCL file %s cannot be rendered: gpdl/gpcl6 not installed", ps_path)
            return False
        cmd = [_gpdl_cmd(), "-dNOPAUSE", "-dBATCH", "-dSAFER", "-q",
               "-sDEVICE=pdfwrite", f"-sOutputFile={pdf_path}", ps_path]
        label = "PCL→PDF"
    else:
        cmd = ["gs", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-q",
               "-sDEVICE=pdfwrite", f"-sOutputFile={pdf_path}", ps_path]
        label = "PS→PDF"

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("%s failed for %s: %s", label, ps_path, e)
        return False
