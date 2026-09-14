#ifndef RELAY_CONFIG_H
#define RELAY_CONFIG_H

// Relay subnet / print server — mirrors printer-relay/config/relay.conf.example
// and the Pi3 AP described in pacemaker-central-relay-spec.md (§3).
#define WIFI_SSID           "relayap"        // BROADCAST WPA3/WPA2 AP on the Pi3
#define WIFI_PASS           "RelayPrint#1"   // changeable — keep in sync with Pi3 hostapd
// WPA2-PSK. The CYW43439 supports WPA3-SAE as a CLIENT, but the AP (Pi3 Broadcom
// brcmfmac) CANNOT host SAE/PMF in AP mode (hostapd dies "key setting validation
// failed", 2026-07-26) — the AP is the limiter, so the fleet stays WPA2-PSK.
// NOTE: relayap must be UN-HIDDEN on the Pi3 — the CYW43 join fails on a hidden
// SSID (see WIFI_SCAN_BEFORE_JOIN below and the plan file).
#define WIFI_AUTH           CYW43_AUTH_WPA2_AES_PSK

// Hidden-SSID join workaround. The CYW43439 firmware's connect_async races/fails
// when joining a HIDDEN AP (confirmed: 106 failed joins on hidden relayap vs a
// clean join on a broadcast AP). wifi.c does an explicit DIRECTED active scan for
// WIFI_SSID first (like wpa_supplicant scan_ssid=1), then joins the discovered
// BSSID. Set to 0 to join directly (only works if relayap is un-hidden).
#define WIFI_SCAN_BEFORE_JOIN   1

// Optional BSSID pinning: connect DIRECTLY to a specific AP MAC instead of
// scanning for the hidden SSID. Reverted — it didn't help the flapping and it
// couples the firmware to one Pi3 (bad for a fleet). Uncomment + set the target
// Pi3's wlan0 MAC to re-enable.
// #define WIFI_BSSID          { 0xb8, 0x27, 0xeb, 0x91, 0x91, 0x7b }

#define PRINT_SERVER_IP     "10.99.0.1"      // Pi3 wlan0
#define PRINT_SERVER_PORT   515              // LPR / cups-lpd
#define PRINT_SERVER_QUEUE  "LaserPrinter"

// LPR delivery: per-attempt give-up (seconds of exponential backoff before the
// job is left pending for a later drain). Mirrors relay.conf LPR_GIVEUP_SECS,
// but shorter here since the persistent queue retries across reconnects/reboots.
#define LPR_GIVEUP_SECS         30
#define QUEUE_DRAIN_INTERVAL_MS 120000   // re-attempt a pending job this often

// NODE_ID is a PREFIX; relay_node_id() (node_id.c) appends a per-board unique
// suffix from the RP2350 flash ID, so every unit is distinct in a fleet.
#define NODE_ID             "relay-pico"

// Remote logging over WiFi — essential once the USB port is occupied by a
// programmer (the debug CDC is then unreachable). Broadcast on the relay subnet:
//   listen with:  nc -ul 9999      (on the Pi3, or any host joined to relayap)
//   remote BOOTSEL: echo -n b | nc -u -w1 <pico-ip> 9999
#define NETLOG_TARGET       "10.99.0.255"   // 10.99.0.0/24 broadcast
#define NETLOG_PORT         9999

// Debug CDC interface. 1 = composite (printer + serial log) for bench work.
// 0 = printer-only, exactly like the Pi Zero's f_printer gadget — use this when
// a picky host (e.g. Abbott Merlin) may reject an IAD/composite device.
// Overridden per-target by CMake (relay_printer vs relay_printer_nocdc).
#ifndef RELAY_DEBUG_CDC
#define RELAY_DEBUG_CDC     1
#endif

// PJL tray override — empty = leave the programmer's own tray selection alone
// (matches relay.conf.example MEDIASOURCE=). Set e.g. "TRAY2" to force a tray.
#define MEDIASOURCE         ""

// Clock: SNTP source is the print server (Pi3 runs chrony, serves stratum 10).
// Interim fixed local offset — CEST (+2). TODO: proper POSIX-TZ DST handling.
#define RELAY_UTC_OFFSET_SEC   (2 * 3600)

// SSD1306 OLED status display (optional — code runs inert if not wired).
#define OLED_ENABLED        1        // set 0 to compile the display out entirely
#define OLED_I2C            i2c0
#define OLED_SDA_PIN        4        // GP4 (board default I2C0 SDA)
#define OLED_SCL_PIN        5        // GP5 (board default I2C0 SCL)
#define OLED_I2C_ADDR       0x3C     // common SSD1306 address (0x3D on some)
#define OLED_I2C_BAUD       400000

#endif // RELAY_CONFIG_H
