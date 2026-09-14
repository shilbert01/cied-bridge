#ifndef OLED_H
#define OLED_H

#include <stdint.h>
#include <stdbool.h>

// Minimal SSD1306 128x64 I2C status display. All calls are safe no-ops if the
// panel is absent or OLED_ENABLED is 0, so the firmware runs unchanged without
// a display wired.

// Probe + initialise. Returns true if a panel ACKed on the I2C bus.
bool oled_init(void);

// True once a panel has been detected.
bool oled_present(void);

// Render the relay status screen and push it to the panel.
//   wifi_up      — associated with the AP
//   ip           — dotted-quad (or "down")
//   server_up    — print server reachable
//   jobs         — session job count
//   last_hms     — "HH:MM:SS" of last job (or "--:--:--")
//   clock_hms    — current wall clock "HH:MM:SS"
void oled_status(bool wifi_up, const char* ip, bool server_up,
                 uint32_t jobs, const char* last_hms, const char* clock_hms);

// Full-screen banner (e.g. "PRINTING") — inverted, centred-ish.
void oled_banner(const char* line);

#endif // OLED_H
