# ESP32-S3 Printer-Relay — Spike Plan (fork of ThingPulse pendrive-s3-wifi-dongle)

## Context / why

The Pico 2W spike proved the relay concept end-to-end (captures a real print job, forwards
over WiFi, prints on the Pi3) but hit two walls:
- **Storage:** real jobs run **up to ~16 MB** (9+ MB is common; a 3.68 MB Merlin job was an early
  sample) — far beyond the Pico's usable internal flash, and **beyond any PSRAM too** → the job
  MUST be streamed to mass storage (SD/SDMMC), never buffered whole in RAM. See §Storage below.
- **WiFi:** traced to the **hidden SSID** (CYW43 can't reliably join a hidden AP) — now fixed
  by un-hiding `relayap` + WPA3-SAE. See `../pico2w/` and the Pico plan.

The **ESP32-S3** does better on both: a real **USB-OTG** controller (so it can present the USB
printer class, like the Pico) **and** far more storage. Two target boards:

| Board | Role | Key specs |
|---|---|---|
| **Waveshare ESP32-S3-LCD-0.85** | **bench / dev** — richest for bring-up + debug | ESP32-S3-PICO-1-**N8R8** (**8 MB flash + 8 MB octal PSRAM**), 0.85" **128×128 color LCD** (GC9107, SPI), **microSD (4-bit SDMMC)**, **WS2812 RGB LED**, **BOOT/PWR/PLUS buttons**, I²C/UART, USB-C. **Type-C = native USB-OTG — CONFIRMED from the schematic** (D+ = IO20, D- = IO19; CC1/CC2 have 5.1 kΩ Rd → device/sink), so the printer class works over the Type-C. Receptacle → needs a cable (bench role). |
| **ThingPulse Pendrive S3 128MB** | **deployed form factor** — USB-C stick you plug into the programmer | ESP32-S3-MINI, 4 MB app flash, **no PSRAM**, **128 MB SDMMC storage**, WS2812B RGB LED, capacitive touch button, **USB-DFU** reflash |

The **Waveshare board is the one to develop on** — its **microSD** is the durable job store (jobs up
to ~16 MB, plus a queue), the BOOT button + UART + exposed pads make the flash/debug loop painless
(the *sealed* Pendrive can't), and the LCD is a live status readout during bring-up. It also proves a
**status-screen relay variant** is trivial (the Pico's `oled.c` status concept ports to the GC9107
SPI TFT). Its 8 MB PSRAM is handy for WiFi/TLS/heap and the LCD framebuffer, **but not for whole-job
buffering** — a 16 MB job doesn't fit in 8 MB (see §Storage). The Pendrive stays the *deployment*
target for its discreet plug-in-the-port form factor.

**Waveshare pin map (verified from the board schematic) — no conflicts across the relay's needs:**

| Function | GPIOs |
|---|---|
| **Native USB (printer)** | D- = IO19, D+ = IO20 (Type-C → native USB-OTG; CC1/CC2 = 5.1 kΩ Rd → USB device/sink) |
| **microSD — 4-bit SDMMC** | D0=17, D1=18, D2=13, D3=14, CMD=15, CLK=16 |
| LCD (GC9107 SPI) | CS=21, CLK=38, DIN=39, RST=40, DC=45, BL=46 |
| WS2812 RGB LED | IO48 |
| Buttons | KEY_MINUS/**BOOT**=0, KEY_PLUS=4, KEY_PWR=5 |
| I²C (shared: codec/IMU/ext) | SCL=41, SDA=42 |
| UART0 (console/flash fallback) | TXD=43, RXD=44 |

Unused by the relay but wired (own pins, no conflict): LiPo charger/monitor (IO1/2/3), IMU (I²C +
INT IO6/47), audio codec ES8311/ES7210 + PA (IO7–12). Flashing: Type-C USB-Serial/JTAG with BOOT
(IO0) held, or UART0 — so there's a reflash path even once the printer firmware owns the native USB.

## Storage — the design is forced: stream to SD/SDMMC, never buffer in RAM

Real jobs reach **~16 MB** (observed: `#111 done 9127 KB`, and up to 16 MB). That exceeds **every**
RAM option — 4 MB and even 8 MB PSRAM — so "buffer the whole job in PSRAM" is **off the table**. The
only viable design (and the one the Pico already used) is: **USB bulk-OUT → small internal-SRAM ring
(DMA) → `job_filter` → append to a file on mass storage → finalize on 5 s idle → LPR reads the file →
Pi3.** USB flow-control (NAK) applies backpressure, and SD/SDMMC write speed (several MB/s) easily
outruns full-speed USB (~1 MB/s), so there's no bottleneck. Storage sizing is a non-issue: the
Pendrive's **128 MB SDMMC** and the Waveshare's **microSD** both hold a 16 MB job plus a multi-job
retry queue. Consequence to expect: at full-speed USB a 16 MB job takes ~15–20 s+ just to *receive*
(the high-speed Pi Zero is faster) — validate end-to-end timing at steps 3–4.

**Base to fork:** [`ThingPulse/esp32-pendrive-s3-wifi-dongle`](https://github.com/ThingPulse/esp32-pendrive-s3-wifi-dongle)
— a working **ESP-IDF 5.2.2 + TinyUSB + esp_wifi** firmware for the *exact* Pendrive board, with
USB-DFU flashing. It's a USB-network↔WiFi bridge; we strip the networking and add printer +
capture + LPR. This proves toolchain, USB-OTG bring-up, WiFi, and flashing up front.

## What the base repo gives us (reuse)

- **`main/usb_dongle_main.c`** — `app_main`: `nvs_flash_init` → `tinyusb_driver_install` →
  *[per-class init]* → `initialise_wifi`. Drop our printer/capture/LPR init into the class slot.
- **`components/tinyusb_dongle/`** — vendored fork of Espressif `esp_tinyusb`. USB device stack +
  descriptor framework: `usb_descriptors.c` (device desc with **custom VID/PID**, `ITF_NUM_*` /
  `EPNUM_*` / `TUSB_DESC_TOTAL_LEN` assembly), `descriptors_control.c` (the `tud_descriptor_*_cb`).
- **`main/cmd_wifi.c`** — full `esp_wifi` STA (join, events, scan, `pmf_cfg.capable=true`).
- **`components/tinyusb_dongle/tusb_dfu*.c`** — **USB-DFU** → reflash the sealed stick over USB-C.
- FreeRTOS+CLI + CDC console — bench debugging.

## What we remove

- Networking classes (ECM/RNDIS/NCM) + `tinyusb_net.c` bridge, and BTH — frees endpoints + flash.
  (The base repo itself warns ECM/RNDIS+BTH+CDC can't all coexist — the **FS endpoint budget**.)

## What we add (mostly ported from `../pico2w/`)

- **Printer class** — TinyUSB has no printer class, so hand-roll it exactly as on the Pico:
  printer interface descriptor (class `0x07`, 2 bulk EPs) + app class driver via
  `usbd_app_driver_get_cb` handling `GET_DEVICE_ID` (HP PnP string), `GET_PORT_STATUS`=`0x18`,
  `SOFT_RESET`. Port `pico2w/printer_device.c` + descriptor bytes; in `usb_descriptors.c` add
  `ITF_NUM_PRINTER`, `EPNUM_PRINTER_OUT/IN`, a `TUD_PRINTER_DESC_LEN` term, and set
  **VID `0x03f0` / PID `0x152a`** + serial + product strings (the HP LaserJet 400 M401dn clone).
  **Printer MUST be interface 0** (the Pico lesson: `usblp` routes class requests by low byte of
  `wIndex`).
- **Job capture → 128 MB FATFS** — mount SDMMC once (`esp_vfs_fat_sdmmc_mount`; *new* — base repo
  doesn't touch storage), capture task streams bulk-OUT (through `job_filter`) to a file, finalize
  on 5 s idle, CRC-verify. This is `pico2w/flash_store.c`'s pattern pointed at 128 MB.
- **LPR delivery** — port `pico2w/lpr_client.c` RFC-1179 to **BSD sockets** (simpler than lwIP raw);
  deliver the file to Pi3 `:515`; exp-backoff retry; **persistent queue = files on the 128 MB
  flash** → an undelivered job survives a power loss (the durability win the 4 MB-flash board can't
  give).
- **`job_filter`** (PCL/PJL tray rewrite) — port `pico2w/job_filter.c` verbatim (pure byte logic).
- **WiFi** — set `wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_WPA3_PSK` + `pmf_cfg.capable`
  → **WPA3-SAE mixed**, matching the Pi3 hostapd. `relayap` is now broadcast → no hidden-SSID pain.
  **Note (unlike the Pico):** `esp_wifi` also supports **WPA2/WPA3-Enterprise** natively via
  `esp_eap_client` — **EAP-TLS (per-device certs), PEAP, TTLS** (ESP-IDF example
  `wifi/wifi_enterprise`, present in v5.2.x; uses built-in mbedTLS). So per-device-cert security is
  available on the S3 too (menuconfig + integration, not a WHD port) if a site's Pi3-RADIUS setup
  requires it — the ESP32-S3's security ceiling **matches the Pi Zero's**, not the Pico's.
- **Status/control** — abstract a small status API with two backends: **LCD** (GC9107 SPI, on the
  Waveshare bench board — text status, ports the Pico `oled.c` concept) and **WS2812B RGB LED**
  (`led_strip` over RMT, on the Pendrive — boot / wifi / printing / delivered / error). Control:
  the Pendrive's capacitive touch button (force-deliver + long-press factory-reset), or the
  Waveshare's BOOT/PWR/PLUS buttons on the bench.
- **SNTP** (`esp_sntp`, server = Pi3), **node-id** from `esp_read_mac` → `relay-esp-XXXXXX`, **netlog**
  UDP to `10.99.0.255:9999` (parity with the Pico/fleet).

## Build order (each step has a hardware gate) — mirrors the Pico spike

1. **Toolchain + fork boots on the Waveshare bench board.** ESP-IDF 5.2.2; fork the repo;
   build/flash via BOOT button + `esptool` over USB-C; confirm CDC console; strip networking/BTH in
   menuconfig. (USB-DFU reflash is validated later on the Pendrive, step 6.)
   **Gate:** boots, CDC console + LCD alive, `esp_wifi` joins `relayap` (WPA3-SAE).
2. **Printer-class enumeration.** Add printer descriptor + class driver; HP clone IDs.
   **Gate:** host shows *HP LaserJet 400 M401dn*, `usblp`→`/dev/usb/lp0`, `ieee1284_id` = cloned PnP
   string, a test print reaches bulk-OUT.
3. **Capture → SD/SDMMC FATFS.** Mount storage; stream capture to a file; finalize-on-idle; CRC.
   **Gate:** a **≥16 MB** job lands **intact** on storage (bench: microSD; deploy: 128 MB SDMMC),
   with capture keeping up with the programmer's USB stream.
4. **WiFi + LPR delivery.** Sockets LPR + file-backed retry queue.
   **Gate:** captured job **prints on the Pi3**; a job survives a WiFi drop and delivers on reconnect.
5. **Tray rewrite + status UI + SNTP + netlog.** (Status = LCD on the Waveshare bench board / RGB
   LED on the Pendrive, behind one status API.)
   **Gate:** tray filter verified; status UI reflects state; clock synced from Pi3.
6. **Deploy on the Pendrive S3 + programmer validation** — the go/no-go. Reflash via USB-DFU; plug
   into a real cardiac programmer (USB-C-female→A-male adapter as needed).
   **Gate:** programmer accepts it; job captured + printed **unchanged**; side-by-side vs the Pi Zero.

## Risks / watch-items

- **Endpoint budget (full-speed):** printer (2 bulk) + debug CDC (3) fits; don't stack more classes.
- **128 MB = SDMMC + FATFS, not an ESP flash partition** — separate mount; not exercised by the base
  repo → this is genuinely new code (but a standard ESP-IDF driver).
- **Whole-job-in-RAM is ruled out by job size** (~16 MB > 8 MB PSRAM), not just by the Pendrive
  having no PSRAM → the design is **stream-to-SD/SDMMC** on every board (see §Storage). PSRAM, where
  present, is for WiFi/TLS/heap + LCD framebuffer only.
- **Large-job timing:** a 16 MB job over full-speed USB takes ~15–20 s+ to receive; ensure the
  capture ring + SD writes keep up and the 5 s idle-finalize timer doesn't fire mid-job (it keys off
  *last activity*, so it's fine, but confirm under a real 16 MB transfer).
- **USB-C male stick vs USB-A programmer port** → needs a C-female→A-male adapter; confirm the
  programmer's port type.
- **App flash 4 MB + dual-OTA is tight** — may drop to single-app (no OTA) for space, or use an
  8 MB-flash S3 module. DFU still works either way.
- **Full-speed only (12 Mbps, internal PHY — no HS option on the S3; the Type-C connector doesn't
  change that).** This does NOT hurt acceptance — many real USB printers are FS; the Pi Zero's HS is
  the outlier, and the acceptance risk is about descriptors/class-requests, not speed. The real cost
  is **intake time**: FS bulk ≈ 1.0–1.2 MB/s → a 16 MB job takes ~13–16 s to transfer in (the
  programmer sees this as the print-submit latency; WiFi forward is async after). **Step-6 check:
  confirm the programmer has no max-transfer timeout on a printer accepting bytes that slowly.** (Pi
  Zero's HS swallows the same job in ~1–2 s — its one lasting edge for high-volume/large-job sites.)
- Ships with **CircuitPython**; we replace it with this ESP-IDF fork (MicroPython/CircuitPython was
  already rejected for reliable printer-class emulation).

## PROGRESS

**Board on hand (2026-07-26): a generic ESP32-S3 DevKitC-class board** — NOT the
Waveshare bench board or the Pendrive. Consequence: **no on-board microSD slot.**
Native USB-OTG (GPIO19/20) is present so the printer class works, but the storage
store must be resolved (see below). Exact module (N8/N8R8/N16R8 → flash + PSRAM)
to be read off the silkscreen.

**Toolchain: DONE.** ESP-IDF **v5.2.2** cloned to `~/esp/esp-idf` + `install.sh
esp32s3`; `idf.py` / `xtensa-esp-elf-gcc 13.2.0` working. Fork cloned to
`~/Sources/esp32-pendrive-s3-wifi-dongle` (the buildable project — relay work is
done there; this repo dir holds the plan + helper scripts). Baseline fork built
clean as-is (844 KB) to prove the toolchain before modifying.

**Step 2 (printer-class enumeration + capture): CODE DONE + BUILDS CLEAN
(2026-07-26); hardware gate PENDING (board not yet plugged in).** In the fork:
- `main/printer_device.c/.h` — ESP port of `pico2w/printer_device.c`. Same custom
  printer app-class driver via the weak `usbd_app_driver_get_cb`; SPSC ring;
  GET_DEVICE_ID / GET_PORT_STATUS(0x18) / SOFT_RESET. Pico-isms replaced:
  `to_ms_since_boot`→`esp_timer_get_time()/1000`, `__dmb()`→`__sync_synchronize()`,
  512-byte bulk reads. **Fork-TinyUSB gotcha:** `usbd_class_driver_t` here has no
  `deinit` and `name` is `#if CFG_TUSB_DEBUG >= 2` only (guarded both).
- `main/printer_descriptors.c/.h` — HP LaserJet 400 M401dn clone
  (VID 0x03f0/PID 0x152a), **printer = interface 0** (usblp routing lesson),
  fed to `tinyusb_driver_install()` via `tinyusb_config_t` (device/config/string)
  — no need to hack the vendored Kconfig descriptors. Debug CDC on itf 1/2
  (`RELAY_DEBUG_CDC`, default 1); flip to 0 for the Step-6 printer-only variant.
- `main/usb_dongle_main.c` — rewritten lean `app_main`: nvs → install printer
  descriptors → CDC log console → `capture_task` (drains ring, finalize-on-5s-idle,
  logs `job complete: N bytes`). CLI / cmd_wifi / usb-net all dropped.
- `sdkconfig.defaults` — `NET_MODE_NONE`, BT off, CDC on. Result: **253 KB**
  firmware (from 844 KB), 76% app partition free.
- Helper scripts in this dir: `flash.sh` (auto-detects port, flash + monitor),
  `test-enumeration.sh` (host-side Step-2 gate: lsusb / usblp / ieee1284_id / test
  print). **To pass the gate:** plug the board's native-USB port into a host, run
  `./flash.sh`, then `./test-enumeration.sh` on the host.

**Storage decision — RESOLVED (2026-07-26): path (A), SPI microSD.** User has an
SPI microSD breakout (GND/MISO/CLK/MOSI/CS/3V3) and will wire it. Pinout (in
`relay_config.h`, see `WIRING.md`): CS=10, MOSI=11, SCK=12, MISO=13, 20 MHz.

**Step 3 + 4 (capture→SD + WiFi + LPR delivery + status LED): CODE DONE + BUILDS
CLEAN (2026-07-26); hardware gate PENDING (board + SD not yet wired/flashed).**
Added to the fork `main/`:
- `sd_store.c/.h` — SPI SD mount (`esp_vfs_fat_sdspi_mount`) + **streaming** capture
  to a file and a **filesystem-backed retry queue** (`/sd/jobs/*.prn`). Jobs reach
  ~16 MB so nothing is buffered whole in RAM; undelivered files persist across
  reboot/power-loss (better than the Pico's single flash slot). CRC via esp_crc32.
- `job_filter.c/.h` — **copied verbatim** from `pico2w/` (pure byte logic, PCL tray
  rewrite + PJL MEDIASOURCE inject).
- `wifi_sta.c/.h` — `esp_wifi` STA, event-driven auto-reconnect, WPA2-PSK (fleet
  standard), exposes up/down + IP.
- `lpr_client.c/.h` — RFC-1179 over **BSD sockets** (much simpler than the Pico's
  lwIP-raw callback pumping); **streams the job file from SD** in the data phase;
  exp-backoff retry. Logger defaults to ESP_LOG.
- `status_led.c/.h` — **4-leg ANALOG RGB LED via LEDC PWM** (board has a discrete
  RGB LED, not a WS2812 — `led_strip` dep removed). 3 channels on one timer, 8-bit
  @5 kHz; `STATUS_LED_{R,G,B}_GPIO` (default 4/5/6) + `STATUS_LED_COMMON_ANODE`
  (0=cathode default, 1=anode → inverted duty). **GREEN = WiFi up AND SD ready;
  RED = not ready; BLUE = capturing/forwarding.**
- `node_id.c/.h` — `relay-esp-XXXXXX` from the WiFi MAC.
- `usb_dongle_main.c` — orchestration: nvs → LED → SD mount → WiFi start → USB
  (printer+CDC) → three FreeRTOS tasks: **capture** (prio 6, ring→filter→SD,
  finalize on 5 s idle), **delivery** (prio 4, drains the SD queue over LPR when
  WiFi up), **status** (prio 3, LED + 3 s heartbeat).
- `sdkconfig.defaults` NET/BT off. **Module confirmed ESP32-S3 N16R8 (16 MB flash +
  8 MB octal PSRAM):** octal PSRAM enabled (`SPIRAM`/`SPIRAM_MODE_OCT`/80 MHz,
  malloc-capable), flash 16 MB, and a **custom single-app partition** (`partitions.csv`:
  nvs+phy+**4 MB factory**, no OTA). Firmware **~908 KB → 78% free** in the 4 MB app
  partition (was 14% on the old dual-OTA 1 MB). Octal PSRAM uses GPIO33–37 → keep
  clear (SD 10–13 / LED 48 already are).

**Bring-up when the board + SD are ready:** wire per `WIRING.md`, FAT32-format the
card, `./flash.sh`, then watch the console: expect SD mount info, `wifi up`, LED
green; print to the host's `/dev/usb/lp0` → `job complete … -> queued` → `delivered`.

**Config knobs (`relay_config.h`):** SSID/pass, `PRINT_SERVER_*`, SD pins,
`STATUS_LED_GPIO` (48 default; 38 on some boards), `RELAY_DEBUG_CDC`.

**Open watch-item:** unicore is inherited from the dongle — WiFi+USB+SD on one core
works but dual-core would help throughput; revisit if intake lags. (PSRAM + app
headroom now resolved via N16R8 octal PSRAM + 4 MB single-app partition.)

## Fallback / non-negotiables

Pi Zero stays the **shipping** platform (validated on Medtronic + Abbott); the `pico2w/` tree stays
as reference. Retire nothing until step 6 passes on the real target programmers.
