#include "sensors.h"

#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "onewire_bus.h"
#include "ds18b20.h"

#include "board.h"

#define TAG "SENS"

#define SENSOR_NVS_NAMESPACE "node"
#define SENSOR_NVS_KEY       "table"

typedef struct {
    uint32_t magic;
    uint8_t count;
    uint8_t rom[NODE_MAX_SENSORS][8];
} sensor_table_blob_t;

static onewire_bus_handle_t s_bus;
static ds18b20_device_handle_t s_dev[NODE_MAX_SENSORS];
static uint8_t s_rom[NODE_MAX_SENSORS][8];
static uint8_t s_count;

static void rom_to_str(const uint8_t rom[8], char out[17])
{
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 8; ++i) {
        out[i * 2] = hex[rom[i] >> 4];
        out[i * 2 + 1] = hex[rom[i] & 0x0f];
    }
    out[16] = '\0';
}

static int16_t temp_to_raw(float temp)
{
    float scaled = temp * 16.0f;
    int32_t value = (int32_t)(scaled + (scaled >= 0.0f ? 0.5f : -0.5f));
    if (value > 32767) {
        value = 32767;
    } else if (value < -32768) {
        value = -32768;
    }
    return (int16_t)value;
}

esp_err_t sensors_init(void)
{
    onewire_bus_config_t bus_config = {
        .bus_gpio_num = NODE_ONEWIRE_GPIO,
        .flags = {
            .en_pull_up = false,
        },
    };
    onewire_bus_rmt_config_t rmt_config = {
        .max_rx_bytes = 10,
    };

    esp_err_t err = onewire_new_bus_rmt(&bus_config, &rmt_config, &s_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "1-Wire init failed: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "1-Wire on GPIO%d", NODE_ONEWIRE_GPIO);
    return ESP_OK;
}

void sensors_reset_table(node_sensor_table_t *table)
{
    memset(table, 0, sizeof(*table));
}

uint8_t sensors_scan(node_sensor_table_t *table)
{
    sensors_reset_table(table);

    onewire_device_iter_handle_t iter = NULL;
    esp_err_t err = onewire_new_device_iter(s_bus, &iter);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "device iterator failed: %s", esp_err_to_name(err));
        return 0;
    }

    for (int guard = 0; guard < 64; ++guard) {
        onewire_device_t device;
        err = onewire_device_iter_get_next(iter, &device);
        if (err == ESP_ERR_NOT_FOUND) {
            break;
        }
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "search error: %s", esp_err_to_name(err));
            continue;
        }

        ds18b20_device_handle_t handle = NULL;
        ds18b20_config_t config = {};
        if (ds18b20_new_device_from_enumeration(&device, &config, &handle) != ESP_OK) {
            ESP_LOGW(TAG, "device %016llx is not a DS18B20",
                     (unsigned long long)device.address);
            continue;
        }

        if (table->count >= NODE_MAX_SENSORS) {
            ESP_LOGW(TAG, "more than %u sensors, ignoring the rest", NODE_MAX_SENSORS);
            ds18b20_del_device(handle);
            break;
        }

        onewire_device_address_t address = 0;
        ds18b20_get_device_address(handle, &address);
        memcpy(table->rom[table->count], &address, 8);
        ++table->count;

        char rom[17];
        rom_to_str(table->rom[table->count - 1], rom);
        ESP_LOGI(TAG, "found %s", rom);

        ds18b20_del_device(handle);
    }

    onewire_del_device_iter(iter);
    return table->count;
}

esp_err_t sensors_apply_table(const node_sensor_table_t *table)
{
    for (uint8_t i = 0; i < s_count; ++i) {
        if (s_dev[i] != NULL) {
            ds18b20_del_device(s_dev[i]);
            s_dev[i] = NULL;
        }
    }
    s_count = 0;

    uint8_t count = table->count > NODE_MAX_SENSORS ? NODE_MAX_SENSORS : table->count;
    for (uint8_t i = 0; i < count; ++i) {
        memcpy(s_rom[i], table->rom[i], 8);

        onewire_device_address_t address;
        memcpy(&address, s_rom[i], 8);
        onewire_device_t device = {
            .bus = s_bus,
            .address = address,
        };
        ds18b20_config_t config = {};

        esp_err_t err = ds18b20_new_device_from_enumeration(&device, &config, &s_dev[i]);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "cannot bind sensor %u: %s", i, esp_err_to_name(err));
            s_dev[i] = NULL;
            continue;
        }
        ds18b20_set_resolution(s_dev[i], DS18B20_RESOLUTION_12B);
    }

    s_count = count;
    return ESP_OK;
}

uint8_t sensors_read_all(node_sensor_slot_t slots[NODE_MAX_SENSORS])
{
    memset(slots, 0, sizeof(node_sensor_slot_t) * NODE_MAX_SENSORS);

    for (uint8_t i = 0; i < s_count; ++i) {
        memcpy(slots[i].rom, s_rom[i], 8);
        slots[i].status = NODE_SLOT_MISSING;
    }

    if (s_count == 0) {
        return 0;
    }

    esp_err_t err = ds18b20_trigger_temperature_conversion_for_all(s_bus);
    if (err == ESP_ERR_NOT_FOUND) {
        /* No configured sensor answered; slots already say MISSING. */
        return s_count;
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "conversion failed: %s", esp_err_to_name(err));
        for (uint8_t i = 0; i < s_count; ++i) {
            slots[i].status = NODE_SLOT_ERROR;
        }
        return s_count;
    }

    for (uint8_t i = 0; i < s_count; ++i) {
        if (s_dev[i] == NULL) {
            slots[i].status = NODE_SLOT_MISSING;
            continue;
        }

        float temperature = 0.0f;
        err = ds18b20_get_temperature(s_dev[i], &temperature);
        if (err == ESP_OK) {
            slots[i].temp = temp_to_raw(temperature);
            slots[i].status = NODE_SLOT_OK;
        } else if (err == ESP_ERR_INVALID_CRC) {
            slots[i].status = NODE_SLOT_ERROR;
        } else {
            slots[i].status = NODE_SLOT_MISSING;
        }
    }

    return s_count;
}

esp_err_t sensors_table_save(const node_sensor_table_t *table)
{
    sensor_table_blob_t blob;
    memset(&blob, 0, sizeof(blob));
    blob.magic = NODE_SENSOR_TABLE_MAGIC;
    blob.count = table->count > NODE_MAX_SENSORS ? NODE_MAX_SENSORS : table->count;
    memcpy(blob.rom, table->rom, sizeof(blob.rom));

    nvs_handle_t nvs;
    esp_err_t err = nvs_open(SENSOR_NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(nvs, SENSOR_NVS_KEY, &blob, sizeof(blob));
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    return err;
}

esp_err_t sensors_table_load(node_sensor_table_t *table)
{
    sensor_table_blob_t blob;
    size_t len = sizeof(blob);

    nvs_handle_t nvs;
    esp_err_t err = nvs_open(SENSOR_NVS_NAMESPACE, NVS_READONLY, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_blob(nvs, SENSOR_NVS_KEY, &blob, &len);
    nvs_close(nvs);

    if (err != ESP_OK || len != sizeof(blob) || blob.magic != NODE_SENSOR_TABLE_MAGIC ||
        blob.count > NODE_MAX_SENSORS) {
        return ESP_ERR_NOT_FOUND;
    }

    memset(table, 0, sizeof(*table));
    table->count = blob.count;
    memcpy(table->rom, blob.rom, sizeof(table->rom));
    return ESP_OK;
}
