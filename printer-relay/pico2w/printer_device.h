#ifndef PRINTER_DEVICE_H
#define PRINTER_DEVICE_H

#include <stdint.h>
#include <stdbool.h>

// Snapshot of the current capture state.
//   total_bytes      — bytes received on the printer bulk-OUT since last reset
//   last_activity_ms  — to_ms_since_boot() of the last received chunk (0 if none)
void printer_get_stats(uint32_t* total_bytes, uint32_t* last_activity_ms);

// Number of received bytes currently waiting in the RAM ring buffer.
uint32_t printer_rx_available(void);

// Drain up to max_len bytes from the ring into dst (consumer = main loop).
// Returns the number of bytes copied.
uint32_t printer_rx_read(uint8_t* dst, uint32_t max_len);

// True if the ring overflowed (main loop failed to keep up). Should not happen
// because bulk-OUT is USB flow-controlled, but we track it as a safety check.
bool printer_rx_overflowed(void);

// Clear the byte counter + overflow flag after a job has been finalised.
void printer_reset_job(void);

#endif // PRINTER_DEVICE_H
