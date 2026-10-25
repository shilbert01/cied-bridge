// SPDX-License-Identifier: MPL-2.0
// APA102 LED driver for T-Dongle-S3 (SPI 2-wire: GPIO 39 CLK, GPIO 40 DIN)

#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "bridge_lite.h"

#define APA102_CLK_GPIO  39
#define APA102_DIN_GPIO  40

static spi_device_handle_t s_spi;
static esp_timer_handle_t  s_blink_timer;
static led_state_t         s_state = LED_OFF;
static bool                s_blink_on = false;

static void apa102_set_rgb(uint8_t r, uint8_t g, uint8_t b, uint8_t brightness)
{
    // APA102 frame: [0x00000000] [0xE0|bright, B, G, R] [0xFFFFFFFF]
    uint8_t buf[12] = {
        0x00, 0x00, 0x00, 0x00,
        (uint8_t)(0xE0 | (brightness & 0x1F)), b, g, r,
        0xFF, 0xFF, 0xFF, 0xFF,
    };
    spi_transaction_t txn = {
        .length = sizeof(buf) * 8,
        .tx_buffer = buf,
    };
    spi_device_transmit(s_spi, &txn);
}

static void blink_cb(void *arg)
{
    s_blink_on = !s_blink_on;
    if (s_blink_on) {
        apa102_set_rgb(0, 0, 255, 8);
    } else {
        apa102_set_rgb(0, 0, 0, 0);
    }
}

void led_init(void)
{
    spi_bus_config_t bus = {
        .mosi_io_num = APA102_DIN_GPIO,
        .sclk_io_num = APA102_CLK_GPIO,
        .miso_io_num = -1,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 16,
    };
    spi_bus_initialize(SPI3_HOST, &bus, SPI_DMA_DISABLED);

    spi_device_interface_config_t dev = {
        .clock_speed_hz = 1 * 1000 * 1000,
        .mode = 0,
        .spics_io_num = -1,
        .queue_size = 1,
    };
    spi_bus_add_device(SPI3_HOST, &dev, &s_spi);

    esp_timer_create_args_t timer_args = {
        .callback = blink_cb,
        .name = "led_blink",
    };
    esp_timer_create(&timer_args, &s_blink_timer);

    apa102_set_rgb(0, 0, 0, 0);
}

void led_set(led_state_t state)
{
    if (s_state == LED_BLUE_BLINK) {
        esp_timer_stop(s_blink_timer);
    }
    s_state = state;

    switch (state) {
    case LED_OFF:
        apa102_set_rgb(0, 0, 0, 0);
        break;
    case LED_GREEN_STEADY:
        apa102_set_rgb(0, 255, 0, 8);
        break;
    case LED_BLUE_BLINK:
        s_blink_on = true;
        apa102_set_rgb(0, 0, 255, 8);
        esp_timer_start_periodic(s_blink_timer, 300 * 1000);
        break;
    case LED_BLUE_STEADY:
        apa102_set_rgb(0, 0, 255, 8);
        break;
    case LED_GREEN_FLASH:
        apa102_set_rgb(0, 255, 0, 16);
        break;
    case LED_RED_STEADY:
        apa102_set_rgb(255, 0, 0, 8);
        break;
    }
}
