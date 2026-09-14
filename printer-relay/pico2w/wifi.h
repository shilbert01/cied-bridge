#ifndef WIFI_H
#define WIFI_H

#include <stdbool.h>
#include <stdint.h>

// Optional log sink for verbose WiFi state/link transitions (diagnostics).
typedef void (*wifi_log_fn)(const char* line);
void wifi_set_logger(wifi_log_fn fn);

// Bring up the CYW43 in station mode. Returns false if the chip init fails
// (e.g. a non-W board). Non-blocking: call wifi_poll() from the main loop.
bool wifi_init(void);

// Kick off (or re-kick) an async association with the configured AP.
void wifi_start_connect(void);

// Advance the connection state machine; retries on drop. Call every loop.
// Returns true once associated with an IP (link up).
bool wifi_poll(void);

// True when associated with an IP address.
bool wifi_is_up(void);

// Dotted-quad IP string (valid only while wifi_is_up()); "0.0.0.0" otherwise.
const char* wifi_ip(void);

// Diagnostics for why WiFi is/ isn't up.
//   wifi_link_status()  raw cyw43_tcpip_link_status() value from the last poll
//   wifi_status_str()   short name: down/join/noip/up/fail/nonet/badauth
//   wifi_attempts()     number of association attempts started
//   wifi_init_ok()      did cyw43_arch_init() succeed at boot
int         wifi_link_status(void);
const char* wifi_status_str(void);
uint32_t    wifi_attempts(void);
bool        wifi_init_ok(void);

#endif // WIFI_H
