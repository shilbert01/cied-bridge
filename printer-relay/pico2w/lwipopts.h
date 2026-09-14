#ifndef _LWIPOPTS_H
#define _LWIPOPTS_H

// Bare-metal lwIP config for pico_cyw43_arch_lwip_threadsafe_background
// (NO_SYS=1, raw/callback API only — no BSD sockets/netconn). TCP client + DHCP.

#define NO_SYS                      1
#define LWIP_SOCKET                 0
#define LWIP_NETCONN                0
#define MEM_LIBC_MALLOC             0
#define MEM_ALIGNMENT               4
#define MEM_SIZE                    16000
#define MEMP_NUM_TCP_SEG            32
#define MEMP_NUM_ARP_QUEUE          10
#define PBUF_POOL_SIZE              24
#define LWIP_ARP                    1
#define LWIP_ETHERNET               1
#define LWIP_ICMP                   1
#define LWIP_RAW                    1
#define TCP_MSS                     1460
#define TCP_WND                     (8 * TCP_MSS)
#define TCP_SND_BUF                 (8 * TCP_MSS)
#define TCP_SND_QUEUELEN            ((4 * (TCP_SND_BUF) + (TCP_MSS - 1)) / (TCP_MSS))
#define LWIP_NETIF_STATUS_CALLBACK  1
#define LWIP_NETIF_LINK_CALLBACK    1
#define LWIP_NETIF_HOSTNAME         1
#define LWIP_NETCONN                0
#define MEM_STATS                   0
#define SYS_STATS                   0
#define MEMP_STATS                  0
#define LINK_STATS                  0
#define LWIP_CHKSUM_ALGORITHM       3
#define LWIP_DHCP                   1
#define LWIP_IPV4                   1
#define LWIP_TCP                    1
#define LWIP_UDP                    1
#define LWIP_DNS                    1
#define LWIP_TCP_KEEPALIVE          1
#define LWIP_NETIF_TX_SINGLE_PBUF   1
#define DHCP_DOES_ARP_CHECK         0
#define LWIP_DHCP_DOES_ACD_CHECK    0

// No OS: keep the threadsafe-background protection lightweight.
#define SYS_LIGHTWEIGHT_PROT        0

// ── SNTP (clock from the Pi3) ───────────────────────────────────────────────
#define SNTP_SERVER_DNS             0
#define SNTP_STARTUP_DELAY          0
// lwIP hands us Unix epoch seconds; netclock.c stores it.
#ifndef __ASSEMBLER__
#include <stdint.h>
void clock_on_ntp(uint32_t unix_secs);
#endif
#define SNTP_SET_SYSTEM_TIME(sec)   clock_on_ntp((uint32_t)(sec))

#endif // _LWIPOPTS_H
