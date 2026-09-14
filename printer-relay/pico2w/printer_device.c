/*
 * Custom TinyUSB application class driver for the USB printer class (0x07).
 *
 * TinyUSB ships no printer-class device driver, so we register one via
 * usbd_app_driver_get_cb(). It:
 *   - opens the two bulk endpoints of the printer interface,
 *   - continuously reads bulk-OUT (the raw PostScript/PCL the host prints),
 *     accumulating a byte count + last-activity timestamp,
 *   - answers the three printer class-specific requests:
 *       GET_DEVICE_ID (0x00)   -> IEEE-1284 PnP string (length-prefixed)
 *       GET_PORT_STATUS (0x01) -> 1 status byte (0x18 = Select | Not-Error)
 *       SOFT_RESET (0x02)      -> ack
 *
 * The GET_PORT_STATUS = 0x18 mirrors relay_daemon.py's _PRINTER_STATUS_READY,
 * which stops Windows usbprint.sys from aborting the job.
 */
#include "tusb.h"
#include "device/usbd_pvt.h"
#include "pico/time.h"
#include "hardware/sync.h"   // __dmb()
#include "printer_device.h"

// Printer class-specific requests (USB Printer Class spec).
#define PRINTER_REQ_GET_DEVICE_ID    0x00
#define PRINTER_REQ_GET_PORT_STATUS  0x01
#define PRINTER_REQ_SOFT_RESET       0x02

#define PRINTER_STATUS_READY         0x18  // Select (0x10) | Not-Error (0x08)

// IEEE-1284 device ID — must match PNP_STRING in relay.conf.example so the host
// identifies us as the trusted HP LaserJet.
static const char PNP_STRING[] =
    "MFG:Hewlett-Packard;MDL:HP LaserJet 400 M401dn;"
    "CMD:PJL,PCL,PCLXL,POSTSCRIPT;CLS:PRINTER;"
    "DES:HP LaserJet 400 M401dn;MEM:MEM=52MB;";

// ── capture state ───────────────────────────────────────────────────────────
static uint8_t  _rx_buf[64];
static uint8_t  _ep_out;
static uint8_t  _ep_in;
static uint8_t  _itf_num;

static volatile uint32_t _total_bytes;
static volatile uint32_t _last_activity_ms;

// SPSC ring buffer: producer = printer_drv_xfer_cb (USB IRQ), consumer =
// main loop (printer_rx_read). Power-of-two size for cheap masking.
#define RX_RING_SIZE  16384u
#define RX_RING_MASK  (RX_RING_SIZE - 1u)
static uint8_t          _ring[RX_RING_SIZE];
static volatile uint32_t _ring_head;   // consumer index (main loop writes)
static volatile uint32_t _ring_tail;   // producer index (IRQ writes)
static volatile bool     _ring_overflow;

void printer_get_stats(uint32_t* total_bytes, uint32_t* last_activity_ms) {
  if (total_bytes)     *total_bytes     = _total_bytes;
  if (last_activity_ms) *last_activity_ms = _last_activity_ms;
}

uint32_t printer_rx_available(void) {
  return _ring_tail - _ring_head;   // unsigned wrap-safe
}

uint32_t printer_rx_read(uint8_t* dst, uint32_t max_len) {
  uint32_t avail = _ring_tail - _ring_head;
  uint32_t n     = (max_len < avail) ? max_len : avail;
  for (uint32_t i = 0; i < n; i++) {
    dst[i] = _ring[(_ring_head + i) & RX_RING_MASK];
  }
  __dmb();
  _ring_head += n;
  return n;
}

bool printer_rx_overflowed(void) {
  return _ring_overflow;
}

void printer_reset_job(void) {
  _total_bytes    = 0;
  _last_activity_ms = 0;
  _ring_head      = 0;
  _ring_tail      = 0;
  _ring_overflow  = false;
}

// Push received bytes into the ring (producer, IRQ context).
static void ring_push(const uint8_t* data, uint32_t len) {
  uint32_t free = RX_RING_SIZE - (_ring_tail - _ring_head);
  if (len > free) { _ring_overflow = true; len = free; }
  for (uint32_t i = 0; i < len; i++) {
    _ring[(_ring_tail + i) & RX_RING_MASK] = data[i];
  }
  __dmb();
  _ring_tail += len;
}

// ── class driver callbacks ──────────────────────────────────────────────────
static void printer_drv_init(void) {
  _ep_out = _ep_in = 0;
  printer_reset_job();
}

static bool printer_drv_deinit(void) {
  return true;
}

static void printer_drv_reset(uint8_t rhport) {
  (void) rhport;
  _ep_out = _ep_in = 0;
}

static uint16_t printer_drv_open(uint8_t rhport, tusb_desc_interface_t const* itf_desc,
                                 uint16_t max_len) {
  // Only claim printer-class interfaces.
  TU_VERIFY(itf_desc->bInterfaceClass == TUSB_CLASS_PRINTER, 0);

  uint16_t const drv_len = sizeof(tusb_desc_interface_t) +
                           itf_desc->bNumEndpoints * sizeof(tusb_desc_endpoint_t);
  TU_ASSERT(max_len >= drv_len, 0);

  _itf_num = itf_desc->bInterfaceNumber;

  uint8_t const* p = tu_desc_next(itf_desc);
  for (uint8_t i = 0; i < itf_desc->bNumEndpoints; i++) {
    tusb_desc_endpoint_t const* ep = (tusb_desc_endpoint_t const*) p;
    TU_ASSERT(TUSB_DESC_ENDPOINT == ep->bDescriptorType, 0);
    TU_ASSERT(usbd_edpt_open(rhport, ep), 0);
    if (tu_edpt_dir(ep->bEndpointAddress) == TUSB_DIR_OUT) _ep_out = ep->bEndpointAddress;
    else                                                   _ep_in  = ep->bEndpointAddress;
    p = tu_desc_next(p);
  }

  // Arm the first bulk-OUT read.
  if (_ep_out) {
    TU_ASSERT(usbd_edpt_xfer(rhport, _ep_out, _rx_buf, sizeof(_rx_buf)), 0);
  }
  return drv_len;
}

static bool printer_drv_control_xfer_cb(uint8_t rhport, uint8_t stage,
                                        tusb_control_request_t const* request) {
  if (stage != CONTROL_STAGE_SETUP) return true;
  if (request->bmRequestType_bit.type != TUSB_REQ_TYPE_CLASS) return false;

  switch (request->bRequest) {
    case PRINTER_REQ_GET_DEVICE_ID: {
      // 2-byte big-endian length prefix (length INCLUDES the two length bytes).
      static uint8_t buf[2 + sizeof(PNP_STRING)];
      uint16_t const id_len = (uint16_t) strlen(PNP_STRING);
      uint16_t const total  = id_len + 2;
      buf[0] = (uint8_t) (total >> 8);
      buf[1] = (uint8_t) (total & 0xff);
      memcpy(&buf[2], PNP_STRING, id_len);
      return tud_control_xfer(rhport, request, buf, total);
    }

    case PRINTER_REQ_GET_PORT_STATUS: {
      static uint8_t status = PRINTER_STATUS_READY;
      return tud_control_xfer(rhport, request, &status, 1);
    }

    case PRINTER_REQ_SOFT_RESET:
      return tud_control_xfer(rhport, request, NULL, 0);

    default:
      return false;  // stall unsupported class requests
  }
}

static bool printer_drv_xfer_cb(uint8_t rhport, uint8_t ep_addr, xfer_result_t result,
                                uint32_t xferred_bytes) {
  if (ep_addr == _ep_out && result == XFER_RESULT_SUCCESS) {
    ring_push(_rx_buf, xferred_bytes);
    _total_bytes += xferred_bytes;
    _last_activity_ms = to_ms_since_boot(get_absolute_time());
    // Re-arm the next read.
    usbd_edpt_xfer(rhport, _ep_out, _rx_buf, sizeof(_rx_buf));
  }
  return true;
}

static const usbd_class_driver_t _printer_driver = {
    .name            = "printer",
    .init            = printer_drv_init,
    .deinit          = printer_drv_deinit,
    .reset           = printer_drv_reset,
    .open            = printer_drv_open,
    .control_xfer_cb = printer_drv_control_xfer_cb,
    .xfer_cb         = printer_drv_xfer_cb,
    .sof             = NULL,
};

// TinyUSB calls this to append application class drivers.
usbd_class_driver_t const* usbd_app_driver_get_cb(uint8_t* driver_count) {
  *driver_count = 1;
  return &_printer_driver;
}
