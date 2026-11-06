#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t battery_init(void);

/* Returns battery voltage in millivolts, or 0 when unavailable. */
uint16_t battery_read_mv(void);
