#include "link.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"

#define TAG "LINK"

static uint8_t s_mac[6];
static SemaphoreHandle_t s_send_done;
static volatile esp_now_send_status_t s_send_status;
static volatile link_recv_fn s_recv_fn;

static void send_cb(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    (void)info;
    s_send_status = status;
    if (s_send_done != NULL) {
        xSemaphoreGive(s_send_done);
    }
}

static void recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (info == NULL || data == NULL || len <= 0) {
        return;
    }
    link_recv_fn fn = s_recv_fn;
    if (fn != NULL) {
        fn(info->src_addr, data, len);
    }
}

esp_err_t link_init(void)
{
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    s_send_done = xSemaphoreCreateBinary();
    if (s_send_done == NULL) {
        return ESP_ERR_NO_MEM;
    }

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_disconnect());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, s_mac));

    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_send_cb(send_cb));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(recv_cb));

    ESP_LOGI(TAG, "wifi ready, mac %02x:%02x:%02x:%02x:%02x:%02x",
             s_mac[0], s_mac[1], s_mac[2], s_mac[3], s_mac[4], s_mac[5]);
    return ESP_OK;
}

const uint8_t *link_mac(void)
{
    return s_mac;
}

esp_err_t link_set_channel(uint8_t channel)
{
    return esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
}

esp_err_t link_add_peer(const uint8_t mac[6])
{
    esp_now_peer_info_t peer;
    memset(&peer, 0, sizeof(peer));
    memcpy(peer.peer_addr, mac, 6);
    peer.channel = 0;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;

    esp_err_t err = esp_now_add_peer(&peer);
    if (err == ESP_ERR_ESPNOW_EXIST) {
        err = esp_now_mod_peer(&peer);
    }
    return err;
}

void link_set_recv_handler(link_recv_fn fn)
{
    s_recv_fn = fn;
}

bool link_send(const uint8_t mac[6], const void *data, size_t len,
               int retries, int timeout_ms)
{
    if (retries < 1) {
        retries = 1;
    }

    for (int attempt = 0; attempt < retries; ++attempt) {
        while (xSemaphoreTake(s_send_done, 0) == pdTRUE) {
        }

        esp_err_t err = esp_now_send(mac, data, len);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "send error: %s", esp_err_to_name(err));
            continue;
        }

        if (xSemaphoreTake(s_send_done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
            ESP_LOGW(TAG, "send timeout");
            continue;
        }

        if (s_send_status == ESP_NOW_SEND_SUCCESS) {
            return true;
        }
    }

    return false;
}
