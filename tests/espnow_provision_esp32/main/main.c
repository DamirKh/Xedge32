#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs.h"
#include "nvs_flash.h"

#define TAG "PROV"
#define PROV_GPIO GPIO_NUM_4
#define CFG_MAGIC 0x50524f56u
#define PROV_TIMEOUT_MS 30000
#define PROV_CHANNEL_DWELL_MS 350

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t controller[ESP_NOW_ETH_ALEN];
    uint8_t channel;
    uint8_t token[4];
} prov_cfg_t;

static SemaphoreHandle_t s_send_done;
static volatile esp_now_send_status_t s_send_status;
static volatile bool s_prov_found;
static uint8_t s_prov_controller[ESP_NOW_ETH_ALEN];
static uint8_t s_prov_token[4];
static uint8_t s_prov_channel;

static uint8_t s_sta_mac[ESP_NOW_ETH_ALEN];

static void send_cb(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    (void)info;
    s_send_status = status;
    if (s_send_done) {
        xSemaphoreGive(s_send_done);
    }
}

static void recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (!info || !data || len < 16) {
        return;
    }
    if (data[0] == 'P' && data[1] == 'R' && data[2] == 'O' && data[3] == 'V') {
        memcpy(s_prov_controller, info->src_addr, ESP_NOW_ETH_ALEN);
        memcpy(s_prov_token, data + 11, sizeof(s_prov_token));
        s_prov_channel = data[15];
        s_prov_found = true;
    }
}

static esp_err_t add_controller_peer(const uint8_t *mac)
{
    esp_now_peer_info_t peer;
    memset(&peer, 0, sizeof(peer));
    memcpy(peer.peer_addr, mac, ESP_NOW_ETH_ALEN);
    peer.channel = 0;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;
    esp_err_t err = esp_now_add_peer(&peer);
    if (err == ESP_ERR_ESPNOW_EXIST) {
        err = esp_now_mod_peer(&peer);
    }
    return err;
}

static bool send_wait(const uint8_t *mac, const uint8_t *data, size_t len, int timeout_ms)
{
    while (xSemaphoreTake(s_send_done, 0) == pdTRUE) {
    }
    esp_err_t err = esp_now_send(mac, data, len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "send error %s", esp_err_to_name(err));
        return false;
    }
    if (xSemaphoreTake(s_send_done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        ESP_LOGW(TAG, "send timeout");
        return false;
    }
    return s_send_status == ESP_NOW_SEND_SUCCESS;
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_disconnect());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, s_sta_mac));
}

static esp_err_t load_cfg(prov_cfg_t *cfg)
{
    nvs_handle_t nvs;
    size_t len = sizeof(*cfg);
    esp_err_t err = nvs_open("prov", NVS_READONLY, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_blob(nvs, "cfg", cfg, &len);
    nvs_close(nvs);
    if (err != ESP_OK || len != sizeof(*cfg) || cfg->magic != CFG_MAGIC) {
        return ESP_ERR_NOT_FOUND;
    }
    return ESP_OK;
}

static void save_cfg(const prov_cfg_t *cfg)
{
    nvs_handle_t nvs;
    ESP_ERROR_CHECK(nvs_open("prov", NVS_READWRITE, &nvs));
    ESP_ERROR_CHECK(nvs_set_blob(nvs, "cfg", cfg, sizeof(*cfg)));
    ESP_ERROR_CHECK(nvs_commit(nvs));
    nvs_close(nvs);
}

static bool gpio_provision_requested(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PROV_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    vTaskDelay(pdMS_TO_TICKS(20));
    return gpio_get_level(PROV_GPIO) == 0;
}

static bool provision(void)
{
    ESP_LOGI(TAG, "provisioning: listening for controller broadcast");
    s_prov_found = false;
    int64_t deadline = esp_timer_get_time() + (int64_t)PROV_TIMEOUT_MS * 1000;
    while (esp_timer_get_time() < deadline && !s_prov_found) {
        for (int ch = 1; ch <= 13 && !s_prov_found; ++ch) {
            esp_err_t err = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "channel %d: %s", ch, esp_err_to_name(err));
            }
            vTaskDelay(pdMS_TO_TICKS(PROV_CHANNEL_DWELL_MS));
        }
    }
    if (!s_prov_found) {
        ESP_LOGE(TAG, "provisioning timeout");
        return false;
    }
    prov_cfg_t cfg = {
        .magic = CFG_MAGIC,
        .channel = s_prov_channel,
        .token = {0},
    };
    memcpy(cfg.controller, s_prov_controller, sizeof(cfg.controller));
    memcpy(cfg.token, s_prov_token, sizeof(cfg.token));
    save_cfg(&cfg);
    ESP_LOGI(TAG, "provisioned: controller %02x:%02x:%02x:%02x:%02x:%02x channel %u",
             cfg.controller[0], cfg.controller[1], cfg.controller[2],
             cfg.controller[3], cfg.controller[4], cfg.controller[5], cfg.channel);

    ESP_ERROR_CHECK(esp_wifi_set_channel(cfg.channel, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(add_controller_peer(cfg.controller));
    uint8_t pok[14];
    memcpy(pok, "POK", 3);
    pok[3] = 1;
    memcpy(pok + 4, s_sta_mac, ESP_NOW_ETH_ALEN);
    memcpy(pok + 10, cfg.token, sizeof(cfg.token));
    bool pok_ok = false;
    for (int attempt = 0; attempt < 3 && !pok_ok; ++attempt) {
        pok_ok = send_wait(cfg.controller, pok, sizeof(pok), 150);
    }
    ESP_LOGI(TAG, "POK ok=%d", pok_ok);
    return true;
}

static void send_test_data(const prov_cfg_t *cfg)
{
    ESP_ERROR_CHECK(esp_wifi_set_channel(cfg->channel, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(add_controller_peer(cfg->controller));
    uint8_t msg[32];
    for (uint32_t seq = 1; seq <= 20; ++seq) {
        memset(msg, 0, sizeof(msg));
        memcpy(msg, "XNW1", 4);
        uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        memcpy(msg + 4, &seq, sizeof(seq));
        memcpy(msg + 8, &now, sizeof(now));
        bool ok = false;
        for (int attempt = 0; attempt < 3 && !ok; ++attempt) {
            ok = send_wait(cfg->controller, msg, sizeof(msg), 150);
        }
        ESP_LOGI(TAG, "DATA seq=%u ok=%d channel=%u", seq, ok, cfg->channel);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ESP_LOGI(TAG, "DATA done");
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    prov_cfg_t cfg;
    bool have_cfg = load_cfg(&cfg) == ESP_OK;
    bool force_prov = gpio_provision_requested();

    wifi_init();
    ESP_LOGI(TAG, "boot mac=%02x:%02x:%02x:%02x:%02x:%02x cfg=%d gpio=%d",
             s_sta_mac[0], s_sta_mac[1], s_sta_mac[2], s_sta_mac[3],
             s_sta_mac[4], s_sta_mac[5], have_cfg, force_prov);
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_send_cb(send_cb));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(recv_cb));
    s_send_done = xSemaphoreCreateBinary();
    configASSERT(s_send_done != NULL);

    if (!have_cfg || force_prov) {
        if (!provision()) {
            ESP_LOGE(TAG, "not provisioned; rebooting in 10s");
            vTaskDelay(pdMS_TO_TICKS(10000));
            esp_restart();
        }
    }

    if (load_cfg(&cfg) != ESP_OK) {
        ESP_LOGE(TAG, "configuration missing");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    send_test_data(&cfg);
    ESP_LOGI(TAG, "test finished");
}
