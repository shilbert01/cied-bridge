/*
 * Interim print-job store on the RP2350's internal QSPI flash.
 *
 * A reserved region [JOB_OFFSET, JOB_OFFSET+JOB_MAX) is used as a linear append
 * buffer. Real firmware is ~56 KB at the bottom of flash, so a 1 MB offset is a
 * safe crude reservation for the spike; the final design writes to SD via FatFs
 * instead (no XIP hazard there).
 *
 * Flash program/erase disables XIP, so these routines briefly disable interrupts
 * around each sector commit and must run in thread context, never in an IRQ.
 */
#include "flash_store.h"
#include "hardware/flash.h"
#include "hardware/sync.h"
#include "hardware/regs/addressmap.h"   // XIP_NOCACHE_NOALLOC_BASE
#include <string.h>

#define JOB_OFFSET   (1u * 1024 * 1024)   // 1 MB into flash
#define JOB_MAX      (2u * 1024 * 1024)   // cap the job region at 2 MB
#define META_OFFSET  (JOB_OFFSET + JOB_MAX) // metadata sector just past the job region (3 MB)

static uint8_t  _sector[FLASH_SECTOR_SIZE];
static uint32_t _sector_len;
static uint32_t _flash_off;    // next flash offset to commit
static uint32_t _total;        // bytes written this job
static uint32_t _crc;          // running CRC32 (pre-final XOR)
static bool     _overflow;

// Table-less reflected CRC32 (poly 0xEDB88320). Init 0xFFFFFFFF, final ^ 0xFFFFFFFF.
static uint32_t crc32_step(uint32_t crc, const uint8_t* data, uint32_t len) {
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int k = 0; k < 8; k++) {
      crc = (crc >> 1) ^ (0xEDB88320u & (~(crc & 1u) + 1u));
    }
  }
  return crc;
}

void flash_store_begin(void) {
  _sector_len = 0;
  _flash_off  = JOB_OFFSET;
  _total      = 0;
  _crc        = 0xFFFFFFFFu;
  _overflow   = false;
}

static void commit_sector(void) {
  uint32_t ints = save_and_disable_interrupts();
  flash_range_erase(_flash_off, FLASH_SECTOR_SIZE);
  flash_range_program(_flash_off, _sector, FLASH_SECTOR_SIZE);
  restore_interrupts(ints);
  _flash_off  += FLASH_SECTOR_SIZE;
  _sector_len  = 0;
}

void flash_store_write(const uint8_t* data, uint32_t len) {
  if (_overflow) return;
  _crc    = crc32_step(_crc, data, len);
  _total += len;

  while (len) {
    if ((_flash_off - JOB_OFFSET) >= JOB_MAX) { _overflow = true; return; }
    uint32_t space = FLASH_SECTOR_SIZE - _sector_len;
    uint32_t n     = (len < space) ? len : space;
    memcpy(_sector + _sector_len, data, n);
    _sector_len += n;
    data        += n;
    len         -= n;
    if (_sector_len == FLASH_SECTOR_SIZE) commit_sector();
  }
}

uint32_t flash_store_finish(uint32_t* out_crc, bool* out_overflow) {
  if (_sector_len > 0) {
    memset(_sector + _sector_len, 0xFF, FLASH_SECTOR_SIZE - _sector_len);
    commit_sector();
  }
  if (out_crc)      *out_crc      = _crc ^ 0xFFFFFFFFu;
  if (out_overflow) *out_overflow = _overflow;
  return _total;
}

bool flash_store_verify(uint32_t total, uint32_t expect_crc) {
  // Read through the cache-bypassing alias so we see freshly programmed data.
  const uint8_t* p = flash_store_data();
  uint32_t c = crc32_step(0xFFFFFFFFu, p, total) ^ 0xFFFFFFFFu;
  return c == expect_crc;
}

const uint8_t* flash_store_data(void) {
  return (const uint8_t*) (XIP_NOCACHE_NOALLOC_BASE + JOB_OFFSET);
}

void flash_store_write_meta(const void* buf, uint32_t len) {
  if (len > FLASH_SECTOR_SIZE) len = FLASH_SECTOR_SIZE;
  memset(_sector, 0xFF, FLASH_SECTOR_SIZE);
  memcpy(_sector, buf, len);
  uint32_t ints = save_and_disable_interrupts();
  flash_range_erase(META_OFFSET, FLASH_SECTOR_SIZE);
  flash_range_program(META_OFFSET, _sector, FLASH_SECTOR_SIZE);
  restore_interrupts(ints);
}

void flash_store_read_meta(void* buf, uint32_t len) {
  const uint8_t* p = (const uint8_t*) (XIP_NOCACHE_NOALLOC_BASE + META_OFFSET);
  memcpy(buf, p, len);
}
