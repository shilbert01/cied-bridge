// SPDX-License-Identifier: MPL-2.0
// SD/TF card — SDMMC init only (no VFS mount here).
// VFS mount/unmount is owned by tinyusb_msc_storage.

#include <string.h>
#include <sys/stat.h>
#include <dirent.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdmmc_cmd.h"
#include "driver/sdmmc_host.h"
#include "esp_log.h"
#include "bridge_lite.h"

static const char *TAG = "sd";
const char *SD_MOUNT = "/sd";
static sdmmc_card_t *s_card = NULL;
static bool s_vfs_mounted = false;
static FILE *s_write_fp = NULL;

// T-Dongle-S3 SDMMC pins (4-bit mode)
#define SD_CLK   12
#define SD_CMD   16
#define SD_D0    14
#define SD_D1    17
#define SD_D2    21
#define SD_D3    18

#define SD_INIT_RETRIES 5

esp_err_t sd_init(void)
{
    vTaskDelay(pdMS_TO_TICKS(1000));

    s_card = (sdmmc_card_t *)malloc(sizeof(sdmmc_card_t));
    if (!s_card) return ESP_ERR_NO_MEM;

    esp_err_t ret = ESP_FAIL;
    int widths[] = {4, 1, 1, 1, 1};

    for (int attempt = 0; attempt < SD_INIT_RETRIES; attempt++) {
        char msg[16];
        snprintf(msg, sizeof(msg), "try %d/%d %db", attempt + 1, SD_INIT_RETRIES, widths[attempt]);
        lcd_update("CIED Bridge", "SD INIT", msg, "");

        sdmmc_host_t host = SDMMC_HOST_DEFAULT();
        host.slot = SDMMC_HOST_SLOT_1;
        host.max_freq_khz = SDMMC_FREQ_DEFAULT;

        sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
        slot.clk   = SD_CLK;
        slot.cmd   = SD_CMD;
        slot.d0    = SD_D0;
        slot.d1    = SD_D1;
        slot.d2    = SD_D2;
        slot.d3    = SD_D3;
        slot.cd    = SDMMC_SLOT_NO_CD;
        slot.wp    = SDMMC_SLOT_NO_WP;
        slot.width = widths[attempt];
        slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;

        ret = sdmmc_host_init();
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "host init attempt %d failed: %s", attempt + 1, esp_err_to_name(ret));
            goto retry;
        }

        ret = sdmmc_host_init_slot(host.slot, &slot);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "slot init attempt %d failed: %s", attempt + 1, esp_err_to_name(ret));
            sdmmc_host_deinit();
            goto retry;
        }

        memset(s_card, 0, sizeof(sdmmc_card_t));
        ret = sdmmc_card_init(&host, s_card);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "card init OK on attempt %d (%d-bit)", attempt + 1, widths[attempt]);
            break;
        }

        ESP_LOGW(TAG, "card init attempt %d/%d failed: %s",
                 attempt + 1, SD_INIT_RETRIES, esp_err_to_name(ret));
        sdmmc_host_deinit();

retry:
        if (attempt < SD_INIT_RETRIES - 1) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "SD card init failed after %d attempts", SD_INIT_RETRIES);
        lcd_update("CIED Bridge", "SD FAIL", esp_err_to_name(ret), "reboot");
        free(s_card);
        s_card = NULL;
        return ret;
    }

    sdmmc_card_print_info(stdout, s_card);
    ESP_LOGI(TAG, "SD card ready (VFS not mounted — owned by MSC storage)");
    return ESP_OK;
}

sdmmc_card_t *sd_get_card(void)
{
    return s_card;
}

void sd_set_vfs_mounted(bool mounted)
{
    s_vfs_mounted = mounted;
}

bool sd_ready(void)
{
    return s_card != NULL && s_vfs_mounted;
}

esp_err_t sd_open_write(const char *path)
{
    if (!s_vfs_mounted) return ESP_FAIL;
    if (s_write_fp) fclose(s_write_fp);
    s_write_fp = fopen(path, "wb");
    return s_write_fp ? ESP_OK : ESP_FAIL;
}

esp_err_t sd_append(const uint8_t *data, size_t len)
{
    if (!s_write_fp) return ESP_FAIL;
    return (fwrite(data, 1, len, s_write_fp) == len) ? ESP_OK : ESP_FAIL;
}

esp_err_t sd_close_write(void)
{
    if (!s_write_fp) return ESP_OK;
    int ret = fclose(s_write_fp);
    s_write_fp = NULL;
    return ret == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t sd_write_file(const char *path, const uint8_t *data, size_t len)
{
    if (!s_vfs_mounted) return ESP_FAIL;
    FILE *f = fopen(path, "wb");
    if (!f) return ESP_FAIL;
    size_t written = fwrite(data, 1, len, f);
    fclose(f);
    return (written == len) ? ESP_OK : ESP_FAIL;
}

esp_err_t sd_read_file(const char *path, uint8_t *buf, size_t buf_len, size_t *out_len)
{
    if (!s_vfs_mounted) return ESP_FAIL;
    FILE *f = fopen(path, "rb");
    if (!f) return ESP_FAIL;
    *out_len = fread(buf, 1, buf_len, f);
    fclose(f);
    return ESP_OK;
}

esp_err_t sd_delete_file(const char *path)
{
    if (!s_vfs_mounted) return ESP_FAIL;
    return remove(path) == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t sd_list_files(const char *dir, void (*cb)(const char *name, uint32_t size))
{
    if (!s_vfs_mounted) return ESP_FAIL;
    DIR *d = opendir(dir);
    if (!d) return ESP_FAIL;
    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        if (ent->d_type == DT_REG) {
            char fullpath[280];
            snprintf(fullpath, sizeof(fullpath), "%s/%s", dir, ent->d_name);
            struct stat st;
            stat(fullpath, &st);
            cb(ent->d_name, (uint32_t)st.st_size);
        }
    }
    closedir(d);
    return ESP_OK;
}
