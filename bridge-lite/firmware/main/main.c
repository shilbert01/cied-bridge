// SPDX-License-Identifier: MPL-2.0
// CIED Bridge Lite — WiFi file relay for small clinics
// Role selected at build time via menuconfig

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "bridge_lite.h"

static const char *TAG = "bridge-lite";

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_event_loop_create_default());

    led_init();
    led_set(LED_RED_STEADY);
    lcd_init();

    ret = sd_init();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "SD not ready (%s) — insert TF card and reboot", esp_err_to_name(ret));
    }

    if (sd_get_card()) {
#ifdef CONFIG_BRIDGE_LITE_ROLE_SENDER
        sd_set_volume_label("CIED Sender");
#else
        sd_set_volume_label("CIED Receiver");
#endif
    }

    ESP_ERROR_CHECK(wifi_transport_init());

#ifdef CONFIG_BRIDGE_LITE_ROLE_SENDER
    ESP_LOGI(TAG, "role: SENDER (id=%s)", CONFIG_BRIDGE_LITE_SENDER_ID);
    lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, "starting...");
    if (sd_get_card()) {
        ESP_ERROR_CHECK(usb_host_msc_init());
    } else {
        ESP_LOGW(TAG, "USB MSC skipped — no SD card");
    }
    ESP_ERROR_CHECK(file_sender_init());
#else
    ESP_LOGI(TAG, "role: RECEIVER");
    lcd_update("CIED Bridge", "RECEIVER", "", "starting...");
    if (sd_get_card()) {
        ESP_ERROR_CHECK(usb_msc_device_init());
    } else {
        ESP_LOGW(TAG, "USB MSC skipped — no SD card");
    }
    ESP_ERROR_CHECK(file_receiver_init());
#endif

    if (sd_get_card()) {
        led_set(LED_GREEN_STEADY);
    }

#ifdef CONFIG_BRIDGE_LITE_ROLE_SENDER
    lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID,
               sd_get_card() ? "press BTN" : "no SD");
#else
    lcd_update("CIED Bridge", "RECEIVER",
               sd_get_card() ? "SD OK" : "no SD", "listening");
#endif
    ESP_LOGI(TAG, "ready (sd=%s)", sd_get_card() ? "OK" : "FAIL");
}
