#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    uint32_t magic;
    uint8_t controller[6];
    uint8_t channel;
    uint8_t token[4];
} node_prov_cfg_t;

esp_err_t prov_load(node_prov_cfg_t *cfg);
esp_err_t prov_save(const node_prov_cfg_t *cfg);
bool prov_valid(const node_prov_cfg_t *cfg);

/* Blocks until a controller is found and the provisioning record is saved. */
void prov_run_forever(node_prov_cfg_t *cfg);
