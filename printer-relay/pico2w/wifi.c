/*
 * CYW43 station-mode WiFi bring-up for the printer relay.
 *
 * Non-blocking so the TinyUSB printer stays responsive while WiFi associates:
 * wifi_start_connect() kicks an async join and wifi_poll() (called every main
 * loop) advances a small state machine, retrying with back-off on drop/failure.
 */
#include <string.h>
#include "wifi.h"
#include "config.h"
#include "node_id.h"
#include "pico/cyw43_arch.h"
#include "lwip/netif.h"
#include "lwip/ip4_addr.h"

enum { WS_IDLE, WS_SCANNING, WS_CONNECTING, WS_UP };

static int      _state = WS_IDLE;
static uint32_t _next_retry_ms;
static uint32_t _attempt_started_ms;
static uint32_t _scan_started_ms;
static uint32_t _degraded_since;   // when the link first dropped below UP (0 = up)
static char     _ip[16] = "0.0.0.0";
static int      _last_link = 99;   // last cyw43_tcpip_link_status() value (99 = unset)
static uint32_t _attempts;         // association attempts started
static uint32_t _scans;            // directed scans started
static bool     _init_ok;          // cyw43_arch_init() succeeded
static wifi_log_fn _log;

// Directed-scan result for the (possibly hidden) target SSID.
static bool     _scan_found;
static uint8_t  _scan_bssid[6];
static int16_t  _scan_best_rssi;

#define CONNECT_TIMEOUT_MS  30000
#define RETRY_BACKOFF_MS    5000
#define SCAN_TIMEOUT_MS     8000   // give a directed scan this long to complete
#define LINK_GRACE_MS       15000  // tolerate a degraded (non-UP) link this long
                                   // before tearing down + reconnecting — a
                                   // transient dip must NOT trigger a disassociate.

static uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

void wifi_set_logger(wifi_log_fn fn) { _log = fn; }

static const char* link_name(int l) {
  switch (l) {
    case CYW43_LINK_DOWN: return "down"; case CYW43_LINK_JOIN: return "join";
    case CYW43_LINK_NOIP: return "noip"; case CYW43_LINK_UP:   return "up";
    case CYW43_LINK_FAIL: return "fail"; case CYW43_LINK_NONET:return "nonet";
    case CYW43_LINK_BADAUTH: return "badauth"; default: return "?";
  }
}

static const char* state_name(int s) {
  return s == WS_UP ? "UP" : s == WS_CONNECTING ? "CONNECTING"
       : s == WS_SCANNING ? "SCANNING" : "IDLE";
}

static void wlog(const char* what, int a, int b) {
  if (!_log) return;
  char m[80];
  snprintf(m, sizeof(m), "wifi: %s %s->%s", what, link_name(a), link_name(b));
  _log(m);
}

static void set_state(int s) {
  if (_state == s) return;
  if (_log) { char m[64]; snprintf(m, sizeof(m), "wifi: state %s->%s (link=%s)",
             state_name(_state), state_name(s), link_name(_last_link)); _log(m); }
  _state = s;
}

bool wifi_init(void) {
  if (cyw43_arch_init()) { _init_ok = false; return false; }  // non-W board / init fail
  cyw43_arch_enable_sta_mode();
  // Disable WiFi power-save. The CYW43 default PM sleeps the radio between
  // beacons; on a marginal/hidden AP that causes missed beacons and constant
  // associate<->disassociate flapping (the node never holds a link). The relay
  // is USB-powered, so favour a rock-stable link over battery life.
  cyw43_wifi_pm(&cyw43_state, CYW43_NONE_PM);
  // Advertise our unique node id as the DHCP hostname, so the node shows up
  // NAMED in the Pi3's dnsmasq leases and the edge-node panel — no manual MAC
  // registration needed for the fleet. Set before DHCP starts (on link-up).
#if LWIP_NETIF_HOSTNAME
  netif_set_hostname(&cyw43_state.netif[CYW43_ITF_STA], relay_node_id());
#endif
  _init_ok = true;
  return true;
}

// cyw43 calls this for each AP seen during a scan. We keep the strongest match
// for our target SSID; a DIRECTED active scan (ssid set below) makes even a
// hidden AP answer with a probe response, so this fires for hidden relayap too.
static int scan_cb(void* env, const cyw43_ev_scan_result_t* r) {
  (void) env;
  if (r->ssid_len == strlen(WIFI_SSID) &&
      memcmp(r->ssid, WIFI_SSID, r->ssid_len) == 0) {
    if (!_scan_found || r->rssi > _scan_best_rssi) {
      memcpy(_scan_bssid, r->bssid, 6);
      _scan_best_rssi = r->rssi;
      _scan_found = true;
    }
  }
  return 0;   // keep scanning
}

// Kick a DIRECTED active scan for WIFI_SSID (mirrors wpa_supplicant scan_ssid=1).
// This is the hidden-SSID workaround: the CYW43 join path won't reliably probe
// for a hidden AP itself, but a directed scan will, and we then join the exact
// BSSID it returns.
static void wifi_start_scan(void) {
  _scan_found = false;
  _scan_best_rssi = -128;
  cyw43_wifi_scan_options_t opts;
  memset(&opts, 0, sizeof(opts));
  opts.ssid_len = strlen(WIFI_SSID);
  memcpy(opts.ssid, WIFI_SSID, opts.ssid_len);   // directed probe for this SSID
  opts.scan_type = 0;                              // 0 = active
  int err = cyw43_wifi_scan(&cyw43_state, &opts, NULL, scan_cb);
  set_state(WS_SCANNING);
  _scan_started_ms = now_ms();
  _scans++;
  if (_log) { char m[56]; snprintf(m, sizeof(m),
             "wifi: directed scan %lu for \"%s\" (err=%d)",
             (unsigned long) _scans, WIFI_SSID, err); _log(m); }
}

// Join the AP. When a directed scan has found the BSSID we join it explicitly;
// otherwise fall back to a plain SSID join (broadcast APs / SCAN_BEFORE_JOIN=0).
void wifi_start_connect(void) {
#ifdef WIFI_BSSID
  static const uint8_t bssid[6] = WIFI_BSSID;
  cyw43_arch_wifi_connect_bssid_async(WIFI_SSID, bssid, WIFI_PASS, WIFI_AUTH);
#else
  if (_scan_found)
    cyw43_arch_wifi_connect_bssid_async(WIFI_SSID, _scan_bssid, WIFI_PASS, WIFI_AUTH);
  else
    cyw43_arch_wifi_connect_async(WIFI_SSID, WIFI_PASS, WIFI_AUTH);
#endif
  set_state(WS_CONNECTING);
  _attempt_started_ms = now_ms();
  _attempts++;
  if (_log) {
    char m[80];
    if (_scan_found)
      snprintf(m, sizeof(m),
               "wifi: join attempt %lu bssid=%02x:%02x:%02x:%02x:%02x:%02x rssi=%d",
               (unsigned long) _attempts, _scan_bssid[0], _scan_bssid[1],
               _scan_bssid[2], _scan_bssid[3], _scan_bssid[4], _scan_bssid[5],
               _scan_best_rssi);
    else
      snprintf(m, sizeof(m), "wifi: join attempt %lu (no bssid — plain SSID)",
               (unsigned long) _attempts);
    _log(m);
  }
}

static void cache_ip(void) {
  const ip4_addr_t* a = netif_ip4_addr(netif_default);
  const char* s = a ? ip4addr_ntoa(a) : "0.0.0.0";
  // ip4addr_ntoa uses a shared static buffer — copy it out.
  size_t n = 0;
  while (s[n] && n < sizeof(_ip) - 1) { _ip[n] = s[n]; n++; }
  _ip[n] = '\0';
}

bool wifi_poll(void) {
  int link = cyw43_tcpip_link_status(&cyw43_state, CYW43_ITF_STA);
  if (link != _last_link) { wlog("link", _last_link, link); _last_link = link; }

  switch (_state) {
    case WS_SCANNING:
      // Directed scan in flight. When it finishes, join the BSSID it found; if
      // it found nothing (AP truly absent), back off and rescan.
      if (cyw43_wifi_scan_active(&cyw43_state)) {
        if (now_ms() - _scan_started_ms > SCAN_TIMEOUT_MS) {
          if (_log) _log("wifi: scan timed out — retrying");
          set_state(WS_IDLE);
          _next_retry_ms = now_ms() + RETRY_BACKOFF_MS;
        }
      } else if (_scan_found) {
        wifi_start_connect();          // -> WS_CONNECTING, joins discovered BSSID
      } else {
        if (_log) _log("wifi: scan complete — SSID not found, backing off");
        set_state(WS_IDLE);
        _next_retry_ms = now_ms() + RETRY_BACKOFF_MS;
      }
      break;

    case WS_CONNECTING:
      if (link == CYW43_LINK_UP) {
        cache_ip();
        set_state(WS_UP);
        _degraded_since = 0;
      } else if (link < 0 && (now_ms() - _attempt_started_ms) > 3000) {
        // Hard failure (fail/nonet/badauth) — back off and retry.
        set_state(WS_IDLE);
        _next_retry_ms = now_ms() + RETRY_BACKOFF_MS;
      } else if ((now_ms() - _attempt_started_ms) > CONNECT_TIMEOUT_MS
                 && link <= CYW43_LINK_DOWN) {
        // Timed out with no association at all — retry. But if we ARE associated
        // (join/noip), keep waiting for DHCP rather than disassociating.
        set_state(WS_IDLE);
        _next_retry_ms = now_ms() + RETRY_BACKOFF_MS;
      }
      break;

    case WS_UP:
      // Tolerate transient dips: only reconnect if the link stays below UP for
      // LINK_GRACE_MS. Tearing down on the first non-UP poll is what caused the
      // associate<->disassociate flapping (this is the theory under test).
      if (link == CYW43_LINK_UP) {
        _degraded_since = 0;
      } else {
        if (_degraded_since == 0) _degraded_since = now_ms();
        if (now_ms() - _degraded_since > LINK_GRACE_MS) {
          if (_log) _log("wifi: link degraded > grace — reconnecting");
          set_state(WS_IDLE);
          _next_retry_ms = now_ms();
          _ip[0] = '0'; _ip[1] = '\0';
        }
      }
      break;

    case WS_IDLE:
    default:
      if ((int32_t)(now_ms() - _next_retry_ms) >= 0) {
#if WIFI_SCAN_BEFORE_JOIN
        wifi_start_scan();     // find the (hidden) AP first, then join its BSSID
#else
        wifi_start_connect();  // plain SSID join (broadcast AP only)
#endif
      }
      break;
  }
  return _state == WS_UP;
}

bool wifi_is_up(void) { return _state == WS_UP; }

const char* wifi_ip(void) { return _ip; }

int wifi_link_status(void) { return _last_link; }

const char* wifi_status_str(void) {
  switch (_last_link) {
    case CYW43_LINK_DOWN:    return "down";     //  0 not joined
    case CYW43_LINK_JOIN:    return "join";     //  1 associated, no IP yet
    case CYW43_LINK_NOIP:    return "noip";     //  2 associated, awaiting DHCP
    case CYW43_LINK_UP:      return "up";       //  3 up with IP
    case CYW43_LINK_FAIL:    return "fail";     // -1 connection failed
    case CYW43_LINK_NONET:   return "nonet";    // -2 SSID not found
    case CYW43_LINK_BADAUTH: return "badauth";  // -3 wrong passphrase
    default:                 return "?";
  }
}

uint32_t wifi_attempts(void) { return _attempts; }

bool wifi_init_ok(void) { return _init_ok; }
