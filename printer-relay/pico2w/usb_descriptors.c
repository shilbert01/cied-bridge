/*
 * USB descriptors for the Pico 2W printer relay (step 2).
 *
 * Presents a composite device:
 *   - Interface 2: USB printer class (0x07), the identity a cardiac programmer
 *     talks to. Identity cloned from printer-relay/config/relay.conf.example
 *     (HP LaserJet 400 M401dn).
 *   - Interfaces 0/1: CDC ACM — a DEBUG-ONLY serial log (/dev/ttyACM0).
 *
 * NOTE: the CDC makes this an IAD/composite device, which is fine for bring-up on
 * a PC host but is NOT how a real single-function printer looks. Step 6 (real
 * programmer validation) should build a printer-only variant (drop CDC, set the
 * device class back to 0x00 like setup-gadget.sh does).
 */
#include "tusb.h"

// ── Identity (mirrors relay.conf.example) ───────────────────────────────────
#define USB_VID   0x03f0            // Hewlett-Packard
#define USB_PID   0x152a            // HP LaserJet 400 M401dn
#define USB_BCD   0x0200

enum {
  STRID_LANGID = 0,
  STRID_MANUFACTURER,
  STRID_PRODUCT,
  STRID_SERIAL,
  STRID_CDC_ITF,
};

// Printer MUST be interface 0. Linux usblp sends GET_DEVICE_ID / GET_PORT_STATUS
// with wIndex = (interface << 8), i.e. low byte 0. TinyUSB routes class requests
// by the LOW byte of wIndex, so these land on interface 0 regardless of the real
// printer interface number — putting the printer at 0 makes routing correct.
enum {
  ITF_NUM_PRINTER = 0,
#if RELAY_DEBUG_CDC
  ITF_NUM_CDC,
  ITF_NUM_CDC_DATA,
#endif
  ITF_NUM_TOTAL
};

#define EPNUM_CDC_NOTIF   0x81
#define EPNUM_CDC_OUT     0x02
#define EPNUM_CDC_IN      0x82
#define EPNUM_PRN_OUT     0x03
#define EPNUM_PRN_IN      0x83

// ── Device descriptor ───────────────────────────────────────────────────────
// Misc/IAD (0xEF/0x02/0x01) because of the CDC function.
static tusb_desc_device_t const desc_device = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = USB_BCD,
#if RELAY_DEBUG_CDC
    // Composite (printer + CDC) needs Misc/IAD at the device level.
    .bDeviceClass       = TUSB_CLASS_MISC,
    .bDeviceSubClass    = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol    = MISC_PROTOCOL_IAD,
#else
    // Printer-only: class declared at interface level, exactly as a real USB
    // printer (and as printer-relay/gadget/setup-gadget.sh configures f_printer).
    .bDeviceClass       = 0x00,
    .bDeviceSubClass    = 0x00,
    .bDeviceProtocol    = 0x00,
#endif
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor           = USB_VID,
    .idProduct          = USB_PID,
    .bcdDevice          = 0x0100,
    .iManufacturer      = STRID_MANUFACTURER,
    .iProduct           = STRID_PRODUCT,
    .iSerialNumber      = STRID_SERIAL,
    .bNumConfigurations = 0x01
};

uint8_t const* tud_descriptor_device_cb(void) {
  return (uint8_t const*) &desc_device;
}

// ── Configuration descriptor ────────────────────────────────────────────────
// Printer interface (9) + 2 bulk endpoints (2*7) = 23 bytes, hand-rolled since
// TinyUSB has no TUD_PRINTER_DESCRIPTOR macro.
#define TUD_PRINTER_DESC_LEN  (9 + 7 + 7)

#if RELAY_DEBUG_CDC
#define CONFIG_TOTAL_LEN  (TUD_CONFIG_DESC_LEN + TUD_PRINTER_DESC_LEN + TUD_CDC_DESC_LEN)
#else
#define CONFIG_TOTAL_LEN  (TUD_CONFIG_DESC_LEN + TUD_PRINTER_DESC_LEN)
#endif

static uint8_t const desc_configuration[] = {
    // Config: number, interface count, string index, total length, attr, power (mA)
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 500),

    // Printer interface FIRST (interface 0) — class 0x07, subclass 1, protocol 2 (bidirectional)
    9, TUSB_DESC_INTERFACE, ITF_NUM_PRINTER, 0, 2, TUSB_CLASS_PRINTER, 1, 2, 0,
    // Bulk OUT endpoint
    7, TUSB_DESC_ENDPOINT, EPNUM_PRN_OUT, TUSB_XFER_BULK, U16_TO_U8S_LE(64), 0,
    // Bulk IN endpoint
    7, TUSB_DESC_ENDPOINT, EPNUM_PRN_IN, TUSB_XFER_BULK, U16_TO_U8S_LE(64), 0,

#if RELAY_DEBUG_CDC
    // CDC (debug log) after the printer — IAD groups interfaces 1 & 2.
    // Args: control itf number, string idx, notif EP, notif size, data OUT, data IN, data size
    TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, STRID_CDC_ITF, EPNUM_CDC_NOTIF, 8,
                       EPNUM_CDC_OUT, EPNUM_CDC_IN, 64),
#endif
};

uint8_t const* tud_descriptor_configuration_cb(uint8_t index) {
  (void) index;
  return desc_configuration;
}

// ── String descriptors ──────────────────────────────────────────────────────
static char const* string_desc_arr[] = {
    [STRID_LANGID]       = (const char[]){0x09, 0x04}, // English (0x0409)
    [STRID_MANUFACTURER] = "Hewlett-Packard",
    [STRID_PRODUCT]      = "HP LaserJet 400 M401dn",
    [STRID_SERIAL]       = "VNH6G15554",
    [STRID_CDC_ITF]      = "Relay Debug Log",
};

static uint16_t _desc_str[32];

uint16_t const* tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
  (void) langid;
  size_t chr_count;

  if (index == STRID_LANGID) {
    memcpy(&_desc_str[1], string_desc_arr[STRID_LANGID], 2);
    chr_count = 1;
  } else {
    if (index >= sizeof(string_desc_arr) / sizeof(string_desc_arr[0])) return NULL;
    const char* str = string_desc_arr[index];
    chr_count = strlen(str);
    if (chr_count > 31) chr_count = 31;
    for (size_t i = 0; i < chr_count; i++) _desc_str[1 + i] = str[i];
  }

  _desc_str[0] = (uint16_t) ((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));
  return _desc_str;
}
