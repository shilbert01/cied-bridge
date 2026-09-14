/*
 * Streaming tray/paper-source rewrite (see job_filter.h).
 *
 * Pipeline:  input -> [prefix buffer for PJL injection] -> [PCL state machine] -> sink
 *
 * The prefix buffer holds the first PREFIX_MAX bytes so PJL MEDIASOURCE can be
 * injected before "@PJL ENTER LANGUAGE" (as in relay_daemon.py). Once the prefix
 * is flushed, subsequent bytes stream straight through the PCL rewriter.
 */
#include "job_filter.h"
#include <string.h>

#define PREFIX_MAX   512
#define PCL_HOLD_MAX 24     // ESC & l <digits...> — hold until terminator known

// ── PCL state machine ───────────────────────────────────────────────────────
enum { P_NORM, P_ESC, P_AMP, P_L, P_DIG };

static int      _pcl_state;
static uint8_t  _hold[PCL_HOLD_MAX]; // buffered "\x1b&l<digits>" while undecided
static uint32_t _hold_len;
static uint32_t _pcl_rewrites;

// ── PJL prefix buffer ───────────────────────────────────────────────────────
static const char* _mediasource;
static uint8_t  _prefix[PREFIX_MAX + 64];   // room for injected bytes
static uint32_t _prefix_len;
static bool     _prefix_done;
static bool     _pjl_injected;

static void pcl_reset(void) { _pcl_state = P_NORM; _hold_len = 0; }

// Emit one byte through the PCL rewriter to the sink.
static void pcl_byte(uint8_t b, job_sink_fn sink, void* ctx) {
  switch (_pcl_state) {
    case P_NORM:
      if (b == 0x1b) { _hold[0] = b; _hold_len = 1; _pcl_state = P_ESC; }
      else sink(&b, 1, ctx);
      return;
    case P_ESC:
      if (b == '&') { _hold[_hold_len++] = b; _pcl_state = P_AMP; }
      else { sink(_hold, _hold_len, ctx); pcl_reset(); pcl_byte(b, sink, ctx); }
      return;
    case P_AMP:
      if (b == 'l') { _hold[_hold_len++] = b; _pcl_state = P_L; }
      else { sink(_hold, _hold_len, ctx); pcl_reset(); pcl_byte(b, sink, ctx); }
      return;
    case P_L:
      if (b >= '0' && b <= '9') { _hold[_hold_len++] = b; _pcl_state = P_DIG; }
      else { sink(_hold, _hold_len, ctx); pcl_reset(); pcl_byte(b, sink, ctx); }
      return;
    case P_DIG:
      if (b == 'H' || b == 'h') {
        // Paper source command -> rewrite to source 7 (auto-select).
        uint8_t out[4] = { 0x1b, '&', 'l', '7' };
        sink(out, 4, ctx);
        sink(&b, 1, ctx);
        _pcl_rewrites++;
        pcl_reset();
      } else if (b >= '0' && b <= '9' && _hold_len < PCL_HOLD_MAX - 1) {
        _hold[_hold_len++] = b;   // keep collecting digits
      } else {
        // Some other ESC&l command (orientation, page size, ...) — pass through.
        sink(_hold, _hold_len, ctx);
        pcl_reset();
        pcl_byte(b, sink, ctx);
      }
      return;
  }
}

static void pcl_flush(job_sink_fn sink, void* ctx) {
  if (_hold_len) sink(_hold, _hold_len, ctx);
  pcl_reset();
}

static void pcl_run(const uint8_t* in, uint32_t len, job_sink_fn sink, void* ctx) {
  for (uint32_t i = 0; i < len; i++) pcl_byte(in[i], sink, ctx);
}

// ── PJL injection on the buffered prefix, then hand it to the PCL stage ──────
static void flush_prefix(job_sink_fn sink, void* ctx) {
  if (_prefix_done) return;
  _prefix_done = true;

  if (_mediasource && _mediasource[0]) {
    const char* marker = "@PJL ENTER LANGUAGE";
    // find marker in prefix
    uint32_t mlen = (uint32_t) strlen(marker);
    for (uint32_t i = 0; i + mlen <= _prefix_len; i++) {
      if (memcmp(_prefix + i, marker, mlen) == 0) {
        char inj[64];
        int ilen = 0;
        const char* p1 = "@PJL SET MEDIASOURCE=";
        for (const char* p = p1; *p; p++) inj[ilen++] = *p;
        for (const char* p = _mediasource; *p; p++) inj[ilen++] = *p;
        inj[ilen++] = '\n';
        if (i + (uint32_t) ilen + (_prefix_len - i) <= sizeof(_prefix)) {
          memmove(_prefix + i + ilen, _prefix + i, _prefix_len - i);
          memcpy(_prefix + i, inj, ilen);
          _prefix_len += ilen;
          _pjl_injected = true;
        }
        break;
      }
    }
  }
  pcl_run(_prefix, _prefix_len, sink, ctx);
}

// ── public API ──────────────────────────────────────────────────────────────
void job_filter_begin(const char* mediasource) {
  _mediasource  = mediasource;
  _prefix_len   = 0;
  _prefix_done  = false;
  _pjl_injected = false;
  _pcl_rewrites = 0;
  pcl_reset();
}

void job_filter_write(const uint8_t* in, uint32_t len, job_sink_fn sink, void* ctx) {
  if (!_prefix_done) {
    uint32_t take = PREFIX_MAX - _prefix_len;
    if (take > len) take = len;
    memcpy(_prefix + _prefix_len, in, take);
    _prefix_len += take;
    in  += take;
    len -= take;
    if (_prefix_len >= PREFIX_MAX) flush_prefix(sink, ctx);
    if (len == 0) return;
  }
  pcl_run(in, len, sink, ctx);
}

void job_filter_end(job_sink_fn sink, void* ctx) {
  if (!_prefix_done) flush_prefix(sink, ctx);
  pcl_flush(sink, ctx);
}

void job_filter_stats(uint32_t* pcl_rewrites, bool* pjl_injected) {
  if (pcl_rewrites) *pcl_rewrites = _pcl_rewrites;
  if (pjl_injected) *pjl_injected = _pjl_injected;
}
