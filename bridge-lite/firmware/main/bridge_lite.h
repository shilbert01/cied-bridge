// SPDX-License-Identifier: MPL-2.0
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "sdmmc_cmd.h"

#define MAX_FILENAME_LEN     64
#define SHA256_LEN           32

// --- LED status ---

typedef enum {
    LED_OFF,
    LED_GREEN_STEADY,
    LED_BLUE_BLINK,
    LED_BLUE_STEADY,
    LED_GREEN_FLASH,
    LED_RED_STEADY,
} led_state_t;

// --- SD storage ---

extern const char *SD_MOUNT;

esp_err_t      sd_init(void);
sdmmc_card_t  *sd_get_card(void);
void           sd_set_vfs_mounted(bool mounted);
esp_err_t      sd_write_file(const char *path, const uint8_t *data, size_t len);
esp_err_t      sd_open_write(const char *path);
esp_err_t      sd_append(const uint8_t *data, size_t len);
esp_err_t      sd_close_write(void);
esp_err_t      sd_list_files(const char *dir, void (*cb)(const char *name, uint32_t size));
esp_err_t      sd_read_file(const char *path, uint8_t *buf, size_t buf_len, size_t *out_len);
esp_err_t      sd_delete_file(const char *path);
bool           sd_ready(void);
esp_err_t      sd_set_volume_label(const char *label);

// --- WiFi transport ---

esp_err_t wifi_transport_init(void);
bool      wifi_is_connected(void);

// --- LED ---

void led_init(void);
void led_set(led_state_t state);

// --- LCD (ST7735S 160x80) ---

void lcd_init(void);
void lcd_update(const char *line1, const char *line2,
                const char *line3, const char *line4);

// --- USB MSC device (receiver: present drive to PC) ---

esp_err_t usb_msc_device_init(void);
esp_err_t usb_msc_claim_for_local(void);
void      usb_msc_release_to_host(void);

// --- USB host MSC (sender: capture from programmer) ---

esp_err_t  usb_host_msc_init(void);

// --- File protocol (orchestration) ---

esp_err_t file_sender_init(void);
esp_err_t file_receiver_init(void);
