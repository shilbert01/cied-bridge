#!/usr/bin/env python3
"""
Relay status display — 0.96" I2C OLED SSD1306 (128×64 px).

Physical wiring:
  GND → GND   (pin 6)
  VCC → 3.3V  (pin 1)
  SCL → GPIO3 (pin 5)  I2C1 SCL
  SDA → GPIO2 (pin 3)  I2C1 SDA

Refresh: every 60 s, or immediately on SIGUSR1.
"""

import os
import fcntl
import mmap
import struct
import subprocess
import sys
import time
import signal
import socket
import logging
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s display %(levelname)s %(message)s")
log = logging.getLogger("display")

# ── SSD1306 I2C driver ────────────────────────────────────────────────────────

I2C_DEV   = "/dev/i2c-1"
I2C_ADDR  = 0x3C        # most 0.96" OLEDs; try 0x3D if this fails
W, H      = 128, 64

I2C_SLAVE = 0x0703      # ioctl to set slave address

_CMD  = 0x00            # control byte: command stream
_DATA = 0x40            # control byte: data stream

_INIT = bytes([
    _CMD,
    0xAE,           # display off
    0xD5, 0x80,     # clock div / osc freq
    0xA8, H - 1,    # multiplex ratio
    0xD3, 0x00,     # display offset
    0x40,           # start line 0
    0x8D, 0x14,     # charge pump on
    0x20, 0x02,     # page addressing mode (required: fd is closed/reopened per page)
    0xA1,           # segment remap: col 0→SEG127 (horizontal mirror for 180° rotation)
    0xC8,           # COM scan direction: COM63 at top (vertical mirror for 180° rotation)
    0xDA, 0x12,     # COM pins (128×64)
    0x81, 0xCF,     # contrast
    0xD9, 0xF1,     # pre-charge
    0xDB, 0x40,     # VCOMH deselect level
    0xA4,           # display from RAM
    0xA6,           # normal (non-inverted)
    0x2E,           # deactivate scroll
    0xAF,           # display on
])

# BCM2835 GPIO for I2C bus recovery via /dev/mem bit-bang.
# After writing 128 GDDRAM bytes the SSD1306 holds SDA low, sticking the bus.
# 9 SCL pulses + a STOP condition via direct GPIO register access clears it.
_GPIO_BASE = 0x20200000
_PAGE_SIZE = 4096
_SDA, _SCL = 2, 3


def _fsel(m, gpio, func):
    reg = (gpio // 10) * 4
    bit = (gpio % 10) * 3
    v = struct.unpack_from('<I', m, reg)[0]
    struct.pack_into('<I', m, reg, (v & ~(7 << bit)) | ((func & 7) << bit))


def _i2c_bus_recover():
    f = open('/dev/mem', 'r+b', 0)
    try:
        m = mmap.mmap(f.fileno(), _PAGE_SIZE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE,
                      offset=_GPIO_BASE & ~(_PAGE_SIZE - 1))
        try:
            _fsel(m, _SDA, 1); _fsel(m, _SCL, 1)          # both → output
            struct.pack_into('<I', m, 0x1C, 1 << _SDA)     # SDA high
            struct.pack_into('<I', m, 0x1C, 1 << _SCL)     # SCL high
            time.sleep(0.001)
            for _ in range(9):
                struct.pack_into('<I', m, 0x28, 1 << _SCL) # SCL low
                time.sleep(0.0002)
                struct.pack_into('<I', m, 0x1C, 1 << _SCL) # SCL high
                time.sleep(0.0002)
            struct.pack_into('<I', m, 0x28, 1 << _SDA)     # SDA low (STOP setup)
            time.sleep(0.0002)
            struct.pack_into('<I', m, 0x1C, 1 << _SCL)     # SCL high
            time.sleep(0.0002)
            struct.pack_into('<I', m, 0x1C, 1 << _SDA)     # SDA high (STOP)
            time.sleep(0.001)
            _fsel(m, _SDA, 4); _fsel(m, _SCL, 4)          # both → ALT0 (I2C)
            time.sleep(0.02)
        finally:
            m.close()
    finally:
        f.close()


class SSD1306:
    def __init__(self):
        _i2c_bus_recover()                              # clean slate at startup
        self._fd = os.open(I2C_DEV, os.O_RDWR)
        fcntl.ioctl(self._fd, I2C_SLAVE, I2C_ADDR)
        # Send init WITHOUT the trailing display-on (_INIT[-1] == 0xAF). The
        # first init byte (0xAE) blanks any frame retained from the previous
        # boot, and the panel stays OFF until display() writes the first real
        # frame — so the stale status screen is never shown before the splash.
        os.write(self._fd, _INIT[:-1])
        self._on = False
        time.sleep(0.05)                                # let display settle after init
        log.info("SSD1306 init complete (addr=0x%02X, display held off)", I2C_ADDR)

    def _reopen(self):
        os.close(self._fd)
        _i2c_bus_recover()
        time.sleep(0.02)                                # extra settle time after pin mode restore
        self._fd = os.open(I2C_DEV, os.O_RDWR)
        fcntl.ioctl(self._fd, I2C_SLAVE, I2C_ADDR)

    def display(self, pil_image):
        img = pil_image.convert("1")
        px = img.load()
        buf = bytearray(W * H // 8)
        for page in range(H // 8):
            for col in range(W):
                byte = 0
                for bit in range(8):
                    if px[col, page * 8 + bit] == 0:   # black PIL pixel → lit OLED pixel
                        byte |= (1 << bit)
                buf[page * W + col] = byte

        # BCM2835 I2C bus sticks after each 128-byte GDDRAM write (SDA held low).
        # GPIO bit-bang recovery clears it before every page.
        for page in range(H // 8):
            self._reopen()
            os.write(self._fd, bytes([_CMD, 0xB0 | page, 0x00, 0x10]))
            os.write(self._fd, bytes([_DATA]) + bytes(buf[page * W:(page + 1) * W]))
        # Turn the panel on only after the first full frame is in GDDRAM, so the
        # very first thing shown is this frame (the splash) — never the frame
        # retained from the previous boot.
        if not self._on:
            self._reopen()
            os.write(self._fd, bytes([_CMD, 0xAF]))     # display on
            self._on = True
        log.info("SSD1306 frame sent")

    def clear(self):
        from PIL import Image
        self.display(Image.new("1", (W, H), 255))

    def close(self):
        os.close(self._fd)


# ── Embedded 5×7 bitmap font ─────────────────────────────────────────────────
# ASCII 32-126 (95 glyphs × 7 bytes).  Each byte: bit 4 = left column, bit 0 = right.
# No FreeType / _imagingft dependency.

_GLYPH_W    = 5   # pixels wide per glyph
_GLYPH_H    = 7   # pixels tall per glyph
_GLYPH_SP   = 1   # horizontal gap between characters
_FONT_BASE  = 32  # first ASCII codepoint stored

_FONT = (
    b'\x00\x00\x00\x00\x00\x00\x00'  # 32  ' '
    b'\x04\x04\x04\x04\x00\x04\x00'  # 33  !
    b'\x0a\x0a\x00\x00\x00\x00\x00'  # 34  "
    b'\x0a\x1f\x0a\x0a\x1f\x0a\x00'  # 35  #
    b'\x04\x0f\x14\x0e\x05\x1e\x04'  # 36  $
    b'\x18\x19\x02\x04\x09\x13\x00'  # 37  %
    b'\x0c\x12\x14\x08\x15\x12\x0d'  # 38  &
    b'\x0c\x04\x08\x00\x00\x00\x00'  # 39  '
    b'\x02\x04\x08\x08\x08\x04\x02'  # 40  (
    b'\x08\x04\x02\x02\x02\x04\x08'  # 41  )
    b'\x00\x04\x15\x0e\x15\x04\x00'  # 42  *
    b'\x00\x04\x04\x1f\x04\x04\x00'  # 43  +
    b'\x00\x00\x00\x00\x0c\x04\x08'  # 44  ,
    b'\x00\x00\x00\x1f\x00\x00\x00'  # 45  -
    b'\x00\x00\x00\x00\x00\x0c\x00'  # 46  .
    b'\x01\x02\x04\x08\x10\x00\x00'  # 47  /
    b'\x0e\x11\x13\x15\x19\x11\x0e'  # 48  0
    b'\x04\x0c\x04\x04\x04\x04\x0e'  # 49  1
    b'\x0e\x11\x01\x06\x08\x10\x1f'  # 50  2
    b'\x1e\x01\x01\x0e\x01\x01\x1e'  # 51  3
    b'\x02\x06\x0a\x12\x1f\x02\x02'  # 52  4
    b'\x1f\x10\x1e\x01\x01\x11\x0e'  # 53  5
    b'\x06\x08\x10\x1e\x11\x11\x0e'  # 54  6
    b'\x1f\x01\x02\x04\x08\x08\x08'  # 55  7
    b'\x0e\x11\x11\x0e\x11\x11\x0e'  # 56  8
    b'\x0e\x11\x11\x0f\x01\x02\x0c'  # 57  9
    b'\x00\x0c\x0c\x00\x0c\x0c\x00'  # 58  :
    b'\x00\x0c\x0c\x00\x0c\x04\x08'  # 59  ;
    b'\x02\x04\x08\x10\x08\x04\x02'  # 60  <
    b'\x00\x00\x1f\x00\x1f\x00\x00'  # 61  =
    b'\x08\x04\x02\x01\x02\x04\x08'  # 62  >
    b'\x0e\x11\x01\x06\x04\x00\x04'  # 63  ?
    b'\x0e\x11\x17\x15\x17\x10\x0e'  # 64  @
    b'\x0e\x11\x11\x1f\x11\x11\x11'  # 65  A
    b'\x1e\x11\x11\x1e\x11\x11\x1e'  # 66  B
    b'\x0e\x11\x10\x10\x10\x11\x0e'  # 67  C
    b'\x1c\x12\x11\x11\x11\x12\x1c'  # 68  D
    b'\x1f\x10\x10\x1e\x10\x10\x1f'  # 69  E
    b'\x1f\x10\x10\x1e\x10\x10\x10'  # 70  F
    b'\x0e\x11\x10\x17\x11\x11\x0e'  # 71  G
    b'\x11\x11\x11\x1f\x11\x11\x11'  # 72  H
    b'\x0e\x04\x04\x04\x04\x04\x0e'  # 73  I
    b'\x07\x02\x02\x02\x02\x12\x0c'  # 74  J
    b'\x11\x12\x14\x18\x14\x12\x11'  # 75  K
    b'\x10\x10\x10\x10\x10\x10\x1f'  # 76  L
    b'\x11\x1b\x15\x11\x11\x11\x11'  # 77  M
    b'\x11\x19\x15\x13\x11\x11\x11'  # 78  N
    b'\x0e\x11\x11\x11\x11\x11\x0e'  # 79  O
    b'\x1e\x11\x11\x1e\x10\x10\x10'  # 80  P
    b'\x0e\x11\x11\x11\x15\x12\x0d'  # 81  Q
    b'\x1e\x11\x11\x1e\x14\x12\x11'  # 82  R
    b'\x0f\x10\x10\x0e\x01\x01\x1e'  # 83  S
    b'\x1f\x04\x04\x04\x04\x04\x04'  # 84  T
    b'\x11\x11\x11\x11\x11\x11\x0e'  # 85  U
    b'\x11\x11\x11\x11\x11\x0a\x04'  # 86  V
    b'\x11\x11\x11\x15\x15\x1b\x11'  # 87  W
    b'\x11\x11\x0a\x04\x0a\x11\x11'  # 88  X
    b'\x11\x11\x0a\x04\x04\x04\x04'  # 89  Y
    b'\x1f\x01\x02\x04\x08\x10\x1f'  # 90  Z
    b'\x0e\x08\x08\x08\x08\x08\x0e'  # 91  [
    b'\x10\x08\x04\x02\x01\x00\x00'  # 92  \
    b'\x0e\x02\x02\x02\x02\x02\x0e'  # 93  ]
    b'\x04\x0a\x11\x00\x00\x00\x00'  # 94  ^
    b'\x00\x00\x00\x00\x00\x00\x1f'  # 95  _
    b'\x0c\x04\x02\x00\x00\x00\x00'  # 96  `
    b'\x00\x00\x0e\x01\x0f\x11\x0f'  # 97  a
    b'\x10\x10\x16\x19\x11\x11\x1e'  # 98  b
    b'\x00\x00\x0e\x10\x10\x11\x0e'  # 99  c
    b'\x01\x01\x0d\x13\x11\x11\x0f'  # 100 d
    b'\x00\x00\x0e\x11\x1f\x10\x0e'  # 101 e
    b'\x06\x09\x08\x1e\x08\x08\x08'  # 102 f
    b'\x00\x0f\x11\x11\x0f\x01\x0e'  # 103 g
    b'\x10\x10\x16\x19\x11\x11\x11'  # 104 h
    b'\x04\x00\x0c\x04\x04\x04\x0e'  # 105 i
    b'\x02\x00\x06\x02\x02\x12\x0c'  # 106 j
    b'\x10\x10\x12\x14\x18\x14\x12'  # 107 k
    b'\x0c\x04\x04\x04\x04\x04\x0e'  # 108 l
    b'\x00\x00\x1a\x15\x15\x11\x11'  # 109 m
    b'\x00\x00\x16\x19\x11\x11\x11'  # 110 n
    b'\x00\x00\x0e\x11\x11\x11\x0e'  # 111 o
    b'\x00\x1e\x11\x11\x1e\x10\x10'  # 112 p
    b'\x00\x0f\x11\x11\x0f\x01\x01'  # 113 q
    b'\x00\x00\x16\x19\x10\x10\x10'  # 114 r
    b'\x00\x00\x0f\x10\x0e\x01\x1e'  # 115 s
    b'\x08\x08\x1e\x08\x08\x09\x06'  # 116 t
    b'\x00\x00\x11\x11\x11\x13\x0d'  # 117 u
    b'\x00\x00\x11\x11\x11\x0a\x04'  # 118 v
    b'\x00\x00\x11\x11\x15\x1b\x11'  # 119 w
    b'\x00\x00\x11\x0a\x04\x0a\x11'  # 120 x
    b'\x00\x11\x11\x0f\x01\x11\x0e'  # 121 y
    b'\x00\x00\x1f\x02\x04\x08\x1f'  # 122 z
    b'\x06\x08\x08\x10\x08\x08\x06'  # 123 {
    b'\x04\x04\x04\x00\x04\x04\x04'  # 124 |
    b'\x0c\x02\x02\x01\x02\x02\x0c'  # 125 }
    b'\x08\x15\x02\x00\x00\x00\x00'  # 126 ~
)

_FALLBACK_GLYPH = b'\x1f\x11\x11\x11\x11\x11\x1f'  # box for unknown chars


def _blit_text(draw, pos, text, fill=0):
    """Draw text at pos using the embedded 5×7 bitmap font."""
    x, y = pos
    for ch in text:
        idx = ord(ch) - _FONT_BASE
        if 0 <= idx < len(_FONT) // _GLYPH_H:
            glyph = _FONT[idx * _GLYPH_H:(idx + 1) * _GLYPH_H]
        else:
            glyph = _FALLBACK_GLYPH
        for row, bits in enumerate(glyph):
            for col in range(_GLYPH_W):
                if bits & (1 << (_GLYPH_W - 1 - col)):
                    draw.point((x + col, y + row), fill=fill)
        x += _GLYPH_W + _GLYPH_SP


def _text_w(text, scale=1):
    """Pixel width of text in the 5×7 font at the given integer scale."""
    return (len(text) * (_GLYPH_W + _GLYPH_SP) - _GLYPH_SP) * scale


def _blit_text_scaled(draw, pos, text, scale=1, fill=0):
    """Like _blit_text but each font pixel is drawn as a scale×scale block."""
    if scale <= 1:
        _blit_text(draw, pos, text, fill=fill)
        return
    x, y = pos
    for ch in text:
        idx = ord(ch) - _FONT_BASE
        if 0 <= idx < len(_FONT) // _GLYPH_H:
            glyph = _FONT[idx * _GLYPH_H:(idx + 1) * _GLYPH_H]
        else:
            glyph = _FALLBACK_GLYPH
        for row, bits in enumerate(glyph):
            for col in range(_GLYPH_W):
                if bits & (1 << (_GLYPH_W - 1 - col)):
                    px = x + col * scale
                    py = y + row * scale
                    draw.rectangle([(px, py), (px + scale - 1, py + scale - 1)], fill=fill)
        x += (_GLYPH_W + _GLYPH_SP) * scale


# Brand shown on the boot splash. Change this string to rebrand (per-customer);
# it is device branding only — pick a name you are entitled to display.
BOOT_BRAND    = "KONSILADO"
BOOT_SUBTITLE = "Printer Relay"


def _boot_splash(oled, hold_s=2.0):
    """Show the brand splash once at startup, then hold briefly."""
    from PIL import Image, ImageDraw
    img  = Image.new("1", (W, H), 255)   # white background
    draw = ImageDraw.Draw(img)
    # Brand: largest scale that fits the width (min 1), centred.
    scale = 2
    while scale > 1 and _text_w(BOOT_BRAND, scale) > W - 4:
        scale -= 1
    bw = _text_w(BOOT_BRAND, scale)
    bx = max(0, (W - bw) // 2)
    by = (H - _GLYPH_H * scale) // 2 - 6
    _blit_text_scaled(draw, (bx, by), BOOT_BRAND, scale=scale, fill=0)
    # Subtitle at 1× under the brand.
    sw = _text_w(BOOT_SUBTITLE)
    _blit_text(draw, ((W - sw) // 2, by + _GLYPH_H * scale + 4), BOOT_SUBTITLE, fill=0)
    oled.display(img)
    time.sleep(hold_s)


# ── Status gathering ──────────────────────────────────────────────────────────

CONFIG_PATH     = "/etc/pacemaker-relay/relay.conf"
QUEUE_DIR       = "/var/lib/pacemaker-relay/queue"
SENT_DIR        = "/var/lib/pacemaker-relay/sent"
PRINT_FLAG      = "/var/run/relay_printing"
PRINT_SHOW_SECS = 10


def _load_conf() -> dict:
    conf = {}
    try:
        for line in Path(CONFIG_PATH).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip()
    except Exception:
        pass
    return conf


def _wifi_up() -> bool:
    try:
        # carrier=1 only when associated with an AP; operstate="up" fires too early
        return Path("/sys/class/net/wlan0/carrier").read_text().strip() == "1"
    except Exception:
        return False


def _relay_running() -> bool:
    try:
        pid = int(Path("/var/run/relay_daemon.pid").read_text().strip())
        Path(f"/proc/{pid}/status").read_text()
        return True
    except Exception:
        return False


def _pi3_reachable(host: str, queue: str = "LaserPrinter", port: int = 515) -> bool:
    """Print-server reachability for the OLED 'Server' line.

    Sends a valid RFC-1179 'send queue state (short)' request (command 0x03) and
    drains the reply, instead of a bare connect+close. A bare connect makes
    cups-lpd log 'Unable to get command line from client!' every probe (~1/min),
    which floods the server log and masks real job activity. A proper query is
    answered cleanly, so the probe leaves no error trail while still confirming
    the LPR service is actually serving the queue.
    """
    try:
        s = socket.create_connection((host, port), timeout=2)
        try:
            s.sendall(b"\x03" + queue.encode() + b"\n")   # 0x03 = send queue state (short)
            s.recv(64)                                     # drain reply so cups-lpd finishes cleanly
        finally:
            s.close()
        return True
    except Exception:
        return False


def _print_active() -> bool:
    try:
        age = time.time() - Path(PRINT_FLAG).stat().st_mtime
        return 0 <= age < PRINT_SHOW_SECS
    except Exception:
        return False


def _job_stats():
    try:
        uptime    = float(Path("/proc/uptime").read_text().split()[0])
        boot_time = time.time() - uptime
        files = sorted(
            (f for f in Path(SENT_DIR).iterdir() if f.stat().st_mtime >= boot_time),
            key=lambda f: f.stat().st_mtime,
        )
        count = len(files)
        last  = datetime.fromtimestamp(files[-1].stat().st_mtime + _time_offset).strftime("%H:%M") if files else None
        return count, last
    except Exception:
        return 0, None


# ── Time sync via NTP from print server ───────────────────────────────────────
# The Pi3 runs chrony with "local stratum 10" so it serves its own clock
# on port 123 even when it has no internet access.

_time_offset: float = 0.0   # seconds to add to local clock to get server time


def _sync_time(host: str) -> None:
    global _time_offset
    _NTP_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01
    req = bytearray(48)
    req[0] = 0x1B            # LI=0, VN=3, Mode=3 (client)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            t0 = time.time()
            s.sendto(bytes(req), (host, 123))
            data, _ = s.recvfrom(48)
            t1 = time.time()
        if len(data) >= 48:
            ntp_unix = struct.unpack_from("!I", data, 40)[0] - _NTP_DELTA
            _time_offset = ntp_unix - (t0 + t1) / 2
            log.debug("NTP synced from %s: offset %.1fs", host, _time_offset)
            if abs(_time_offset) > 1:
                subprocess.run(["date", "-s", f"@{int(ntp_unix)}"],
                               capture_output=True)
                _time_offset = 0.0
                log.info("System clock set from NTP: offset was %.1fs", _time_offset)
    except Exception:
        pass   # keep previous offset; local time is the fallback


def _display_time() -> datetime:
    return datetime.fromtimestamp(time.time() + _time_offset)


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render(conf: dict):
    from PIL import Image, ImageDraw

    img  = Image.new("1", (W, H), 255)   # 128×64, white background
    draw = ImageDraw.Draw(img)

    global _server_online
    wifi     = _wifi_up()
    relay    = _relay_running()
    host     = conf.get("PRINT_SERVER_HOST", "10.99.0.1")
    queue    = conf.get("PRINT_SERVER_QUEUE", "LaserPrinter")
    pi3      = _pi3_reachable(host, queue) if wifi else False
    _server_online = pi3
    if pi3:
        _sync_time(host)
    printing = _print_active()
    count, last = _job_stats()

    if printing:
        # Full-screen inverted notice — unmissable
        img  = Image.new("1", (W, H), 0)   # PIL black = all OLED pixels ON (bright white)
        draw = ImageDraw.Draw(img)
        for text, y in [("Print in", 20), ("progress", 35)]:
            x = (W - len(text) * (_GLYPH_W + _GLYPH_SP)) // 2
            _blit_text(draw, (x, y), text, fill=255)  # dark letters on white screen
        return img

    # header bar (rows 0-8)
    draw.rectangle([(0, 0), (W - 1, 8)], fill=0)
    _blit_text(draw, (2, 1), "Printer Relay", fill=255)

    # status rows (7-px glyph + 2-px gap = 9 per row)
    y = 11
    _blit_text(draw, (2,  y), "WiFi:",        fill=0)
    _blit_text(draw, (40, y), "connected" if wifi else "disconnected", fill=0)
    y += 9

    _blit_text(draw, (2,  y), "Relay:",       fill=0)
    _blit_text(draw, (40, y), "active" if relay else "stopped", fill=0)
    y += 9

    _blit_text(draw, (2,  y), "Server:",      fill=0)
    _blit_text(draw, (46, y), "online" if pi3 else "offline", fill=0)
    y += 9

    _blit_text(draw, (2,  y), "Jobs:",        fill=0)
    _blit_text(draw, (40, y), f"{count}  {last or '--'}", fill=0)

    # timestamp bottom-right (NTP-synced to print server when reachable)
    ts = _display_time().strftime("%H:%M")
    _blit_text(draw, (W - len(ts) * (_GLYPH_W + _GLYPH_SP), H - _GLYPH_H - 1), ts, fill=0)
    return img


# ── Main loop ─────────────────────────────────────────────────────────────────

_refresh_now   = False
_server_online = False


def _on_signal(sig, frame):
    global _refresh_now
    _refresh_now = True


def _apply_timezone():
    try:
        tz = Path("/etc/TZ").read_text().strip()
        if tz:
            os.environ["TZ"] = tz
            time.tzset()
            log.info("timezone: %s", tz)
    except Exception:
        pass


def main():
    global _refresh_now
    _apply_timezone()
    signal.signal(signal.SIGUSR1, _on_signal)

    log.info("initialising OLED display (%s addr=0x%02X)", I2C_DEV, I2C_ADDR)
    oled = SSD1306()
    conf = _load_conf()
    oled.clear()

    try:
        _boot_splash(oled)
    except Exception as exc:
        log.error("boot splash failed: %s", exc)

    while True:
        printing = False
        try:
            conf = _load_conf()
            printing = _print_active()
            oled.display(_render(conf))
            log.info("display refreshed")
        except Exception as exc:
            log.error("display update failed: %s", exc)

        _refresh_now = False
        interval = 60 if _server_online else 5
        for _ in range(interval):
            time.sleep(1)
            if _refresh_now:
                break
            # While showing "Print in progress", poll every second so the
            # display reverts within a second of the 10s window expiring.
            if printing and not _print_active():
                break


if __name__ == "__main__":
    main()
