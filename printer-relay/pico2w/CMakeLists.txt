cmake_minimum_required(VERSION 3.13)

# Target the Pico 2 W (RP2350 + CYW43439 wireless). Must be set before the SDK
# is initialised so the right board header and platform (rp2350-arm-s) are chosen.
set(PICO_BOARD pico2_w CACHE STRING "Board type")

# Pull in the SDK (uses $PICO_SDK_PATH).
include(pico_sdk_import.cmake)

project(relay_printer C CXX ASM)

pico_sdk_init()

set(RELAY_SOURCES
    main.c
    usb_descriptors.c
    printer_device.c
    flash_store.c
    job_filter.c
    job_queue.c
    wifi.c
    netclock.c
    netlog.c
    node_id.c
    lpr_client.c
    oled.c
)

# Two builds from the same sources:
#   relay_printer        RELAY_DEBUG_CDC=1  printer + debug CDC (bench work)
#   relay_printer_nocdc  RELAY_DEBUG_CDC=0  printer-only, single interface, no IAD
#                        — matches the Pi Zero's f_printer gadget for picky hosts
#                        (e.g. Abbott Merlin). Diagnose it via the WiFi UDP log.
function(add_relay_variant name cdc)
    add_executable(${name} ${RELAY_SOURCES})

    # tusb_config.h + lwipopts.h live in this directory.
    target_include_directories(${name} PRIVATE ${CMAKE_CURRENT_LIST_DIR})
    target_compile_definitions(${name} PRIVATE RELAY_DEBUG_CDC=${cdc})

    # pico_stdlib: timing/gpio. tinyusb_device: USB device stack + rp2350 dcd.
    # hardware_flash/hardware_sync: internal-flash job store (flash_store.c).
    # hardware_i2c: SSD1306 OLED. cyw43_arch_lwip: WiFi + lwIP (LPR, SNTP, netlog).
    target_link_libraries(${name}
        pico_stdlib
        tinyusb_device
        hardware_flash
        hardware_sync
        hardware_i2c
        hardware_watchdog
        pico_unique_id
        pico_cyw43_arch_lwip_threadsafe_background
        pico_lwip_sntp
    )

    pico_add_extra_outputs(${name})
endfunction()

add_relay_variant(relay_printer       1)
add_relay_variant(relay_printer_nocdc 0)
