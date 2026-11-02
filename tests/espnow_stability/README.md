# ESP-NOW stability test

Two ESP32-S3 boards are used:

- one runs the Xedge32 firmware with the `esp32.espnow` receive API;
- the other runs MicroPython and sends ESP-NOW packets.

The host script loads the receive-side Lua code, starts the MicroPython sender
through its raw REPL, polls both sides, and writes a JSON/CSV report. It does
not need `mpremote`.

## Requirements

- Xedge32 serial port, normally `/dev/ttyACM0`.
- MicroPython ESP32-S3 board on a second serial port. The normal ESP32-S3
  MicroPython firmware puts the REPL on UART0, not on native USB, so connect
  the board's UART USB port. It may appear as `/dev/ttyACM1` or
  `/dev/ttyUSB0`.
- The MicroPython build must include the `espnow` module. The normal
  ESP32-S3 MicroPython firmware does.
- Both boards must use the same Wi-Fi channel.
- While the receiver is open, Xedge32 disables Wi-Fi power saving; the previous
  mode is restored on close.
- The Xedge32 receiver should stay on one channel for the whole test. Fix the
  access-point channel, or use an Xedge32 access-point configuration with a
  fixed channel. A receiver channel change is reported as a test failure by
  default.
- Use a powered USB hub or a stable 5 V supply. If the ESP32-S3 browns out
  while Wi-Fi starts, the board resets and a long run is invalid.

Run the host script through the existing container wrapper, because it already
contains `pyserial`:

```sh
./tools/idf.sh python tests/espnow_stability/host.py --help
```

## Self-test without a sender

This checks the Lua receiver, sequence accounting, duplicate detection, gap
detection, and report parsing on the Xedge32 board:

```sh
./tools/idf.sh python tests/espnow_stability/host.py \
    --receiver-port /dev/ttyACM0 \
    --self-test
```

## Short live test

Use this first after connecting the MicroPython board:

```sh
./tools/idf.sh python tests/espnow_stability/host.py \
    --receiver-port /dev/ttyACM0 \
    --sender-port /dev/ttyACM1 \
    --duration 30 \
    --interval-ms 20 \
    --payload-len 32 \
    --max-loss-pct 1 \
    --json build/espnow-short.json \
    --csv build/espnow-short.csv
```

`--interval-ms 20` is about 50 packets per second.

## Long test

For example, about one hour at 10 packets per second:

```sh
./tools/idf.sh python tests/espnow_stability/host.py \
    --receiver-port /dev/ttyACM0 \
    --sender-port /dev/ttyACM1 \
    --duration 3600 \
    --interval-ms 100 \
    --payload-len 32 \
    --max-loss-pct 1 \
    --json build/espnow-long.json \
    --csv build/espnow-long.csv
```

Use `--count N` instead of `--duration` to stop after a fixed number of sends.
The test stops on a receiver channel change unless
`--allow-channel-change` is supplied.

## Packet format

Every packet is:

```text
offset  size  field
0       4     ASCII "XNW1"
4       4     sequence number, little endian, starts at 1
8       4     sender uptime in milliseconds, little endian
12      ...   zero padding to --payload-len
```

The sender addresses the Xedge32 receiver MAC by default, so ESP-NOW can use
its link-layer acknowledgement and retry behavior. `--broadcast` sends to
`ff:ff:ff:ff:ff:ff` instead. The receiver does not need peer setup either way.
In testing on a connected station, unicast was much more reliable than
broadcast.

## Result

The final report contains:

- sent, received, unique, lost, loss percentage;
- duplicates, out-of-order packets, maximum sequence gap;
- receiver queue drops, bad magic packets, wrong-length packets;
- RSSI minimum, average, maximum;
- receiver channel and number of channel changes.

`PASS` means the configured loss and duplicate/drop thresholds were met and no
bad packets or channel changes were seen. The JSON report contains the full
configuration, live history, and errors. The CSV is optional and contains the
periodic samples.
