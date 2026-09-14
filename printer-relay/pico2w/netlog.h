#ifndef NETLOG_H
#define NETLOG_H

#include <stdbool.h>

// UDP log sink so the relay can be diagnosed while its USB port is occupied by a
// programmer (the debug CDC is only readable by whatever host the Pico is
// plugged into — useless once it's attached to a Merlin). Logs are broadcast on
// the relay subnet; listen with e.g.  nc -ul 9999  on the Pi3.
//
// The same socket accepts a one-byte command: 'b' reboots to BOOTSEL, which
// keeps buttonless re-flashing working in the printer-only build that has no CDC.

// Create the UDP socket. Call once WiFi is up.
bool netlog_init(void);

// Send one log line (no-op until netlog_init succeeds).
void netlog_line(const char* s);

// True if a 'b' command arrived over UDP — main loop should reset to BOOTSEL.
bool netlog_reboot_requested(void);

#endif // NETLOG_H
