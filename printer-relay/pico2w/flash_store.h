#ifndef FLASH_STORE_H
#define FLASH_STORE_H

#include <stdint.h>
#include <stdbool.h>

// Interim job store: spills a captured print job to a reserved region of the
// RP2350's internal QSPI flash (a stand-in for the SD card of the final design).
// Data is buffered a sector (4 KB) at a time and committed with interrupts off,
// so it is safe to call from thread context (NOT from the USB IRQ).

// Start a new job: reset write pointer, byte count and running CRC.
void flash_store_begin(void);

// Append received bytes. Buffers into the current sector and commits full
// sectors to flash as they fill. Silently drops once the region is full
// (flagged via the overflow out-param of flash_store_finish()).
void flash_store_write(const uint8_t* data, uint32_t len);

// Finalise: flush the last partial sector (0xFF-padded). Returns total bytes
// stored; *out_crc = CRC32 of the stored bytes; *out_overflow = region filled.
uint32_t flash_store_finish(uint32_t* out_crc, bool* out_overflow);

// Read the stored job back from flash (cache-bypassing) and check its CRC32
// against expect_crc over `total` bytes. Proves the data actually persisted.
bool flash_store_verify(uint32_t total, uint32_t expect_crc);

// Cache-bypassing pointer to the stored job bytes (for LPR delivery). Valid
// between flash_store_finish() and the next flash_store_begin().
const uint8_t* flash_store_data(void);

// Persist a small metadata blob in a dedicated flash sector (used by job_queue
// to remember a pending job across reboots). len is clamped to one sector.
void flash_store_write_meta(const void* buf, uint32_t len);
void flash_store_read_meta(void* buf, uint32_t len);

#endif // FLASH_STORE_H
