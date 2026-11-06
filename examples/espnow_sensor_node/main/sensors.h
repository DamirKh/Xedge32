#pragma once

#include <stdint.h>

#include "esp_err.h"

#include "payload.h"

#define NODE_SENSOR_TABLE_MAGIC 0x54424c31u

typedef struct {
    uint8_t count;
    uint8_t rom[NODE_MAX_SENSORS][8];
} node_sensor_table_t;

esp_err_t sensors_init(void);
void sensors_reset_table(node_sensor_table_t *table);

/* Searches the bus and fills the ROM table. Returns the number of sensors. */
uint8_t sensors_scan(node_sensor_table_t *table);

/* Creates persistent DS18B20 handles for the ROM table. */
esp_err_t sensors_apply_table(const node_sensor_table_t *table);

/* Converts all configured sensors. Returns slot_count (0..5). */
uint8_t sensors_read_all(node_sensor_slot_t slots[NODE_MAX_SENSORS]);

esp_err_t sensors_table_load(node_sensor_table_t *table);
esp_err_t sensors_table_save(const node_sensor_table_t *table);
