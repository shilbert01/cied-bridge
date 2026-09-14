/*
 * UDP log sink + remote BOOTSEL trigger (see netlog.h).
 *
 * Broadcasts each log line on the relay subnet so the relay can be watched while
 * plugged into a programmer. Also listens on the same port for a 'b' byte, which
 * requests a reboot to BOOTSEL — this is how the printer-only (no-CDC) build
 * stays re-flashable without the physical button.
 */
#include "netlog.h"
#include "config.h"
#include "node_id.h"
#include "pico/cyw43_arch.h"
#include "lwip/udp.h"
#include "lwip/ip_addr.h"
#include "lwip/ip.h"          // ip_current_dest_addr()
#include "lwip/netif.h"
#include "lwip/pbuf.h"
#include <string.h>

static struct udp_pcb* _pcb;
static ip_addr_t       _target;
static volatile bool   _reboot_req;

static void on_rx(void* arg, struct udp_pcb* pcb, struct pbuf* p,
                  const ip_addr_t* addr, u16_t port) {
  (void) arg; (void) pcb; (void) addr; (void) port;
  if (!p) return;
  if (p->len > 0) {
    const uint8_t* b = (const uint8_t*) p->payload;
    // Fleet safety: only honour BOOTSEL when the packet was UNICAST to us. A
    // broadcast 'b' must NOT reboot every Pico in the room — target one node by
    // sending 'b' to its specific IP (visible in the Pi3 edge-node panel).
    bool unicast = !ip_addr_isbroadcast(ip_current_dest_addr(), netif_default);
    if (unicast && (b[0] == 'b' || b[0] == 'B')) _reboot_req = true;
  }
  pbuf_free(p);
}

bool netlog_init(void) {
  if (_pcb) return true;
  if (!ipaddr_aton(NETLOG_TARGET, &_target)) return false;

  cyw43_arch_lwip_begin();
  _pcb = udp_new();
  if (_pcb) {
    udp_bind(_pcb, IP_ADDR_ANY, NETLOG_PORT);
    udp_recv(_pcb, on_rx, NULL);
  }
  cyw43_arch_lwip_end();
  return _pcb != NULL;
}

void netlog_line(const char* s) {
  if (!_pcb || !s) return;
  // Prefix every UDP line with the node id so a fleet's interleaved logs on the
  // shared port are readable: "relay-pico-9F3A2C  <line>". (The local CDC gets
  // the raw line; only the network sink needs the tag.)
  const char* id = relay_node_id();
  char line[256];
  int n = snprintf(line, sizeof(line), "%s  %s\r\n", id, s);
  if (n <= 0) return;
  if (n > (int) sizeof(line)) n = sizeof(line);

  cyw43_arch_lwip_begin();
  struct pbuf* p = pbuf_alloc(PBUF_TRANSPORT, (uint16_t) n, PBUF_RAM);
  if (p) {
    memcpy(p->payload, line, n);
    udp_sendto(_pcb, p, &_target, NETLOG_PORT);
    pbuf_free(p);
  }
  cyw43_arch_lwip_end();
}

bool netlog_reboot_requested(void) { return _reboot_req; }
