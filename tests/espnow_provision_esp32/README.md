# ESP-NOW provisioning test: classic ESP32 transmitter

This test uses:

- an ESP32-S3 running Xedge32 as the controller;
- a classic ESP32 running a minimal ESP-IDF transmitter.

The controller sends a `PROV` broadcast from its current STA channel. The
transmitter cycles channels 1..13, receives the packet, stores the controller
MAC and channel in NVS, sends `POK`, then sends unicast `XNW1` test readings.

## Build and flash the transmitter

```sh
./tools/idf.sh bash -c 'cd /project/tests/espnow_provision_esp32 && \
    IDF_TARGET=esp32 idf.py -B /project/build/espnow_provision_esp32/build \
    -D SDKCONFIG=/project/build/espnow_provision_esp32/build/sdkconfig build'

./tools/idf.sh esptool --chip esp32 -p /dev/ttyUSB0 -b 115200 erase-flash
./tools/idf.sh esptool --chip esp32 -p /dev/ttyUSB0 -b 115200 \
    --before default-reset --after hard-reset write-flash \
    --flash-mode dio --flash-freq 40m --flash-size 4MB \
    0x1000 build/espnow_provision_esp32/build/bootloader/bootloader.bin \
    0x8000 build/espnow_provision_esp32/build/partition_table/partition-table.bin \
    0x10000 build/espnow_provision_esp32/build/espnow_provision_esp32.bin
```

The transmitter erases its configuration from the test script by default.
For a manual reprovision, erase the NVS partition (default layout):

```sh
./tools/idf.sh esptool --chip esp32 -p /dev/ttyUSB0 -b 115200 \
    erase-region 0x9000 0x6000
```

## Provisioning trigger

- No valid NVS configuration: the transmitter enters provisioning
  automatically.
- GPIO4 connected to GND at boot: force provisioning.

Do not use GPIO0/BOOT for a boot-time strap; it selects the ESP32 download
mode. GPIO4 is safe for this board.

## Controller requirements

- Xedge32 is connected as a STA to an access point with a stable 2.4 GHz
  channel.
- The controller must expose `rx:broadcast()` and `rx:send()`.
- Use a controlled access point. A changing AP channel makes provisioning and
  subsequent ESP-NOW traffic fail because the radio changes channel.

## Run the test

The transmitter is on `/dev/ttyUSB0`; the Xedge32 controller is on
`/dev/ttyACM0`.

```sh
./tools/idf.sh python tests/espnow_provision_test_esp32.py
```

The script:

1. erases the transmitter NVS;
2. opens the Xedge32 receiver;
3. resets the transmitter;
4. sends `PROV` broadcasts for up to 45 seconds;
5. waits for `POK` and at least three `XNW1` data packets.

Use `--keep-config` to keep an existing transmitter configuration.

## Packet formats

```text
PROV from controller:
  0..3   "PROV"
  4      version
  5..10  controller MAC
  11..14 token
  15     current channel

POK from transmitter:
  0..2   "POK"
  3      version
  4..9   transmitter MAC
  10..13 token

XNW1 test reading:
  0..3   "XNW1"
  4..7   sequence, little endian
  8..11  uptime in milliseconds, little endian
  12..31 zero padding
```
