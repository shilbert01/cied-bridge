#ifndef JOB_QUEUE_H
#define JOB_QUEUE_H

#include <stdint.h>
#include <stdbool.h>

// Persistent single-slot job queue backed by flash metadata. Remembers one
// captured-but-undelivered job (length + CRC) so it survives LPR failures and
// reboots; the job bytes live in the flash_store region. A multi-job queue
// comes with the SD/FatFs storage; this 1-deep version covers WiFi blips and
// power cycles, which is the main resilience win on internal flash.

// On boot: load metadata; a pending job is honoured only if its bytes still
// verify against the stored CRC.
void jobq_init(void);

bool     jobq_has_pending(void);
uint32_t jobq_pending_len(void);

// Mark the just-finalised job (bytes already in flash_store) as pending.
void jobq_mark_pending(uint32_t len, uint32_t crc);

// Clear the pending slot (after successful delivery).
void jobq_clear(void);

#endif // JOB_QUEUE_H
