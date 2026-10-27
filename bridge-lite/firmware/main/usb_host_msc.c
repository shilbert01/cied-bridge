// SPDX-License-Identifier: MPL-2.0
// Sender USB: present SD card as a FAT32 USB drive to the programmer.
// During file transfer, VFS is mounted locally (host sees BECOMING_READY).
// After transfer, UNIT_ATTENTION tells the host to re-read the filesystem.

#include <string.h>
#include <sys/stat.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tinyusb.h"
#include "tusb_msc_storage.h"
#include "esp_log.h"
#include "bridge_lite.h"

#ifdef CONFIG_BRIDGE_LITE_ROLE_SENDER

static const char *TAG = "usb-ingest";

static void msc_mount_changed_cb(tinyusb_msc_event_t *event)
{
    if (event->mount_changed_data.is_mounted) {
        ESP_LOGI(TAG, "VFS mounted (local access)");
        sd_set_vfs_mounted(true);
    } else {
        ESP_LOGI(TAG, "VFS unmounted (USB host access)");
        sd_set_vfs_mounted(false);
    }
}

esp_err_t usb_host_msc_init(void)
{
    sdmmc_card_t *card = sd_get_card();
    if (!card) {
        ESP_LOGE(TAG, "SD card not initialised");
        return ESP_FAIL;
    }

    const tinyusb_msc_sdmmc_config_t sdmmc_cfg = {
        .card = card,
        .callback_mount_changed = msc_mount_changed_cb,
        .mount_config = {
            .format_if_mount_failed = false,
            .max_files = 5,
            .allocation_unit_size = 16 * 1024,
        },
    };
    ESP_ERROR_CHECK(tinyusb_msc_storage_init_sdmmc(&sdmmc_cfg));

    tinyusb_msc_storage_mount(SD_MOUNT);
    mkdir("/sd/done", 0775);
    tinyusb_msc_storage_unmount();

    static const char *str_desc[] = {
        "",
        "CIED Bridge",
        "CIED Sender",
        CONFIG_BRIDGE_LITE_SENDER_ID,
    };
    const tinyusb_config_t tusb_cfg = {
        .device_descriptor = NULL,
        .string_descriptor = str_desc,
        .string_descriptor_count = 4,
        .external_phy = false,
        .configuration_descriptor = NULL,
    };
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    ESP_LOGI(TAG, "USB MSC ready — programmer can write files to SD");
    return ESP_OK;
}

esp_err_t usb_msc_claim_for_local(void)
{
    esp_err_t ret = tinyusb_msc_storage_mount(SD_MOUNT);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "VFS mount failed (0x%x)", ret);
    }
    return ret;
}

void usb_msc_release_to_host(void)
{
    tinyusb_msc_storage_unmount();
    tud_disconnect();
    vTaskDelay(pdMS_TO_TICKS(500));
    tud_connect();
    ESP_LOGI(TAG, "VFS released — USB reconnected, host will re-mount");
}

#endif
