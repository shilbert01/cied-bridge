// SPDX-License-Identifier: MPL-2.0
// Receiver USB: present SD card as a FAT32 USB drive to the office PC.
// Files received over ESP-NOW are written via VFS; PC sees them on refresh.

#include <string.h>
#include <sys/stat.h>
#include "tinyusb.h"
#include "tusb_msc_storage.h"
#include "esp_log.h"
#include "bridge_lite.h"

#ifdef CONFIG_BRIDGE_LITE_ROLE_RECEIVER

static const char *TAG = "usb-present";

static void msc_mount_changed_cb(tinyusb_msc_event_t *event)
{
    if (event->mount_changed_data.is_mounted) {
        ESP_LOGI(TAG, "VFS mounted (local write access)");
        sd_set_vfs_mounted(true);
    } else {
        ESP_LOGI(TAG, "VFS unmounted (USB host access)");
        sd_set_vfs_mounted(false);
    }
}

esp_err_t usb_msc_device_init(void)
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

    // Mount VFS to create dirs, then unmount so PC sees the drive
    tinyusb_msc_storage_mount(SD_MOUNT);
    sd_set_vfs_mounted(true);
    mkdir("/sd/inbox", 0775);
    tinyusb_msc_storage_unmount();
    sd_set_vfs_mounted(false);

    static const char *str_desc[] = {
        "",
        "CIED Bridge",
        "CIED Receiver",
        "receiver",
    };
    const tinyusb_config_t tusb_cfg = {
        .device_descriptor = NULL,
        .string_descriptor = str_desc,
        .string_descriptor_count = 4,
        .external_phy = false,
        .configuration_descriptor = NULL,
    };
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    ESP_LOGI(TAG, "USB MSC ready — PC sees a USB drive");
    return ESP_OK;
}

// Claim VFS for writing an incoming ESP-NOW file
esp_err_t usb_msc_claim_for_local(void)
{
    esp_err_t ret = tinyusb_msc_storage_mount(SD_MOUNT);
    if (ret == ESP_OK) sd_set_vfs_mounted(true);
    return ret;
}

// Release VFS back to USB host after writing
void usb_msc_release_to_host(void)
{
    tinyusb_msc_storage_unmount();
    sd_set_vfs_mounted(false);
}

#endif
