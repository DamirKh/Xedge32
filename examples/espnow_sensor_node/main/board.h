#pragma once

/* Classic ESP32 sensor node wiring. */

/* Dallas 1-Wire bus, external 4.7k pull-up to 3.3 V. */
#define NODE_ONEWIRE_GPIO          18

/* Setup jumper to GND. */
#define NODE_PROV_GPIO             4

/* Status LED, active high. */
#define NODE_LED_GPIO              2
#define NODE_LED_ACTIVE_HIGH       1

/* Battery divider on ADC1 channel 7 (GPIO35), 1:2. */
#define NODE_BATTERY_ADC_CHANNEL   7
#define NODE_BATTERY_DIVIDER       2

/* Normal measurement interval. */
#define NODE_NORMAL_INTERVAL_MS    300000
