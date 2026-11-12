# ESP-NOW sensor node (classic ESP32)

Battery/sensor node firmware for the Xedge32 warm-floor controller. The node
runs on a classic ESP32, reads up to five DS18B20 sensors, and sends `XNW1` v2
packets to the controller over ESP-NOW. It does not connect to an access point
and does not accept commands.

The controller side is Xedge32; see
[`doc/AI/ESPNow-Agent-Guide.md`](../../doc/AI/ESPNow-Agent-Guide.md). The packet
layout is defined in
[`doc/AI/ESPNow-Packet-Format.md`](../../doc/AI/ESPNow-Packet-Format.md).

## Status

Builds for `esp32` with ESP-IDF 6.1. On-device validation is pending; the
module has not been flashed from this tree yet.

## Wiring

| Signal | GPIO | Notes |
| --- | ---: | --- |
| DS18B20 data | 18 | External 4.7k pull-up to 3.3 V, 3-wire sensors |
| Setup jumper | 4 | Jumper to GND; checked at boot only |
| Status LED | 2 | Active high, built-in LED on most dev boards |
| Battery sense | 35 | ADC1 channel 7, 1:2 divider, input-only pin |

Use three-wire DS18B20 sensors (VDD, GND, DATA). Parasitic power is not
supported by this configuration.

The battery divider should be high value, for example 1M/1M with a 100 nF
capacitor at the ADC pin. A 100k/100k divider already draws about 21 uA, which
matters for a battery-powered board.

## Behavior

On boot the node reads GPIO4:

- jumper installed, or no valid provisioning record: run setup;
- otherwise: run the normal cycle.

Setup is unconditional: it re-provisions the radio, clears the old sensor
table, scans the bus, saves the new table, and sends one packet.

After setup:

- jumper still installed: **fast mode**. Read all sensors, send immediately,
  repeat without an extra delay. The DS18B20 conversion at 12 bits takes about
  800 ms, so the packet rate is roughly one per second. When no sensor is
  configured or none responds, the node keeps the same ~800 ms cadence instead
  of flooding the controller.
- jumper removed: **normal mode**. Read and send every 300 s.

Fast mode ends when the jumper is removed; the node then switches to the normal
cycle. The jumper is only checked at boot for entering setup, so inserting it
while the node runs does nothing until the next reset.

If the controller is not found during setup, the node keeps scanning channels
1..13 and retrying forever. It does not fall back to the previous configuration.

If the scan finds no sensors, the node saves an empty table, signals an error
with the LED, and keeps sending packets with zero slots and the `FAULT` flag.

## Channel recovery

The node normally sends on the channel stored in `prov/cfg`.

1. It makes three attempts on that channel.
2. If none is acknowledged, it scans channels 1..13 (except the current one),
   waits 20 ms for the radio to settle, and sends once per channel.
3. The first channel that returns a link-layer ACK is stored back into
   `prov/cfg`, and the node continues on it.
4. If no channel is acknowledged, the node restores the previous channel and
   reports the failure; the next cycle tries again.

The packet and its `seq` do not change while scanning, so the controller can
deduplicate a packet that arrives twice. In fast mode a failed cycle takes about
2.5 s instead of 0.8 s. The controller does not move: it stays on its AP
channel, and only the node follows the channel change.

## LED

| Pattern | Meaning |
| --- | --- |
| Fast blink (100 ms) | Provisioning, scanning channels for `PROV` |
| Solid on | Scanning the 1-Wire bus |
| Slow blink (1 s) | Measuring and transmitting (fast or normal mode) |
| Three short pulses | No sensors found |
| Off | Not used in this version |

## Build

From the repository root:

```sh
./tools/idf.sh bash -c 'cd /project/examples/espnow_sensor_node && \
  IDF_TARGET=esp32 idf.py -B /project/build/espnow_sensor_node/build \
  -D SDKCONFIG=/project/build/espnow_sensor_node/build/sdkconfig build'
```

The first build downloads `espressif/ds18b20` and `espressif/onewire_bus` into
`examples/espnow_sensor_node/managed_components`, which is ignored by git.

The app is about 740 KB and uses a 2 MB factory partition, leaving room for
future growth. There is no OTA support; the node has no network connection.

## Testing

`tests/espnow_sensor_node_setup_test.py` drives the node over UART and, when
available, the Xedge32 controller over LuaShell. It covers setup entry,
provisioning retries, malformed `PROV`, bus scan, the first packet,
fast/normal transitions, and reboot persistence. LED patterns, channel
recovery, and sensor edge cases are manual cases. See the harness docstring for
the full list and hardware requirements.

**STATUS: not executed.** The harness was written while both boards were
disconnected. Only the Python syntax, `--list`, and the missing-port path have
been checked. Run it after connecting the hardware:

```sh
./tools/idf.sh python tests/espnow_sensor_node_setup_test.py --list
./tools/idf.sh python tests/espnow_sensor_node_setup_test.py --case all \
  --sensors 0
```

For 1 or 5 sensors, pass `--sensors N` and connect the sensors first. Jumper
cases prompt for the physical jumper unless `--yes` is used; the boot log still
verifies the actual state.

## Flash

The module used during development is a classic ESP32 on `/dev/ttyUSB0`:

```sh
./tools/idf.sh bash -c 'cd /project/examples/espnow_sensor_node && \
  IDF_TARGET=esp32 idf.py -B /project/build/espnow_sensor_node/build \
  -D SDKCONFIG=/project/build/espnow_sensor_node/build/sdkconfig \
  -p /dev/ttyUSB0 -b 115200 flash'
```

A full flash is appropriate for the first installation. Do not use
`app-flash` if the partition table or NVS layout changed.

## Provisioning

The node uses the same `PROV`/`POK` exchange as the test transmitter in
`tests/espnow_provision_esp32`:

1. The controller broadcasts `PROV` with its MAC, token and channel.
2. The node scans channels 1..13, stores the controller MAC, token and channel
   in NVS, and replies with a unicast `POK`.
3. The node scans the 1-Wire bus and saves the ROM table.

To force setup, install the jumper between GPIO4 and GND and reset the node.
There is no serial command interface.

## Packet

The node sends a fixed 96-byte `XNW1` v2 payload:

- five sensor slots, each with an 8-byte ROM, temperature in 1/16 °C, and a
  status byte;
- battery voltage in millivolts;
- chip temperature, `0x8000` on the classic ESP32;
- sequence number and boot ID for deduplication;
- reserved extension bytes for future humidity/pressure data.

See [`doc/AI/ESPNow-Packet-Format.md`](../../doc/AI/ESPNow-Packet-Format.md) for
offsets, flags, status codes and decoder examples.

## Decode a packet

```sh
python3 examples/espnow_sensor_node/tools/decode.py --hex \
  584e5731020002000500000078563412102700002c0d00800100000028ff641e5b16040d78010100283c2a11223344555eff01000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
```

Use `--json` for machine-readable output, or pass a binary file instead of
`--hex`.
