// SPDX-License-Identifier: MPL-2.0
// File transfer over TCP — sender pushes files, receiver writes to SD

#include <string.h>
#include <strings.h>
#include <stdio.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "mbedtls/sha256.h"
#include "bridge_lite.h"

static const char *TAG = "proto";

#define TCP_PORT      5000
#define RECEIVER_IP   "192.168.4.1"
#define BUF_SIZE      4096

// Wire format: [2B name_len][name][4B file_size][32B sha256][raw data]
// Response:    [1B] 0x01=OK, 0x00=FAIL

static int recv_exact(int sock, void *buf, size_t len)
{
    size_t got = 0;
    while (got < len) {
        int n = recv(sock, (uint8_t *)buf + got, len - got, 0);
        if (n <= 0) return -1;
        got += n;
    }
    return 0;
}

static int send_all(int sock, const void *buf, size_t len)
{
    size_t sent = 0;
    while (sent < len) {
        int n = send(sock, (const uint8_t *)buf + sent, len - sent, 0);
        if (n <= 0) return -1;
        sent += n;
    }
    return 0;
}

// ---- Sender side ----

#ifdef CONFIG_BRIDGE_LITE_ROLE_SENDER

static bool is_export_file(const char *name)
{
    size_t len = strlen(name);
    if (len < 5) return false;
    const char *ext = name + len - 4;
    return (strcasecmp(ext, ".pdf") == 0 ||
            strcasecmp(ext, ".xml") == 0);
}

static esp_err_t send_file(const char *filepath, const char *filename)
{
    FILE *f = fopen(filepath, "rb");
    if (!f) return ESP_FAIL;

    fseek(f, 0, SEEK_END);
    uint32_t total = (uint32_t)ftell(f);
    fseek(f, 0, SEEK_SET);

    mbedtls_sha256_context sha_ctx;
    mbedtls_sha256_init(&sha_ctx);
    mbedtls_sha256_starts(&sha_ctx, 0);
    uint8_t buf[BUF_SIZE];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0)
        mbedtls_sha256_update(&sha_ctx, buf, n);
    uint8_t sha256[SHA256_LEN];
    mbedtls_sha256_finish(&sha_ctx, sha256);
    mbedtls_sha256_free(&sha_ctx);
    fseek(f, 0, SEEK_SET);

    char tagged[MAX_FILENAME_LEN];
    snprintf(tagged, sizeof(tagged), "%s-%s", CONFIG_BRIDGE_LITE_SENDER_ID, filename);
    uint16_t name_len = strlen(tagged);

    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock < 0) { fclose(f); return ESP_FAIL; }

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(TCP_PORT),
    };
    inet_pton(AF_INET, RECEIVER_IP, &addr.sin_addr);

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGW(TAG, "TCP connect failed");
        close(sock);
        fclose(f);
        return ESP_FAIL;
    }

    led_set(LED_BLUE_STEADY);
    lcd_update("CIED Bridge", "SENDER", filename, "sending...");

    uint8_t hdr[2] = { name_len >> 8, name_len & 0xFF };
    send_all(sock, hdr, 2);
    send_all(sock, tagged, name_len);
    uint8_t size_buf[4] = { total >> 24, total >> 16, total >> 8, total };
    send_all(sock, size_buf, 4);
    send_all(sock, sha256, SHA256_LEN);

    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        if (send_all(sock, buf, n) < 0) {
            ESP_LOGE(TAG, "send error");
            close(sock);
            fclose(f);
            led_set(LED_RED_STEADY);
            return ESP_FAIL;
        }
    }
    fclose(f);

    uint8_t result = 0;
    recv_exact(sock, &result, 1);
    close(sock);

    if (result == 0x01) {
        led_set(LED_GREEN_FLASH);
        lcd_update("CIED Bridge", "SENDER", filename, "sent OK");
        ESP_LOGI(TAG, "sent %s (%lu bytes)", tagged, (unsigned long)total);
        vTaskDelay(pdMS_TO_TICKS(1000));
        led_set(LED_GREEN_STEADY);
        return ESP_OK;
    }

    ESP_LOGW(TAG, "receiver rejected %s", tagged);
    led_set(LED_RED_STEADY);
    lcd_update("CIED Bridge", "SENDER", filename, "REJECTED");
    return ESP_FAIL;
}

static volatile bool s_scan_requested = false;

#define BOOT_BUTTON_GPIO  0

static void IRAM_ATTR boot_button_isr(void *arg)
{
    s_scan_requested = true;
}

static void sender_task(void *arg)
{
    gpio_config_t btn = {
        .pin_bit_mask = 1ULL << BOOT_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_NEGEDGE,
    };
    gpio_config(&btn);
    gpio_install_isr_service(0);
    gpio_isr_handler_add(BOOT_BUTTON_GPIO, boot_button_isr, NULL);

    while (1) {
        if (!s_scan_requested || !wifi_is_connected()) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        s_scan_requested = false;

        ESP_LOGI(TAG, "scan triggered — claiming SD card");
        lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, "scanning...");

        if (usb_msc_claim_for_local() != ESP_OK) {
            ESP_LOGW(TAG, "could not claim VFS");
            lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, "claim failed");
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        bool sent_any = false;
        int count = 0;
        DIR *d = opendir("/sd");
        if (d) {
            struct dirent *ent;
            while ((ent = readdir(d)) != NULL) {
                if (ent->d_type != DT_REG) continue;
                if (!is_export_file(ent->d_name)) continue;
                char src[280], dst[280];
                snprintf(src, sizeof(src), "/sd/%s", ent->d_name);
                snprintf(dst, sizeof(dst), "/sd/done/%s", ent->d_name);

                if (send_file(src, ent->d_name) == ESP_OK) {
                    rename(src, dst);
                    sent_any = true;
                    count++;
                }
            }
            closedir(d);
        }

        usb_msc_release_to_host();

        if (sent_any) {
            char msg[16];
            snprintf(msg, sizeof(msg), "sent %d file%s", count, count > 1 ? "s" : "");
            lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, msg);
            ESP_LOGI(TAG, "sent %d file(s) — card released to USB", count);
        } else {
            lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, "no files");
            ESP_LOGI(TAG, "no files found — card released to USB");
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
        lcd_update("CIED Bridge", "SENDER", CONFIG_BRIDGE_LITE_SENDER_ID, "press BTN");
    }
}

esp_err_t file_sender_init(void)
{
    xTaskCreate(sender_task, "sender", 8192, NULL, 5, NULL);
    return ESP_OK;
}

#endif // CONFIG_BRIDGE_LITE_ROLE_SENDER

// ---- Receiver side ----

#ifdef CONFIG_BRIDGE_LITE_ROLE_RECEIVER

static void handle_client(int sock)
{
    uint8_t hdr[2];
    if (recv_exact(sock, hdr, 2) < 0) return;
    uint16_t name_len = (hdr[0] << 8) | hdr[1];
    if (name_len == 0 || name_len >= MAX_FILENAME_LEN) return;

    char filename[MAX_FILENAME_LEN];
    if (recv_exact(sock, filename, name_len) < 0) return;
    filename[name_len] = '\0';

    uint8_t size_buf[4];
    if (recv_exact(sock, size_buf, 4) < 0) return;
    uint32_t file_size = ((uint32_t)size_buf[0] << 24) | ((uint32_t)size_buf[1] << 16) |
                         ((uint32_t)size_buf[2] << 8)  | size_buf[3];

    uint8_t expected_sha[SHA256_LEN];
    if (recv_exact(sock, expected_sha, SHA256_LEN) < 0) return;

    ESP_LOGI(TAG, "receiving %s (%lu bytes)", filename, (unsigned long)file_size);
    led_set(LED_BLUE_STEADY);
    lcd_update("CIED Bridge", "RECEIVER", filename, "receiving...");

    usb_msc_claim_for_local();

    char path[280];
    snprintf(path, sizeof(path), "/sd/inbox/%s", filename);
    FILE *f = fopen(path, "wb");
    if (!f) {
        ESP_LOGE(TAG, "can't open %s", path);
        uint8_t fail = 0x00;
        send(sock, &fail, 1, 0);
        usb_msc_release_to_host();
        led_set(LED_RED_STEADY);
        return;
    }

    mbedtls_sha256_context sha_ctx;
    mbedtls_sha256_init(&sha_ctx);
    mbedtls_sha256_starts(&sha_ctx, 0);

    uint8_t buf[BUF_SIZE];
    uint32_t remaining = file_size;
    bool ok = true;
    while (remaining > 0) {
        size_t to_read = remaining > sizeof(buf) ? sizeof(buf) : remaining;
        int n = recv(sock, buf, to_read, 0);
        if (n <= 0) { ok = false; break; }
        fwrite(buf, 1, n, f);
        mbedtls_sha256_update(&sha_ctx, buf, n);
        remaining -= n;
    }
    fclose(f);

    uint8_t computed[SHA256_LEN];
    mbedtls_sha256_finish(&sha_ctx, computed);
    mbedtls_sha256_free(&sha_ctx);

    uint8_t result;
    if (ok && memcmp(computed, expected_sha, SHA256_LEN) == 0) {
        result = 0x01;
        ESP_LOGI(TAG, "file complete, SHA-256 OK");
        led_set(LED_GREEN_FLASH);
        lcd_update("CIED Bridge", "RECEIVER", filename, "received OK");
    } else {
        result = 0x00;
        ESP_LOGW(TAG, "transfer failed (ok=%d, sha=%s)", ok,
                 ok ? "MISMATCH" : "incomplete");
        remove(path);
        led_set(LED_RED_STEADY);
        lcd_update("CIED Bridge", "RECEIVER", filename, ok ? "SHA MISMATCH" : "INCOMPLETE");
    }

    send(sock, &result, 1, 0);
    usb_msc_release_to_host();

    vTaskDelay(pdMS_TO_TICKS(1000));
    led_set(LED_GREEN_STEADY);
}

static void tcp_server_task(void *arg)
{
    int listen_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listen_fd < 0) {
        ESP_LOGE(TAG, "socket create failed");
        vTaskDelete(NULL);
        return;
    }

    int opt = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(TCP_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGE(TAG, "bind failed");
        close(listen_fd);
        vTaskDelete(NULL);
        return;
    }
    listen(listen_fd, 2);
    ESP_LOGI(TAG, "TCP server listening on port %d", TCP_PORT);

    while (1) {
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        int client = accept(listen_fd, (struct sockaddr *)&client_addr, &addr_len);
        if (client < 0) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        ESP_LOGI(TAG, "client connected");
        handle_client(client);
        close(client);
    }
}

esp_err_t file_receiver_init(void)
{
    xTaskCreate(tcp_server_task, "tcp_srv", 8192, NULL, 5, NULL);
    return ESP_OK;
}

#endif // CONFIG_BRIDGE_LITE_ROLE_RECEIVER
