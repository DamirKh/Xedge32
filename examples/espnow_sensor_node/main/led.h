#pragma once

typedef enum {
    LED_OFF = 0,
    LED_FAST,
    LED_SOLID,
    LED_SLOW,
    LED_ERROR,
} led_pattern_t;

void led_init(void);
void led_set_pattern(led_pattern_t pattern);
