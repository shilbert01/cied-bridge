#ifndef NETCLOCK_H
#define NETCLOCK_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// SNTP-backed clock (the Pico has no RTC). Mirrors the Pi Zero's NTP-from-Pi3
// time source. Start after WiFi is up.
void clock_sntp_start(const char* server_ip);

// True once at least one SNTP response has set the time.
bool clock_is_synced(void);

// Current Unix epoch seconds (0 until synced).
uint32_t clock_epoch(void);

// Format local wall-clock "HH:MM:SS" (uses RELAY_UTC_OFFSET_SEC) into buf.
// Writes "--:--:--" until synced.
void clock_local_hms(char* buf, size_t n);

#endif // NETCLOCK_H
