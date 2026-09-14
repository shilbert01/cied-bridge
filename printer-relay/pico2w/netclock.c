/*
 * SNTP-backed clock. The RP2350 has no RTC, so — like the Pi Zero relay — the
 * relay gets time from the Pi3 (chrony, serving stratum 10 even when air-gapped).
 * lwIP's SNTP app calls clock_on_ntp() (wired via SNTP_SET_SYSTEM_TIME in
 * lwipopts.h) with Unix epoch seconds; we anchor it to the boot millisecond
 * counter and interpolate from there.
 */
#include "netclock.h"
#include "config.h"
#include "pico/stdlib.h"
#include "pico/cyw43_arch.h"
#include "lwip/apps/sntp.h"
#include "lwip/ip_addr.h"
#include <time.h>
#include <stdio.h>

static volatile uint32_t _epoch_base;   // Unix seconds captured at _ms_base
static volatile uint32_t _ms_base;      // to_ms_since_boot() at capture
static volatile bool     _synced;

// Called from lwIP (background context) on each SNTP update.
void clock_on_ntp(uint32_t unix_secs) {
  _epoch_base = unix_secs;
  _ms_base    = to_ms_since_boot(get_absolute_time());
  _synced     = true;
}

void clock_sntp_start(const char* server_ip) {
  ip_addr_t addr;
  if (!ipaddr_aton(server_ip, &addr)) return;
  cyw43_arch_lwip_begin();
  sntp_setoperatingmode(SNTP_OPMODE_POLL);
  sntp_setserver(0, &addr);
  sntp_init();
  cyw43_arch_lwip_end();
}

bool clock_is_synced(void) { return _synced; }

uint32_t clock_epoch(void) {
  if (!_synced) return 0;
  uint32_t elapsed = (to_ms_since_boot(get_absolute_time()) - _ms_base) / 1000u;
  return _epoch_base + elapsed;
}

void clock_local_hms(char* buf, size_t n) {
  if (!_synced) { snprintf(buf, n, "--:--:--"); return; }
  time_t t = (time_t) clock_epoch() + RELAY_UTC_OFFSET_SEC;
  struct tm tm;
  gmtime_r(&t, &tm);
  snprintf(buf, n, "%02d:%02d:%02d", tm.tm_hour, tm.tm_min, tm.tm_sec);
}
