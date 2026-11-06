#include "payload.h"

#include <string.h>

#include "esp_system.h"
#include "esp_timer.h"

void payload_build(node_payload_t *payload,
                   uint32_t boot_id,
                   uint32_t seq,
                   const node_sensor_slot_t slots[NODE_MAX_SENSORS],
                   uint8_t slot_count,
                   uint16_t battery_mv,
                   bool setup_mode,
                   bool cold_boot)
{
    memset(payload, 0, sizeof(*payload));

    memcpy(payload->magic, NODE_PAYLOAD_MAGIC, 4);
    payload->version = NODE_PAYLOAD_VERSION;
    payload->slot_count = slot_count > NODE_MAX_SENSORS ? NODE_MAX_SENSORS : slot_count;
    payload->seq = seq;
    payload->boot_id = boot_id;
    payload->awake_ms = (uint32_t)(esp_timer_get_time() / 1000);
    payload->battery_mv = battery_mv;
    payload->chip_temp = NODE_TEMP_INVALID;
    payload->reset_reason = (uint8_t)esp_reset_reason();
    payload->wake_cause = 0;

    memcpy(payload->sensors, slots, sizeof(payload->sensors));

    uint8_t flags = 0;
    if (setup_mode) {
        flags |= NODE_FLAG_SETUP;
    }
    if (cold_boot) {
        flags |= NODE_FLAG_COLD_BOOT;
    }
    if (payload->slot_count == 0) {
        flags |= NODE_FLAG_FAULT;
    }
    for (uint8_t i = 0; i < payload->slot_count; ++i) {
        if (slots[i].status != NODE_SLOT_OK) {
            flags |= NODE_FLAG_FAULT;
        }
    }
    if (battery_mv != 0 && battery_mv < NODE_BATTERY_LOW_MV) {
        flags |= NODE_FLAG_LOW_BATT;
    }
    payload->flags = flags;
}
