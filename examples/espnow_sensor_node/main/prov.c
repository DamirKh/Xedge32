#include "prov.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"

#include "link.h"

#define TAG "PROV"

#define PROV_MAGIC          0x50524f56u
#define PROV_NVS_NAMESPACE  "prov"
#define PROV_NVS_KEY        "cfg"
#define PROV_TIMEOUT_MS     30000
#define PROV_DWELL_MS       350

static volatile bool s_found;
static uint8_t s_controller[6];
static uint8_t s_token[4];
static uint8_t s_channel;

bool prov_valid(const node_prov_cfg_t *cfg)
{
    return cfg->magic == PROV_MAGIC &&
           cfg->channel >= 1 && cfg->channel <= 13;
}

esp_err_t prov_load(node_prov_cfg_t *cfg)
{
    nvs_handle_t nvs;
    size_t len = sizeof(*cfg);

    esp_err_t err = nvs_open(PROV_NVS_NAMESPACE, NVS_READONLY, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_blob(nvs, PROV_NVS_KEY, cfg, &len);
    nvs_close(nvs);

    if (err != ESP_OK || len != sizeof(*cfg) || !prov_valid(cfg)) {
        return ESP_ERR_NOT_FOUND;
    }
    return ESP_OK;
}

esp_err_t prov_save(const node_prov_cfg_t *cfg)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(PROV_NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(nvs, PROV_NVS_KEY, cfg, sizeof(*cfg));
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    return err;
}

static void prov_recv(const uint8_t *src, const uint8_t *data, int len)
{
    if (len < 16 || memcmp(data, "PROV", 4) != 0) {
        return;
    }

    uint8_t channel = data[15];
    if (channel < 1 || channel > 13) {
        return;
    }

    memcpy(s_controller, src, 6);
    memcpy(s_token, data + 11, sizeof(s_token));
    s_channel = channel;
    s_found = true;
}

void prov_run_forever(node_prov_cfg_t *cfg)
{
    link_set_recv_handler(prov_recv);

    for (;;) {
        s_found = false;
        ESP_LOGI(TAG, "scanning channels 1..13 for PROV");

        int64_t deadline = esp_timer_get_time() + (int64_t)PROV_TIMEOUT_MS * 1000;
        while (!s_found && esp_timer_get_time() < deadline) {
            for (int channel = 1; channel <= 13 && !s_found; ++channel) {
                esp_err_t err = link_set_channel((uint8_t)channel);
                if (err != ESP_OK) {
                    ESP_LOGW(TAG, "channel %d: %s", channel, esp_err_to_name(err));
                }
                vTaskDelay(pdMS_TO_TICKS(PROV_DWELL_MS));
            }
        }

        if (!s_found) {
            ESP_LOGW(TAG, "no controller yet, retrying");
            continue;
        }

        cfg->magic = PROV_MAGIC;
        memcpy(cfg->controller, s_controller, sizeof(cfg->controller));
        cfg->channel = s_channel;
        memcpy(cfg->token, s_token, sizeof(cfg->token));

        ESP_ERROR_CHECK(link_set_channel(cfg->channel));
        ESP_ERROR_CHECK(link_add_peer(cfg->controller));

        esp_err_t save_err = prov_save(cfg);
        if (save_err != ESP_OK) {
            ESP_LOGE(TAG, "cannot save provisioning record: %s",
                     esp_err_to_name(save_err));
        }

        ESP_LOGI(TAG, "provisioned: controller %02x:%02x:%02x:%02x:%02x:%02x channel %u",
                 cfg->controller[0], cfg->controller[1], cfg->controller[2],
                 cfg->controller[3], cfg->controller[4], cfg->controller[5],
                 cfg->channel);

        uint8_t pok[14];
        memcpy(pok, "POK", 3);
        pok[3] = 1;
        memcpy(pok + 4, link_mac(), 6);
        memcpy(pok + 10, cfg->token, sizeof(cfg->token));

        bool ok = false;
        for (int attempt = 0; attempt < 3 && !ok; ++attempt) {
            ok = link_send(cfg->controller, pok, sizeof(pok), 1, 150);
        }
        ESP_LOGI(TAG, "POK ok=%d", ok);

        link_set_recv_handler(NULL);
        return;
    }
}
