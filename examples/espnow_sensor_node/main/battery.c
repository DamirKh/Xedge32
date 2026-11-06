#include "battery.h"

#include <stdbool.h>

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_log.h"

#include "board.h"

#define TAG "BATT"

#define BATTERY_SAMPLES 16

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static bool s_cali_ready;

esp_err_t battery_init(void)
{
    adc_oneshot_unit_init_cfg_t unit_config = {
        .unit_id = ADC_UNIT_1,
    };
    esp_err_t err = adc_oneshot_new_unit(&unit_config, &s_adc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC init failed: %s", esp_err_to_name(err));
        return err;
    }

    adc_oneshot_chan_cfg_t channel_config = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    err = adc_oneshot_config_channel(s_adc, (adc_channel_t)NODE_BATTERY_ADC_CHANNEL,
                                     &channel_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC channel failed: %s", esp_err_to_name(err));
        return err;
    }

    adc_cali_line_fitting_config_t cali_config = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
#if CONFIG_IDF_TARGET_ESP32
        .default_vref = 1100,
#endif
    };
    err = adc_cali_create_scheme_line_fitting(&cali_config, &s_cali);
    if (err == ESP_OK) {
        s_cali_ready = true;
    } else {
        ESP_LOGW(TAG, "ADC calibration unavailable: %s", esp_err_to_name(err));
        s_cali = NULL;
        s_cali_ready = false;
    }

    ESP_LOGI(TAG, "battery ADC on GPIO35, divider 1:%d", NODE_BATTERY_DIVIDER);
    return ESP_OK;
}

uint16_t battery_read_mv(void)
{
    if (s_adc == NULL) {
        return 0;
    }

    int32_t total = 0;
    int samples = 0;
    for (int i = 0; i < BATTERY_SAMPLES; ++i) {
        int raw = 0;
        if (adc_oneshot_read(s_adc, (adc_channel_t)NODE_BATTERY_ADC_CHANNEL, &raw) == ESP_OK) {
            total += raw;
            ++samples;
        }
    }
    if (samples == 0) {
        return 0;
    }

    int raw = (int)(total / samples);
    int voltage_mv = 0;
    if (s_cali_ready) {
        if (adc_cali_raw_to_voltage(s_cali, raw, &voltage_mv) != ESP_OK) {
            return 0;
        }
    } else {
        voltage_mv = (raw * 3300) / 4095;
    }

    int32_t battery_mv = (int32_t)voltage_mv * NODE_BATTERY_DIVIDER;
    if (battery_mv < 0) {
        battery_mv = 0;
    } else if (battery_mv > 65535) {
        battery_mv = 65535;
    }
    return (uint16_t)battery_mv;
}
