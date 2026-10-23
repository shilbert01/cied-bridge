#!/usr/bin/env python3
"""
Export Relay status display — 0.96" I2C OLED SSD1306 (128×64 px).

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
    # 0xAF intentionally omitted — display stays off until first frame is written
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
        os.write(self._fd, _INIT)
        time.sleep(0.05)                                # let display settle after init
        log.info("SSD1306 init complete (addr=0x%02X)", I2C_ADDR)

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
        log.info("SSD1306 frame sent")

    def on(self):
        self._reopen()
        os.write(self._fd, bytes([_CMD, 0xAF]))

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

# ── Boot logo ─────────────────────────────────────────────────────────────────
# Abbott logo rendered at 128×33 px, centred in 128×64, light-on-dark polarity
# (dark PIL pixels = lit OLED pixels).  Generated from Abbott_Laboratories_logo.svg.

_BOOT_LOGO = bytes([
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x80, 0x00, 0x00, 0x00, 0x07, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x80, 0x00, 0x00, 0x00, 0x03, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x80, 0x00, 0x00, 0x00, 0x01, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x80, 0x00, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xc0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xfe, 0x0f, 0xc0, 0xff, 0x03, 0xff, 0xff, 0xff, 0x3f, 0x9f,
    0xff, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xfc, 0x0f, 0xc0, 0xff, 0x03, 0xff, 0xff, 0xfe, 0x3f, 0x8f,
    0xff, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xfc, 0x07, 0xf0, 0xff, 0xc3, 0xff, 0xff, 0xfe, 0x3f, 0x8f,
    0xff, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xfc, 0x07, 0xf0, 0xff, 0xc3, 0xff, 0xff, 0xfc, 0x3f, 0x0f,
    0xe0, 0x00, 0x00, 0x07, 0xe0, 0xff, 0xf8, 0x87, 0xf0, 0xef, 0xc3, 0x3f, 0xf9, 0xf8, 0x3e, 0x0f,
    0xc0, 0x00, 0x00, 0x07, 0xe0, 0xff, 0xf8, 0x83, 0xf0, 0x83, 0xc0, 0x0f, 0xc0, 0x70, 0x00, 0x01,
    0x80, 0x00, 0x00, 0x07, 0xe0, 0xff, 0xf9, 0x83, 0xf0, 0x01, 0xc0, 0x07, 0x84, 0x10, 0x00, 0x03,
    0x80, 0x00, 0x00, 0x07, 0xe0, 0xff, 0xf1, 0xc3, 0xf0, 0xe0, 0xc3, 0x87, 0x0e, 0x1c, 0x3e, 0x0f,
    0x83, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xf1, 0xc1, 0xf0, 0xf0, 0xc3, 0x83, 0x0e, 0x0c, 0x3e, 0x0f,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xf0, 0x01, 0xf0, 0xf0, 0xc3, 0xc2, 0x0f, 0x0c, 0x3e, 0x0f,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xe0, 0x01, 0xf0, 0xf0, 0xc3, 0xc2, 0x0f, 0x0c, 0x3e, 0x0f,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xe3, 0xe0, 0xf0, 0xf0, 0xc3, 0xc2, 0x0f, 0x0c, 0x32, 0x0c,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xe7, 0xe0, 0xf0, 0xf0, 0xc3, 0x82, 0x0e, 0x0c, 0x32, 0x0c,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xc7, 0xe0, 0xf0, 0xe0, 0xc3, 0x83, 0x0e, 0x1c, 0x33, 0x09,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0x01, 0x80, 0x30, 0x41, 0xc1, 0x07, 0x04, 0x1c, 0x03, 0x01,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0x01, 0x80, 0x31, 0x03, 0xc4, 0x0f, 0xc0, 0x3e, 0x07, 0x01,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xff, 0xff, 0xff, 0xc7, 0xff, 0x3f, 0xf1, 0xff, 0x1f, 0xc7,
    0x87, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x83, 0xff, 0xff, 0xff, 0xe0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x80, 0x00, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x80, 0x00, 0x00, 0x00, 0x01, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xc0, 0x00, 0x00, 0x00, 0x01, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xe0, 0x00, 0x00, 0x00, 0x03, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xf0, 0x00, 0x00, 0x00, 0x0f, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
])


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


# ── Status gathering ──────────────────────────────────────────────────────────

CONFIG_PATH        = "/etc/cied-export/export.conf"
EXPORT_DIR         = "/var/lib/cied-export/exports"
DISPLAY_PID_FILE   = "/var/run/display_daemon.pid"
UPLOAD_SENTINEL    = "/var/run/uploading"
RECEIVE_SENTINEL   = "/var/run/receiving"
FORWARD_COUNT_FILE = "/var/lib/cied-export/forward_count.txt"


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
        return Path("/sys/class/net/wlan0/carrier").read_text().strip() == "1"
    except Exception:
        return False


def _export_running() -> bool:
    try:
        pid = int(Path("/var/run/export_daemon.pid").read_text().strip())
        Path(f"/proc/{pid}/status").read_text()
        return True
    except Exception:
        return False


def _upload_active() -> bool:
    return Path(UPLOAD_SENTINEL).exists()


def _receiving_active() -> bool:
    return Path(RECEIVE_SENTINEL).exists()


def _pi3_reachable(host: str, port: int = 80) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def _file_stats():
    try:
        count = int(Path(FORWARD_COUNT_FILE).read_text().strip())
    except Exception:
        count = 0
    last = None
    try:
        files = [f for f in Path(EXPORT_DIR).rglob("*") if f.is_file()]
        if files:
            newest = max(files, key=lambda f: f.stat().st_mtime)
            last = datetime.fromtimestamp(
                newest.stat().st_mtime + _time_offset
            ).strftime("%H:%M")
    except Exception:
        pass
    return count, last


# ── Time sync via NTP from export server ──────────────────────────────────────

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
    export   = _export_running()
    host     = conf.get("EXPORT_SERVER_HOST", "10.99.0.1")
    pi3      = _pi3_reachable(host) if wifi else False
    _server_online = pi3
    if pi3:
        _sync_time(host)
    uploading = _upload_visible()
    count, last = _file_stats()

    if uploading:
        img  = Image.new("1", (W, H), 0)
        draw = ImageDraw.Draw(img)
        for text, y in [("Upload in", 20), ("progress", 35)]:
            x = (W - len(text) * (_GLYPH_W + _GLYPH_SP)) // 2
            _blit_text(draw, (x, y), text, fill=255)
        return img

    if _receiving_active():
        img  = Image.new("1", (W, H), 0)
        draw = ImageDraw.Draw(img)
        for text, y in [("Receiving", 20), ("file...", 35)]:
            x = (W - len(text) * (_GLYPH_W + _GLYPH_SP)) // 2
            _blit_text(draw, (x, y), text, fill=255)
        return img

    # header bar (rows 0-8)
    draw.rectangle([(0, 0), (W - 1, 8)], fill=0)
    _blit_text(draw, (2, 1), "Export Relay", fill=255)

    # status rows (7-px glyph + 2-px gap = 9 per row)
    y = 11
    _blit_text(draw, (2,  y), "WiFi:",        fill=0)
    _blit_text(draw, (40, y), "connected" if wifi else "disconnected", fill=0)
    y += 9

    _blit_text(draw, (2,  y), "Export:",      fill=0)
    _blit_text(draw, (46, y), "active" if export else "stopped", fill=0)
    y += 9

    _blit_text(draw, (2,  y), "Server:",      fill=0)
    _blit_text(draw, (46, y), "online" if pi3 else "offline", fill=0)
    y += 9

    _blit_text(draw, (2,  y), "Files:",       fill=0)
    _blit_text(draw, (40, y), f"{count}  {last or '--'}", fill=0)

    # timestamp bottom-right (NTP-synced to Pi3 when reachable)
    ts = _display_time().strftime("%H:%M")
    _blit_text(draw, (W - len(ts) * (_GLYPH_W + _GLYPH_SP), H - _GLYPH_H - 1), ts, fill=0)
    return img


# ── Main loop ─────────────────────────────────────────────────────────────────

_refresh_now      = False
_server_online    = False
_upload_show_until: float = 0.0  # monotonic time until upload screen stays visible


def _upload_visible() -> bool:
    """True while uploading OR within 5s after upload completes."""
    return _upload_active() or time.monotonic() < _upload_show_until


def _on_sigusr1(sig, frame):
    global _refresh_now
    _refresh_now = True


def _on_sigusr2(sig, frame):
    global _refresh_now, _upload_show_until
    _upload_show_until = time.monotonic() + 10  # upload starting — show screen for 10s
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
    signal.signal(signal.SIGUSR1, _on_sigusr1)
    signal.signal(signal.SIGUSR2, _on_sigusr2)
    Path(DISPLAY_PID_FILE).write_text(str(os.getpid()))

    log.info("initialising OLED display (%s addr=0x%02X)", I2C_DEV, I2C_ADDR)
    oled = SSD1306()
    conf = _load_conf()

    from PIL import Image
    _logo_px = bytearray()
    for _b in _BOOT_LOGO:
        for _i in range(8):
            _logo_px.append(255 if (_b & (0x80 >> _i)) else 0)
    oled.display(Image.frombytes("L", (W, H), bytes(_logo_px)))  # write to GDDRAM while off
    oled.on()                                                      # logo appears instantly
    time.sleep(3)
    oled.clear()

    while True:
        uploading = False
        try:
            conf = _load_conf()
            uploading = _upload_visible()
            oled.display(_render(conf))
            log.info("display refreshed")
        except Exception as exc:
            log.error("display update failed: %s", exc)

        _refresh_now = False
        receiving = _receiving_active()
        interval = 60 if _server_online else 5
        for _ in range(interval):
            time.sleep(1)
            if _refresh_now:
                break
            # Receiving state changed (file copy started or finished)
            if receiving != _receiving_active():
                break
            # Exit upload screen once hold timer expires
            if uploading and not _upload_visible():
                break


if __name__ == "__main__":
    main()
