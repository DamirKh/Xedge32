#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#include "battery.h"
#include "board.h"
#include "led.h"
#include "link.h"
#include "payload.h"
#include "prov.h"
#include "sensors.h"

#define TAG "NODE"

static node_prov_cfg_t s_cfg;
static node_sensor_table_t s_table;

static uint32_t s_boot_id;
static uint32_t s_seq;
static bool s_cold_boot = true;

static void jumper_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << NODE_PROV_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
}

static bool jumper_installed(void)
{
    return gpio_get_level(NODE_PROV_GPIO) == 0;
}

static bool jumper_released(void)
{
    for (int i = 0; i < 3; ++i) {
        if (jumper_installed()) {
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    return true;
}

static void mac_str(const uint8_t mac[6], char out[18])
{
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static void send_packet(bool setup_mode)
{
    node_sensor_slot_t slots[NODE_MAX_SENSORS];
    uint8_t count = sensors_read_all(slots);
    uint16_t battery_mv = battery_read_mv();

    node_payload_t payload;
    payload_build(&payload, s_boot_id, ++s_seq, slots, count,
                  battery_mv, setup_mode, s_cold_boot);

    bool ok = link_send(s_cfg.controller, &payload, sizeof(payload), 3, 150);

    ESP_LOGI(TAG, "tx seq=%lu slots=%u batt=%umV ch=%u flags=0x%02x ok=%d",
             (unsigned long)payload.seq, count, battery_mv, s_cfg.channel,
             payload.flags, ok);

    s_cold_boot = false;
}

static void run_normal(void)
{
    led_set_pattern(LED_SLOW);
    ESP_LOGI(TAG, "normal mode, interval %d ms", NODE_NORMAL_INTERVAL_MS);

    for (;;) {
        send_packet(false);
        vTaskDelay(pdMS_TO_TICKS(NODE_NORMAL_INTERVAL_MS));
    }
}

static void run_fast(void)
{
    led_set_pattern(LED_SLOW);
    ESP_LOGI(TAG, "fast mode, remove the jumper to return to normal");

    while (!jumper_released()) {
        int64_t started_us = esp_timer_get_time();
        send_packet(true);

        /*
         * The DS18B20 conversion already takes about 800 ms. When no sensor is
         * configured or none responds, keep the same cycle rate instead of
         * flooding the controller.
         */
        int64_t elapsed_ms = (esp_timer_get_time() - started_us) / 1000;
        if (elapsed_ms < 800) {
            vTaskDelay(pdMS_TO_TICKS(800 - elapsed_ms));
        }
    }

    ESP_LOGI(TAG, "jumper removed, leaving fast mode");
    run_normal();
}

static void run_setup(void)
{
    led_set_pattern(LED_FAST);
    ESP_LOGI(TAG, "setup: provisioning");
    prov_run_forever(&s_cfg);

    char controller[18];
    mac_str(s_cfg.controller, controller);
    ESP_LOGI(TAG, "setup: controller %s channel %u", controller, s_cfg.channel);

    led_set_pattern(LED_SOLID);
    ESP_LOGI(TAG, "setup: scanning the 1-Wire bus");
    sensors_reset_table(&s_table);
    uint8_t count = sensors_scan(&s_table);
    ESP_LOGI(TAG, "setup: %u sensor(s) found", count);

    esp_err_t err = sensors_table_save(&s_table);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot save sensor table: %s", esp_err_to_name(err));
    }
    sensors_apply_table(&s_table);

    if (count == 0) {
        led_set_pattern(LED_ERROR);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }

    send_packet(true);

    if (jumper_installed()) {
        run_fast();
    } else {
        run_normal();
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    jumper_init();
    led_init();

    if (battery_init() != ESP_OK) {
        ESP_LOGW(TAG, "battery measurement disabled");
    }
    ESP_ERROR_CHECK(sensors_init());
    ESP_ERROR_CHECK(link_init());

    s_boot_id = esp_random();
    if (s_boot_id == 0) {
        s_boot_id = 1;
    }

    bool jumper = jumper_installed();
    bool have_cfg = prov_load(&s_cfg) == ESP_OK;

    ESP_LOGI(TAG, "boot: boot_id=%08lx jumper=%d cfg=%d",
             (unsigned long)s_boot_id, jumper, have_cfg);

    if (jumper || !have_cfg) {
        run_setup();
        return;
    }

    err = sensors_table_load(&s_table);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "no saved sensor table: %s", esp_err_to_name(err));
        sensors_reset_table(&s_table);
    }

    sensors_apply_table(&s_table);
    ESP_ERROR_CHECK(link_set_channel(s_cfg.channel));
    ESP_ERROR_CHECK(link_add_peer(s_cfg.controller));

    char controller[18];
    mac_str(s_cfg.controller, controller);
    ESP_LOGI(TAG, "using controller %s channel %u, %u sensor(s)",
             controller, s_cfg.channel, s_table.count);

    run_normal();
}
