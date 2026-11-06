#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef void (*link_recv_fn)(const uint8_t *src, const uint8_t *data, int len);

esp_err_t link_init(void);
const uint8_t *link_mac(void);
esp_err_t link_set_channel(uint8_t channel);
esp_err_t link_add_peer(const uint8_t mac[6]);
void link_set_recv_handler(link_recv_fn fn);
bool link_send(const uint8_t mac[6], const void *data, size_t len,
               int retries, int timeout_ms);
