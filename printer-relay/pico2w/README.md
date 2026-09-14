# Pico 2W Printer Relay — firmware spike

C / pico-sdk + TinyUSB rewrite of the Pi Zero W printer-relay node. See the
evaluation + full spike plan in
`../../../.claude/plans/look-at-the-export-relay-mossy-bonbon.md` (kept alongside
the Pi Zero path in `printer-relay/`, which stays the shipping platform until this
proves out against a real programmer).

## Status

- **Step 1 — toolchain + blink:** this directory. Blinks the onboard LED, which on
  the Pico 2 W is driven by the CYW43 chip — so a working blink also confirms the
  wireless module is present (proves it's a "W" board).

## Prerequisites

- `arm-none-eabi-gcc` (`sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi`)
- `cmake`, `make`, host `gcc`, `python3`
- pico-sdk 2.x with submodules, at `~/pico/pico-sdk` (SDK 2.3.0 used here)

## Build

```sh
export PICO_SDK_PATH=~/pico/pico-sdk
cmake -S . -B build
cmake --build build -j
```

Output: `build/relay_blink.uf2`.

## Flash

1. Hold **BOOTSEL** while plugging in the Pico → it mounts as `RPI-RP2` / `RP2350`
   (here: `/media/basti/RP2350`).
2. Copy the firmware; the board flashes and reboots automatically:
   ```sh
   cp build/relay_blink.uf2 /media/basti/RP2350/
   ```
3. **Pass:** onboard LED blinks at ~2 Hz. (If it never blinks, `cyw43_arch_init()`
   failed — suspect a non-W board or a power/data-cable issue.)
