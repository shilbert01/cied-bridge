/*
 * RFC 1179 LPR client over lwIP's raw (NO_SYS) TCP API.
 *
 * A faithful port of relay_daemon.py's lpr_send(): the callback-driven TCP
 * primitives are wrapped in blocking helpers (write_all / read_ack) that pump
 * the USB task while waiting, so the printer stays live during delivery. All
 * lwIP API calls are bracketed with cyw43_arch_lwip_begin/end (threadsafe
 * background mode); callbacks run in the background context.
 */
#include "lpr_client.h"
#include "pico/stdlib.h"
#include "pico/cyw43_arch.h"
#include "hardware/watchdog.h"
#include "lwip/tcp.h"
#include "lwip/ip4_addr.h"
#include "tusb.h"
#include <string.h>
#include <stdio.h>

typedef struct {
  struct tcp_pcb*   pcb;
  volatile bool     connected;
  volatile bool     failed;
  volatile bool     remote_closed;
  volatile uint32_t rx_count;   // total bytes received
  volatile uint8_t  rx_last;    // last byte received (LPR ack is one 0x00)
} lpr_ctx_t;

// Keep USB serviced while we wait on network callbacks. lwIP/cyw43 run in the
// background context under threadsafe-background, so we just yield here.
static lpr_log_fn _log;
static uint32_t   _last_tick_ms;

void lpr_set_logger(lpr_log_fn fn) { _log = fn; }

static void logmsg(const char* s) { if (_log) _log(s); }

static void pump(void) {
  watchdog_update();   // a long delivery must not trip the main-loop watchdog
  tud_task();
  // Liveness during blocking waits (connect / ack / large send): without this a
  // slow phase would emit nothing for up to a minute and look like a hang.
  if (_log) {
    uint32_t now = to_ms_since_boot(get_absolute_time());
    if (now - _last_tick_ms >= 3000) { _last_tick_ms = now; _log("LPR: ...working"); }
  }
  sleep_us(200);
}

// ── TCP callbacks (run in background context) ───────────────────────────────
static err_t cb_connected(void* arg, struct tcp_pcb* pcb, err_t err) {
  (void) pcb;
  lpr_ctx_t* c = (lpr_ctx_t*) arg;
  if (err == ERR_OK) c->connected = true; else c->failed = true;
  return ERR_OK;
}

static void cb_err(void* arg, err_t err) {
  (void) err;
  lpr_ctx_t* c = (lpr_ctx_t*) arg;
  c->failed = true;
  c->pcb    = NULL;   // pcb is already freed by lwIP on error
}

static err_t cb_recv(void* arg, struct tcp_pcb* pcb, struct pbuf* p, err_t err) {
  (void) err;
  lpr_ctx_t* c = (lpr_ctx_t*) arg;
  if (!p) { c->remote_closed = true; return ERR_OK; }   // remote closed
  for (struct pbuf* q = p; q; q = q->next) {
    const uint8_t* b = (const uint8_t*) q->payload;
    if (q->len) c->rx_last = b[q->len - 1];
    c->rx_count += q->len;
  }
  tcp_recved(pcb, p->tot_len);
  pbuf_free(p);
  return ERR_OK;
}

static err_t cb_sent(void* arg, struct tcp_pcb* pcb, u16_t len) {
  (void) arg; (void) pcb; (void) len;
  return ERR_OK;
}

// ── blocking helpers ────────────────────────────────────────────────────────
static bool wait_connected(lpr_ctx_t* c, uint32_t timeout_ms) {
  absolute_time_t dl = make_timeout_time_ms(timeout_ms);
  while (!c->connected) {
    if (c->failed) return false;
    if (time_reached(dl)) return false;
    pump();
  }
  return true;
}

static bool write_all(lpr_ctx_t* c, const uint8_t* buf, uint32_t len, uint32_t timeout_ms) {
  uint32_t off = 0;
  absolute_time_t dl = make_timeout_time_ms(timeout_ms);
  while (off < len) {
    if (c->failed) return false;
    if (time_reached(dl)) return false;

    err_t e = ERR_OK;
    cyw43_arch_lwip_begin();
    uint32_t snd = tcp_sndbuf(c->pcb);
    uint32_t n   = len - off;
    if (n > snd) n = snd;
    if (n > 0) {
      u8_t flags = TCP_WRITE_FLAG_COPY | ((off + n < len) ? TCP_WRITE_FLAG_MORE : 0);
      e = tcp_write(c->pcb, buf + off, (u16_t) n, flags);
      if (e == ERR_OK) { tcp_output(c->pcb); off += n; }
    }
    cyw43_arch_lwip_end();

    if (e != ERR_OK && e != ERR_MEM) return false;   // ERR_MEM: pump & retry
    pump();
  }
  return true;
}

// Wait for exactly one more received byte and return true iff it is 0x00 (ack).
static bool read_ack(lpr_ctx_t* c, uint32_t timeout_ms) {
  uint32_t want = c->rx_count + 1;
  absolute_time_t dl = make_timeout_time_ms(timeout_ms);
  while (c->rx_count < want) {
    if (c->failed || c->remote_closed) return false;
    if (time_reached(dl)) return false;
    pump();
  }
  return c->rx_last == 0x00;
}

// ── public API ──────────────────────────────────────────────────────────────
bool lpr_send_job(const char* server_ip, uint16_t port, const char* queue,
                  const uint8_t* data, uint32_t data_len, const char* job_name) {
  static lpr_ctx_t ctx;
  memset(&ctx, 0, sizeof(ctx));

  ip_addr_t ipaddr;
  if (!ip4addr_aton(server_ip, &ipaddr)) return false;

  cyw43_arch_lwip_begin();
  ctx.pcb = tcp_new_ip_type(IPADDR_TYPE_V4);
  if (ctx.pcb) {
    tcp_arg(ctx.pcb,  &ctx);
    tcp_err(ctx.pcb,  cb_err);
    tcp_recv(ctx.pcb, cb_recv);
    tcp_sent(ctx.pcb, cb_sent);
  }
  err_t ce = ctx.pcb ? tcp_connect(ctx.pcb, &ipaddr, port, cb_connected) : ERR_MEM;
  cyw43_arch_lwip_end();
  if (!ctx.pcb || ce != ERR_OK) return false;

  const char* host = "relay";
  const char* jid  = "001";
  char data_fn[32], ctrl_fn[32], cmd[128], ctrl[192];
  snprintf(data_fn, sizeof(data_fn), "dfA%s%s", jid, host);
  snprintf(ctrl_fn, sizeof(ctrl_fn), "cfA%s%s", jid, host);

  bool ok = false;
  do {
    if (!wait_connected(&ctx, 10000)) { logmsg("LPR:   connect failed/timeout"); break; }
    logmsg("LPR:   connected");

    // Receive-a-printer-job for <queue>.
    int len = snprintf(cmd, sizeof(cmd), "\x02%s\n", queue);
    if (!write_all(&ctx, (const uint8_t*) cmd, len, 10000)) break;
    if (!read_ack(&ctx, 10000)) break;

    // Data file (subcmd 3): "\x03<len> dfA...\n"; ack; <data>; \0; ack
    len = snprintf(cmd, sizeof(cmd), "\x03%lu %s\n", (unsigned long) data_len, data_fn);
    if (!write_all(&ctx, (const uint8_t*) cmd, len, 10000)) break;
    if (!read_ack(&ctx, 10000)) break;
    if (!write_all(&ctx, data, data_len, 60000)) break;
    uint8_t zero = 0;
    if (!write_all(&ctx, &zero, 1, 10000)) break;
    if (!read_ack(&ctx, 15000)) break;

    // Control file (subcmd 2): "\x02<len> cfA...\n"; ack; <ctrl>; \0; ack
    int clen = snprintf(ctrl, sizeof(ctrl), "H%s\nP root\nN%s\nf%s\n",
                        host, job_name, data_fn);
    len = snprintf(cmd, sizeof(cmd), "\x02%d %s\n", clen, ctrl_fn);
    if (!write_all(&ctx, (const uint8_t*) cmd, len, 10000)) break;
    if (!read_ack(&ctx, 10000)) break;
    if (!write_all(&ctx, (const uint8_t*) ctrl, clen, 10000)) break;
    if (!write_all(&ctx, &zero, 1, 10000)) break;
    if (!read_ack(&ctx, 10000)) break;

    ok = true;
  } while (0);

  cyw43_arch_lwip_begin();
  if (ctx.pcb) {
    tcp_arg(ctx.pcb, NULL);
    tcp_recv(ctx.pcb, NULL);
    tcp_sent(ctx.pcb, NULL);
    tcp_err(ctx.pcb, NULL);
    if (tcp_close(ctx.pcb) != ERR_OK) tcp_abort(ctx.pcb);
  }
  cyw43_arch_lwip_end();

  return ok;
}

bool lpr_deliver_with_retry(const char* server_ip, uint16_t port, const char* queue,
                            const uint8_t* data, uint32_t data_len,
                            const char* job_name, uint32_t giveup_secs) {
  uint32_t backoff_ms = 5000;
  uint32_t attempt = 0;
  char msg[96];
  absolute_time_t deadline = make_timeout_time_ms((uint64_t) giveup_secs * 1000);

  for (;;) {
    attempt++;
    snprintf(msg, sizeof(msg), "LPR: attempt %lu -> %s:%u (%lu bytes)",
             (unsigned long) attempt, server_ip, port, (unsigned long) data_len);
    logmsg(msg);

    if (lpr_send_job(server_ip, port, queue, data, data_len, job_name)) {
      logmsg("LPR: attempt succeeded");
      return true;
    }
    if (time_reached(deadline)) { logmsg("LPR: giving up (deadline)"); return false; }

    int64_t rem_ms = absolute_time_diff_us(get_absolute_time(), deadline) / 1000;
    if (rem_ms <= 0) { logmsg("LPR: giving up (deadline)"); return false; }
    uint32_t wait_ms = (backoff_ms < (uint32_t) rem_ms) ? backoff_ms : (uint32_t) rem_ms;

    snprintf(msg, sizeof(msg), "LPR: attempt %lu failed - retry in %lus",
             (unsigned long) attempt, (unsigned long) (wait_ms / 1000));
    logmsg(msg);

    absolute_time_t until = make_timeout_time_ms(wait_ms);
    while (!time_reached(until)) { pump(); }

    backoff_ms *= 2;
    if (backoff_ms > 60000) backoff_ms = 60000;
  }
}
