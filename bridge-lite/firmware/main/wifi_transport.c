// SPDX-License-Identifier: MPL-2.0
// WiFi transport — receiver runs SoftAP, sender connects as STA

#include <string.h>
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "bridge_lite.h"

static const char *TAG = "wifi";
static volatile bool s_connected = false;

#ifdef CONFIG_BRIDGE_LITE_ROLE_RECEIVER

esp_err_t wifi_transport_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    wifi_config_t ap_cfg = {};
    strncpy((char *)ap_cfg.ap.ssid, CONFIG_BRIDGE_LITE_WIFI_SSID, sizeof(ap_cfg.ap.ssid));
    ap_cfg.ap.ssid_len = strlen(CONFIG_BRIDGE_LITE_WIFI_SSID);
    strncpy((char *)ap_cfg.ap.password, CONFIG_BRIDGE_LITE_WIFI_PASS, sizeof(ap_cfg.ap.password));
    ap_cfg.ap.channel = 1;
    ap_cfg.ap.max_connection = 4;
    ap_cfg.ap.authmode = WIFI_AUTH_WPA3_PSK;
    ap_cfg.ap.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    s_connected = true;
    ESP_LOGI(TAG, "SoftAP started: %s", CONFIG_BRIDGE_LITE_WIFI_SSID);
    return ESP_OK;
}

#else

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT) {
        if (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED) {
            s_connected = false;
            lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, "connecting...");
            esp_wifi_connect();
            ESP_LOGI(TAG, "connecting to %s ...", CONFIG_BRIDGE_LITE_WIFI_SSID);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        s_connected = true;
        ESP_LOGI(TAG, "connected — IP " IPSTR, IP2STR(&evt->ip_info.ip));
        lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, "connected");
    }
}

esp_err_t wifi_transport_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL));

    wifi_config_t sta_cfg = {};
    strncpy((char *)sta_cfg.sta.ssid, CONFIG_BRIDGE_LITE_WIFI_SSID, sizeof(sta_cfg.sta.ssid));
    strncpy((char *)sta_cfg.sta.password, CONFIG_BRIDGE_LITE_WIFI_PASS, sizeof(sta_cfg.sta.password));
    sta_cfg.sta.threshold.authmode = WIFI_AUTH_WPA3_PSK;
    sta_cfg.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "STA init — connecting to %s", CONFIG_BRIDGE_LITE_WIFI_SSID);
    return ESP_OK;
}

#endif

bool wifi_is_connected(void)
{
    return s_connected;
}
