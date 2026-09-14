/*
 * Step 5 + LPR retry/queue: capture -> tray-filter -> flash -> deliver, with a
 * persistent single-slot queue so an undelivered job survives WiFi drops and
 * reboots and is re-attempted when the print server is reachable again.
 *
 *   bulk-OUT (USB IRQ) -> RAM ring -> tray filter -> internal flash
 *   finalise on 5s idle -> mark pending -> LPR deliver_with_retry
 *   pending job: retried on WiFi-up, periodically, and on next boot
 *
 * Also: SNTP clock from the Pi3 (netclock) and an SSD1306 OLED status display
 * (oled, inert if no panel). Debug log over CDC (/dev/ttyACM0); 'b' -> BOOTSEL.
 * Interim job store is internal flash; final design uses SD/FatFs.
 */
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/bootrom.h"
#include "hardware/watchdog.h"
#include "tusb.h"
#include "printer_device.h"
#include "flash_store.h"
#include "job_filter.h"
#include "job_queue.h"
#include "wifi.h"
#include "netclock.h"
#include "netlog.h"
#include "node_id.h"
#include "lpr_client.h"
#include "oled.h"
#include "config.h"

#define JOB_IDLE_TIMEOUT_MS  5000
#define HEARTBEAT_MS         3000

static bool s_wifi_ok;

// Log to the debug CDC (when built with one) and always to the WiFi UDP sink,
// which is the only channel available once a programmer owns the USB port.
static void logline(const char* s) {
#if RELAY_DEBUG_CDC
  if (tud_cdc_connected()) {
    tud_cdc_write(s, strlen(s));
    tud_cdc_write("\r\n", 2);
    tud_cdc_write_flush();
  }
#endif
  netlog_line(s);
}

// Filter output sink: filtered bytes go straight to the flash store.
static void flash_sink(const uint8_t* data, uint32_t len, void* ctx) {
  (void) ctx;
  flash_store_write(data, len);
}

// Deliver the pending job (if any) with retry. Returns true if delivered/none.
static bool try_deliver_pending(void) {
  if (!jobq_has_pending()) return true;
  if (!(s_wifi_ok && wifi_is_up())) return false;   // keep pending until WiFi up
  logline("LPR: delivering pending job (with retry)...");
  char job_name[56];
  snprintf(job_name, sizeof(job_name), "%s_job", relay_node_id());
  bool sent = lpr_deliver_with_retry(PRINT_SERVER_IP, PRINT_SERVER_PORT,
                                     PRINT_SERVER_QUEUE, flash_store_data(),
                                     jobq_pending_len(), job_name,
                                     LPR_GIVEUP_SECS);
  if (sent) { jobq_clear(); logline("LPR: delivered"); }
  else        logline("LPR: not delivered - kept pending, will retry");
  return sent;
}

int main(void) {
  tusb_init();

  s_wifi_ok    = wifi_init();
  bool oled_ok = oled_init();
  jobq_init();

  char boot[160];
  snprintf(boot, sizeof(boot),
           "boot node=%s  cyw43_init=%s oled=%s%s  (rebooted_by_watchdog=%d)",
           relay_node_id(),
           s_wifi_ok ? "OK" : "FAILED", oled_ok ? "present" : "absent",
           jobq_has_pending() ? "  [recovered pending job]" : "",
           watchdog_caused_reboot() ? 1 : 0);
  logline(boot);

  lpr_set_logger(logline);   // so delivery reports progress over CDC + UDP
  wifi_set_logger(logline);  // verbose WiFi state/link transitions (diagnostic)

  // Hardware watchdog: a relay is meant to sit unattended in a programmer room,
  // so a firmware hang must self-recover rather than need a physical replug.
  // 8 s is comfortably above the worst-case loop stall (flash sector erase +
  // LPR pump). Fed at the top of the loop and inside the LPR wait pump.
  watchdog_enable(8000, true);

  uint32_t next_heartbeat = 0;
  uint32_t next_drain     = 0;
  bool     capturing = false;
  bool     wifi_was_up = false;
  bool     sntp_started = false;
  bool     synced_logged = false;
  uint32_t jobs = 0;
  char     last_hms[12] = "--:--:--";
  uint8_t  drain[512];

  while (true) {
    watchdog_update();
    tud_task();

    if (s_wifi_ok) {
      bool up = wifi_poll();
      if (up && !wifi_was_up) {
        netlog_init();                    // bring up the UDP log before we log
        char m[80];
        snprintf(m, sizeof(m), "WiFi up: %s (udp log -> %s:%d)",
                 wifi_ip(), NETLOG_TARGET, NETLOG_PORT);
        logline(m);
        if (!sntp_started) { clock_sntp_start(PRINT_SERVER_IP); sntp_started = true; }
        if (!capturing) try_deliver_pending();      // drain on (re)connect
      } else if (!up && wifi_was_up) {
        logline("WiFi down - reconnecting");
      }
      wifi_was_up = up;
    }

    if (!synced_logged && clock_is_synced()) {
      synced_logged = true;
      char m[48], hms[12];
      clock_local_hms(hms, sizeof(hms));
      snprintf(m, sizeof(m), "clock synced (local %s)", hms);
      logline(m);
    }

#if RELAY_DEBUG_CDC
    // 'b' over the debug CDC reboots to BOOTSEL.
    if (tud_cdc_available()) {
      uint8_t c;
      if (tud_cdc_read(&c, 1) && (c == 'b' || c == 'B')) {
        logline("rebooting to BOOTSEL...");
        tud_cdc_write_flush();
        sleep_ms(50);
        reset_usb_boot(0, 0);
      }
    }
#endif

    // 'b' over UDP does the same — the only re-flash path in the no-CDC build.
    if (netlog_reboot_requested()) {
      logline("rebooting to BOOTSEL (remote)...");
      sleep_ms(50);
      reset_usb_boot(0, 0);
    }

    // Drain the RX ring through the tray filter into the flash store.
    uint32_t n;
    while ((n = printer_rx_read(drain, sizeof(drain))) > 0) {
      if (!capturing) {
        if (jobq_has_pending())
          logline("WARN: capturing new job over an undelivered pending one");
        flash_store_begin();
        job_filter_begin(MEDIASOURCE);
        capturing = true;
        logline("job started");
        oled_banner("PRINTING");
      }
      job_filter_write(drain, n, flash_sink, NULL);
    }

    uint32_t now = to_ms_since_boot(get_absolute_time());
    uint32_t total, last_activity;
    printer_get_stats(&total, &last_activity);

    // Finalise once captured, ring drained, and host idle.
    if (capturing && printer_rx_available() == 0 &&
        (now - last_activity) >= JOB_IDLE_TIMEOUT_MS) {
      job_filter_end(flash_sink, NULL);

      uint32_t crc = 0;
      bool overflow = false;
      uint32_t stored = flash_store_finish(&crc, &overflow);
      bool verified = flash_store_verify(stored, crc);

      uint32_t pcl_rw = 0; bool pjl_inj = false;
      job_filter_stats(&pcl_rw, &pjl_inj);

      jobs++;
      clock_local_hms(last_hms, sizeof(last_hms));

      char msg[192];
      snprintf(msg, sizeof(msg),
               "job complete: rx=%lu stored=%lu crc32=%08lx flash=%s  tray[pcl=%lu pjl=%d]%s%s",
               (unsigned long) total, (unsigned long) stored, (unsigned long) crc,
               verified ? "VERIFIED" : "MISMATCH",
               (unsigned long) pcl_rw, pjl_inj ? 1 : 0,
               overflow ? "  [region-full]" : "",
               printer_rx_overflowed() ? "  [ring-overflow]" : "");
      logline(msg);

      if (verified && stored > 0) {
        jobq_mark_pending(stored, crc);   // persist before attempting delivery
        try_deliver_pending();
      } else {
        logline("job not stored intact - not queued");
      }

      printer_reset_job();
      capturing = false;
      next_heartbeat = 0;   // force an immediate status refresh
    }

    // Periodic drain of a stuck pending job (server was down at capture time).
    if (jobq_has_pending() && !capturing && s_wifi_ok && wifi_is_up() &&
        now >= next_drain) {
      try_deliver_pending();
      next_drain = to_ms_since_boot(get_absolute_time()) + QUEUE_DRAIN_INTERVAL_MS;
    }

    if (now >= next_heartbeat) {
      next_heartbeat = now + HEARTBEAT_MS;
      char clk[12];
      clock_local_hms(clk, sizeof(clk));

      char wifi_field[40];
      if (!s_wifi_ok)
        snprintf(wifi_field, sizeof(wifi_field), "NOCHIP");
      else if (wifi_is_up())
        snprintf(wifi_field, sizeof(wifi_field), "%s", wifi_ip());
      else
        snprintf(wifi_field, sizeof(wifi_field), "down(%s link=%d try=%lu)",
                 wifi_status_str(), wifi_link_status(), (unsigned long) wifi_attempts());

      char hb[144];
      snprintf(hb, sizeof(hb), "alive t=%lus usb=%s wifi=%s jobs=%lu pend=%d clk=%s",
               (unsigned long) (now / 1000),
               tud_mounted() ? "mounted" : "down",
               wifi_field,
               (unsigned long) jobs, jobq_has_pending() ? 1 : 0, clk);
      logline(hb);

      if (!capturing) {
        bool up = s_wifi_ok && wifi_is_up();
        oled_status(up, up ? wifi_ip() : "down", up, jobs, last_hms, clk);
      }
    }
  }
}
