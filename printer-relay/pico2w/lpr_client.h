#ifndef LPR_CLIENT_H
#define LPR_CLIENT_H

#include <stdint.h>
#include <stdbool.h>

// Submit one print job to a CUPS/lpd server via RFC 1179 LPR over lwIP raw TCP.
// Mirrors relay_daemon.py's lpr_send(): receive-job, data file (subcmd 3), then
// control file (subcmd 2). `data` may point at flash (XIP). Blocks until done or
// timeout, pumping the USB task meanwhile. Returns true on a fully-acked job.
// Optional log sink so delivery reports progress. Without it, delivery is silent
// and a stuck/looping send looks indistinguishable from a WiFi drop (both go
// quiet). main.c wires this to its combined CDC+UDP logger.
typedef void (*lpr_log_fn)(const char* line);
void lpr_set_logger(lpr_log_fn fn);

bool lpr_send_job(const char* server_ip, uint16_t port, const char* queue,
                  const uint8_t* data, uint32_t data_len, const char* job_name);

// Retry lpr_send_job with exponential back-off (5s→60s cap) until it succeeds or
// giveup_secs elapses. Pumps the USB task during waits so the printer stays live.
// Mirrors relay_daemon.py's deliver_with_retry. Returns true on success.
bool lpr_deliver_with_retry(const char* server_ip, uint16_t port, const char* queue,
                            const uint8_t* data, uint32_t data_len,
                            const char* job_name, uint32_t giveup_secs);

#endif // LPR_CLIENT_H
