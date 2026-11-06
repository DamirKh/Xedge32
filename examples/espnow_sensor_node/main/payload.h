#pragma once

#include <stdbool.h>
#include <stdint.h>

#define NODE_PAYLOAD_MAGIC      "XNW1"
#define NODE_PAYLOAD_VERSION    2u
#define NODE_PAYLOAD_SIZE       96u
#define NODE_MAX_SENSORS        5u

#define NODE_FLAG_SETUP         0x01u
#define NODE_FLAG_FAULT         0x02u
#define NODE_FLAG_LOW_BATT      0x04u
#define NODE_FLAG_COLD_BOOT     0x08u

#define NODE_SLOT_ABSENT        0u
#define NODE_SLOT_OK            1u
#define NODE_SLOT_MISSING       2u
#define NODE_SLOT_ERROR         3u

#define NODE_TEMP_INVALID       ((int16_t)0x8000)
#define NODE_BATTERY_LOW_MV     3200u

typedef struct __attribute__((packed)) {
    uint8_t rom[8];
    int16_t temp;
    uint8_t status;
    uint8_t reserved;
} node_sensor_slot_t;

typedef struct __attribute__((packed)) {
    char magic[4];
    uint8_t version;
    uint8_t flags;
    uint8_t slot_count;
    uint8_t reserved0;
    uint32_t seq;
    uint32_t boot_id;
    uint32_t awake_ms;
    uint16_t battery_mv;
    int16_t chip_temp;
    uint8_t reset_reason;
    uint8_t wake_cause;
    uint16_t reserved1;
    node_sensor_slot_t sensors[NODE_MAX_SENSORS];
    uint8_t ext[8];
} node_payload_t;

_Static_assert(sizeof(node_sensor_slot_t) == 12, "sensor slot must be 12 bytes");
_Static_assert(sizeof(node_payload_t) == NODE_PAYLOAD_SIZE, "payload must be 96 bytes");

void payload_build(node_payload_t *payload,
                   uint32_t boot_id,
                   uint32_t seq,
                   const node_sensor_slot_t slots[NODE_MAX_SENSORS],
                   uint8_t slot_count,
                   uint16_t battery_mv,
                   bool setup_mode,
                   bool cold_boot);
