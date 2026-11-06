#include "led.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"

#include "board.h"

#define LED_TICK_MS 100

static volatile led_pattern_t s_pattern = LED_OFF;

static void led_write(int on)
{
    int level = NODE_LED_ACTIVE_HIGH ? on : !on;
    gpio_set_level(NODE_LED_GPIO, level);
}

static void led_task(void *arg)
{
    (void)arg;
    unsigned tick = 0;

    for (;;) {
        int on;

        switch (s_pattern) {
        case LED_OFF:
            on = 0;
            break;
        case LED_SOLID:
            on = 1;
            break;
        case LED_FAST:
            on = (tick & 1u) != 0;
            break;
        case LED_SLOW:
            on = (tick / 5u) & 1u;
            break;
        case LED_ERROR: {
            unsigned phase = tick % 10u;
            on = (phase < 2u) || (phase >= 3u && phase < 5u) ||
                 (phase >= 6u && phase < 8u);
            break;
        }
        default:
            on = 0;
            break;
        }

        led_write(on);
        ++tick;
        vTaskDelay(pdMS_TO_TICKS(LED_TICK_MS));
    }
}

void led_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << NODE_LED_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    led_write(0);
    xTaskCreate(led_task, "led", 2048, NULL, 4, NULL);
}

void led_set_pattern(led_pattern_t pattern)
{
    s_pattern = pattern;
}
