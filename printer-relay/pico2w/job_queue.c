/*
 * Persistent single-slot job queue (see job_queue.h). The job bytes live in the
 * flash_store region; this module persists a small metadata record (magic,
 * state, length, CRC) in a separate flash sector so a pending job is recoverable
 * after a reboot — and only accepted if the bytes still match the stored CRC.
 */
#include "job_queue.h"
#include "flash_store.h"
#include <string.h>

#define JOBQ_MAGIC   0x504D4A51u   // 'PMJQ'
#define ST_EMPTY     0u
#define ST_PENDING   1u

typedef struct {
  uint32_t magic;
  uint32_t state;
  uint32_t len;
  uint32_t crc;
} jobq_meta_t;

static jobq_meta_t _m;

void jobq_init(void) {
  jobq_meta_t m;
  flash_store_read_meta(&m, sizeof(m));
  if (m.magic == JOBQ_MAGIC && m.state == ST_PENDING && m.len > 0 &&
      flash_store_verify(m.len, m.crc)) {
    _m = m;                       // recovered a valid pending job
    return;
  }
  memset(&_m, 0, sizeof(_m));
  _m.magic = JOBQ_MAGIC;
  _m.state = ST_EMPTY;
}

bool     jobq_has_pending(void) { return _m.state == ST_PENDING; }
uint32_t jobq_pending_len(void) { return _m.len; }

void jobq_mark_pending(uint32_t len, uint32_t crc) {
  _m.magic = JOBQ_MAGIC;
  _m.state = ST_PENDING;
  _m.len   = len;
  _m.crc   = crc;
  flash_store_write_meta(&_m, sizeof(_m));
}

void jobq_clear(void) {
  _m.magic = JOBQ_MAGIC;
  _m.state = ST_EMPTY;
  _m.len   = 0;
  _m.crc   = 0;
  flash_store_write_meta(&_m, sizeof(_m));
}
