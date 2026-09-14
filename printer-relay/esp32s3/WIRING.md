# ESP32-S3 DevKitC printer-relay — wiring

Generic ESP32-S3-DevKitC (4 MB flash + 4 MB PSRAM, WS2812 RGB LED). Pins are set
in the fork's `main/relay_config.h` — change there if you route differently.

## SPI microSD breakout (GND / MISO / CLK / MOSI / CS / 3V3)

| SD module pin | ESP32-S3 GPIO | relay_config.h |
|---------------|---------------|----------------|
| CS            | GPIO10        | `SD_PIN_CS`    |
| MOSI (DI)     | GPIO11        | `SD_PIN_MOSI`  |
| SCK (CLK)     | GPIO12        | `SD_PIN_SCK`   |
| MISO (DO)     | GPIO13        | `SD_PIN_MISO`  |
| 3V3           | 3V3           | —              |
| GND           | GND           | —              |

Notes:
- These GPIOs avoid native USB (19/20), UART0 (43/44), strapping pins (0/3/45/46)
  and the octal-PSRAM bus (33–37). Safe on a standard DevKitC-1.
- **Format the card FAT32** (or exFAT if the build enables it). An unformatted card
  → mount fails → LED stays red and jobs are dropped (logged on the console).
- SPI runs at 20 MHz (~1–2 MB/s) — well above full-speed USB intake (~1 MB/s).

## Status RGB LED (4-leg ANALOG RGB, PWM)

A 4-leg RGB LED = one **common** leg + Red/Green/Blue legs, driven by LEDC PWM.
Defaults (change in `relay_config.h` to match your board):

| LED leg | ESP32-S3 GPIO | relay_config.h |
|---------|---------------|----------------|
| Red     | GPIO4         | `STATUS_LED_R_GPIO` |
| Green   | GPIO5         | `STATUS_LED_G_GPIO` |
| Blue    | GPIO6         | `STATUS_LED_B_GPIO` |
| Common  | 3V3 **or** GND (see below) | `STATUS_LED_COMMON_ANODE` |

- **Common cathode** (common leg → GND): set `STATUS_LED_COMMON_ANODE 0` (default).
  Put a ~220–330 Ω resistor in series with each color leg.
- **Common anode** (common leg → 3V3): set `STATUS_LED_COMMON_ANODE 1` (PWM inverts).
- If the RGB LED is **on-board** on your clone, use its documented R/G/B GPIOs
  instead of 4/5/6.

| Color | Meaning |
|-------|---------|
| Green | WiFi connected **and** SD ready — can forward jobs |
| Red   | No WiFi, or SD not ready — cannot forward |
| Blue  | Actively capturing or forwarding a job |
| Dim white | Booting (before first state) |

## USB ports

The DevKitC has **two** USB-C ports:
- **UART / "COM"** port (CP210x/CH340) — use this to **flash** (`./flash.sh`).
- **native USB ("USB")** port — this is what presents the **printer** to the host /
  programmer. Plug the host that prints here. The relay's debug CDC log also appears
  on the host as a `/dev/ttyACM*` on this port once enumerated.
