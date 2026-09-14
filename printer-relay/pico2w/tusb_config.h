#ifndef _TUSB_CONFIG_H_
#define _TUSB_CONFIG_H_

// CFG_TUSB_MCU is provided by the pico-sdk tinyusb_device cmake target.
#ifndef CFG_TUSB_OS
#define CFG_TUSB_OS             OPT_OS_PICO
#endif

#define CFG_TUSB_RHPORT0_MODE   (OPT_MODE_DEVICE | OPT_MODE_FULL_SPEED)

#define CFG_TUD_ENABLED         1
#define CFG_TUD_ENDPOINT0_SIZE  64

//------------- CLASS -------------//
// The USB printer class is not a built-in TinyUSB class — it is provided by our
// own application class driver (printer_device.c via usbd_app_driver_get_cb),
// so it has no CFG_TUD_* count. The CDC is the optional debug log; the
// printer-only build (RELAY_DEBUG_CDC=0) drops it so we present a single
// printer interface exactly like the Pi Zero's f_printer gadget.
#ifndef RELAY_DEBUG_CDC
#define RELAY_DEBUG_CDC         1
#endif
#if RELAY_DEBUG_CDC
#define CFG_TUD_CDC             1
#else
#define CFG_TUD_CDC             0
#endif
#define CFG_TUD_MSC             0
#define CFG_TUD_HID             0
#define CFG_TUD_MIDI            0
#define CFG_TUD_VENDOR          0

// CDC FIFO sizes
#define CFG_TUD_CDC_RX_BUFSIZE  256
#define CFG_TUD_CDC_TX_BUFSIZE  512
#define CFG_TUD_CDC_EP_BUFSIZE  64

#endif // _TUSB_CONFIG_H_
