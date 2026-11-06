# ESP-NOW Agent Guide

This guide is for agents working on Xedge32 ESP-NOW integration. It covers the
Lua API, a typical receive path, the provisioning flow that was tested with a
classic ESP32 transmitter, and the production sensor node firmware.

Authoritative sources:

- `main/BaESP32.c`: ESP-NOW section and `esp32Lib` registration.
- `doc/source/ESPNow.rst`: user-facing API reference.
- `doc/AI/ESPNow-Packet-Format.md`: `XNW1` v2 sensor node packet layout.
- `examples/espnow_sensor_node/`: classic ESP32 sensor node firmware.
- `tests/device/test_espnow.py`: API and radio smoke tests.
- `tests/espnow_provision_esp32/`: classic ESP32 provisioning transmitter.
- `tests/espnow_provision_test_esp32.py`: end-to-end provisioning test.

Do not document or implement behavior that is not present in the code. If a
change adds peer management, encryption, application ACKs, or a new ESP-NOW
interface, update `doc/source/ESPNow.rst` and the tests in the same change.

## Platform and Scope

- ESP-NOW is available on ESP32 and ESP32-S3 builds with native Wi-Fi.
- It is **not registered** on ESP32-P4 ESP-Hosted builds. The implementation is
  guarded by `CONFIG_XEDGE_WIFI_ENABLED && CONFIG_SOC_WIFI_SUPPORTED`.
- The receiver uses the channel of the active Wi-Fi interface. In STA mode this
  follows the connected AP. In AP mode it uses the configured AP channel.
- A sender must use the same channel as the receiver. A changing AP channel can
  break both receive and send without any Lua error.
- While a receiver is open, Xedge32 disables Wi-Fi power saving
  (`WIFI_PS_NONE`) and restores the previous mode on close.

## Sensor Node Firmware

`examples/espnow_sensor_node` is the production node, separate from the
provisioning test in `tests/espnow_provision_esp32`.

- Classic ESP32, ESP-IDF/C, no access-point association.
- DS18B20 on GPIO18 via the RMT-backed `espressif/onewire_bus` and
  `espressif/ds18b20` components.
- Setup jumper on GPIO4, status LED on GPIO2, battery divider on GPIO35.
- Reads up to five sensors and sends the 96-byte `XNW1` v2 packet defined in
  `doc/AI/ESPNow-Packet-Format.md`.
- Send-only: it does not process inbound ESP-NOW packets outside provisioning.

Boot and mode rules:

1. Jumper installed or no valid provisioning record: setup.
2. Setup always re-provisions and rescans the bus. The old sensor table is
   replaced.
3. Jumper still installed after setup: fast mode. Read and send continuously
   with no extra delay; the DS18B20 conversion dominates the cycle at about
   800 ms.
4. Jumper removed: normal mode, one measurement and packet every 300 s.
5. No sensors found: save an empty table, signal an error on the LED, and keep
   sending packets with zero slots and the `FAULT` flag.
6. No controller found: keep scanning channels 1..13 forever; do not fall back
   to the previous configuration.

Build without flashing:

```sh
./tools/idf.sh bash -c 'cd /project/examples/espnow_sensor_node && \
  IDF_TARGET=esp32 idf.py -B /project/build/espnow_sensor_node/build \
  -D SDKCONFIG=/project/build/espnow_sensor_node/build/sdkconfig build'
```

The sensor node has not been validated on hardware from this tree yet. When
changing it, run the build above and the packet decoder in
`examples/espnow_sensor_node/tools/decode.py`. Update the packet format document
in the same change if the layout changes.

## Lua API

### Create a receiver

```lua
local rx, err = esp32.espnow{
   callback = function(data, src, info)
      -- data: binary string, 1..250 bytes
      -- src:  six-byte sender MAC
      -- info: {dest, rssi, channel, len}
   end
}
```

Only one receiver can exist at a time. A second `esp32.espnow()` returns `nil`
and an error until the first receiver is closed. Keep a Lua reference to `rx`
while it is in use; queued callbacks do not keep the object alive.

The callback runs in an `LThreadMgr` worker. Keep it short. If it raises a Lua
error, Xedge32 closes the receiver.

### Receiver methods

| Method | Behavior |
| --- | --- |
| `rx:close()` | Stops receive handling. Returns `true`, or `nil, err`. Closing an already closed receiver succeeds. Pending callbacks are discarded. |
| `rx:info()` | Returns `{channel=..., version=..., drops=..., ps=...}`. `ps == 0` means power saving is disabled while the receiver is open. |
| `rx:broadcast(data)` | Sends 1..250 bytes to `ff:ff:ff:ff:ff:ff`. Returns `true` when queued, or `nil, err`. No link-layer ACK. |
| `rx:send(mac, data)` | Sends 1..250 bytes to a six-byte MAC. Returns `true` when queued, or `nil, err`. The peer is registered automatically. |

Important send semantics:

- `true` means the packet was queued, not that the application processed it.
- Broadcast has no link-layer acknowledgement.
- Unicast can use the ESP-NOW link-layer ACK internally, but the Lua helper does
  not expose the per-packet send result.
- There is no application-level ACK API.
- No encryption, PMK/LMK, or explicit peer-management API is exposed.
- The receive queue holds eight packets. When full, the newest packet is
  dropped and counted in `rx:info().drops`.

## Typical Receive Algorithm

Use this pattern for sensor data received by an Xedge32 controller.

1. Put Wi-Fi in the required mode before creating the receiver.
   - STA: `esp32.netconnect("wifi", cfg)` or an already connected network.
   - AP: the board's configured AP mode/channel.
2. Open `esp32.espnow` once.
3. In the callback:
   - validate a magic/version header and length;
   - use `src` as the device identity;
   - deduplicate by `(boot_id, sequence)`;
   - update a small table with the latest value, RSSI, and timestamp;
   - return immediately.
4. In a timer or application loop:
   - publish/control using the latest values;
   - treat data older than a configured age as stale;
   - expose `rx:info().drops` for diagnostics.
5. On shutdown or reconfiguration, call `rx:close()`.

Minimal Lua skeleton:

```lua
local devices = {}

local function hex(mac)
   return (mac:gsub(".", function(c)
      return string.format("%02x", c:byte())
   end))
end

local rx = assert(esp32.espnow{
   callback = function(data, src, info)
      if #data < 12 or data:sub(1, 4) ~= "XNW1" then
         return
      end
      local id = hex(src)
      local seq = data:byte(5) + data:byte(6) * 256 +
                  data:byte(7) * 65536 + data:byte(8) * 16777216
      devices[id] = {
         seq = seq,
         rssi = info.rssi,
         channel = info.channel,
         seen = os.time(),
         data = data
      }
   end
})

trace("ESP-NOW channel", rx:info().channel)

-- Keep rx referenced while the application is running.
-- Application code reads devices[id] and checks its age, for example:
-- if os.time() - devices[id].seen > 180 then ... stale ... end
```

Production rules:

- Prefer a fixed 2.4 GHz AP channel. Auto-channel behavior caused real
  provisioning and receive failures during testing.
- Prefer unicast for application data. Broadcast is useful for discovery and
  provisioning, but it is best-effort.
- Do not block or perform network I/O in the ESP-NOW callback.
- Do not call `rx:close()` from arbitrary application code while callbacks are
  still needed; treat it as a lifecycle operation.
- A receiver channel follows the AP. If the AP changes channel, the sender must
  follow or traffic stops until the channel matches again.

## Provisioning Flow Tested Here

The tested flow uses a controller running Xedge32 and a classic ESP32
transmitter. The transmitter does not connect to the AP; it only sets the
ESP-NOW channel.

### Roles

- Controller: ESP32-S3, Xedge32, STA on a controlled 2.4 GHz AP.
- Transmitter: classic ESP32, ESP-IDF/C, no AP association.
- The transmitter is provisioned with the controller's ESP-NOW peer address,
  channel, and a provisioning token.

### Boot behavior on the transmitter

1. Initialize NVS.
2. Load the saved provisioning record.
3. Enter provisioning if:
   - no valid record exists, or
   - GPIO4 is pulled low at boot.
4. Otherwise start normal data transmission.

GPIO4 was used for the tested trigger. Do not use GPIO0/BOOT as a boot-time
strap: holding it low at reset enters the ESP32 download mode instead of the
application.

### Provisioning sequence

1. Controller opens its ESP-NOW receiver.
2. Controller repeatedly sends a broadcast `PROV` packet on its current channel.
   The test sends every 100 ms for up to 45 seconds.
3. Transmitter cycles channels 1..13. It changes the Wi-Fi channel, listens for
   a short dwell (350 ms in the tested firmware), and repeats.
4. On a valid `PROV`, the transmitter stores:
   - controller MAC;
   - controller channel;
   - token.
5. Transmitter switches to the controller channel and sends a unicast `POK`
   packet to the controller.
6. Controller's receive callback sees `POK`.
7. Transmitter sends unicast `XNW1` test readings. The firmware waits for the
   ESP-NOW send callback and logs link-layer success/failure.
8. The host test passes when the controller sees `POK` and at least three
   `XNW1` packets, and the transmitter reports `ok=1` for those packets.

### Packet formats

```text
PROV, controller -> broadcast:
  offset  size  field
  0       4     "PROV"
  4       1     version
  5       6     controller MAC
  11      4     token
  15      1     controller channel

POK, transmitter -> controller:
  offset  size  field
  0       3     "POK"
  3       1     version
  4       6     transmitter MAC
  10      4     token

XNW1 test reading, transmitter -> controller (test-only v1):
  offset  size  field
  0       4     "XNW1"
  4       4     sequence, little endian
  8       4     uptime in milliseconds, little endian
  12      ...   zero padding
```

The 32-byte packet above is produced only by the provisioning test. The
production sensor node sends the 96-byte `XNW1` v2 packet with a version byte,
sensor slots, battery voltage and status flags. See
`doc/AI/ESPNow-Packet-Format.md`. Distinguish the two formats by length: v2 is
always 96 bytes and `data[4] == 2`.

The token is a matching aid, not encryption or authentication. ESP-NOW payloads
are unencrypted in the current API.

### Tested result

The end-to-end test produced:

```text
node: provisioned: controller <controller-mac> channel 10
node: POK ok=1
node: DATA seq=1 ok=1 channel=10
node: DATA seq=2 ok=1 channel=10
node: DATA seq=3 ok=1 channel=10
controller: PKT 32 XNW1
...
RESULT: PASS
```

`tests/device/test_espnow.py` also passed its broadcast/unicast send checks
(8 passed, 0 failed, 1 skipped when no external sender was supplied).

### Reproducing the test

Build and flash the classic ESP32 transmitter:

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

Run the provisioning test:

```sh
./tools/idf.sh python tests/espnow_provision_test_esp32.py
```

The test erases the transmitter's NVS by default so that provisioning is
repeatable. Use `--keep-config` to preserve it.

To force provisioning manually, erase the default NVS partition:

```sh
./tools/idf.sh esptool --chip esp32 -p /dev/ttyUSB0 -b 115200 \
    erase-region 0x9000 0x6000
```

Then reset the transmitter or connect GPIO4 to GND before boot.

## ESP32-S3 Controller Build and Test

Build and flash Xedge32:

```sh
IDF_TARGET=esp32s3 ./tools/build.sh esp32s3
./tools/idf.sh idf.py -B build/esp32s3 \
    -D SDKCONFIG=build/esp32s3/sdkconfig \
    -p /dev/ttyACM0 -b 230400 app-flash
```

Run the ESP-NOW API smoke test:

```sh
./tools/idf.sh python tests/device/test_espnow.py --port /dev/ttyACM0
```

Run the receive/stability test with a MicroPython sender when available:

```sh
./tools/idf.sh python tests/espnow_stability/host.py \
    --receiver-port /dev/ttyACM0 \
    --sender-port /dev/ttyACM1 \
    --duration 60 \
    --interval-ms 20 \
    --payload-len 32 \
    --json build/espnow-60s.json
```

## Failure Modes Learned From Hardware

- **AP channel changes:** A phone hotspot changed channel 10 -> 12 -> 10 and
  later 4 -> 5. Provisioning and data failed intermittently. Use a fixed 2.4 GHz
  channel on a controlled AP for tests and production.
- **Download mode:** A reset sequence that leaves DTR/GPIO0 low after reset puts
  the classic ESP32 into UART download mode. Normal reset must release DTR and
  pulse RTS, then leave both in the normal-boot state.
- **MicroPython ESP8266:** It was used only for an exploratory test and was
  abandoned for the final classic-ESP32 provisioning test. Do not treat it as
  the supported provisioning path without revalidating it.
- **Power save:** The Xedge32 receiver disables Wi-Fi power save while open.
  Keep this behavior unless the API contract is intentionally changed.
- **P4:** ESP-NOW bindings are not available on ESP32-P4 ESP-Hosted builds.
- **Secrets:** Do not put AP names, passwords, or device MACs in repository
  documentation or test defaults. Pass provisioning data at run time.

## Agent Checklist

Before changing ESP-NOW code:

1. Read `main/BaESP32.c` ESP-NOW section and `doc/source/ESPNow.rst`.
2. Preserve the one-receiver singleton and callback lifetime rules.
3. Keep callback work short and move application processing to a timer/job.
4. Preserve `WIFI_PS_NONE` while the receiver is open and restore the previous
   mode on close.
5. Keep payload limits at 1..250 bytes unless the API and tests are explicitly
   extended.
6. Do not expose peer management or encryption without designing and testing
   those lifetimes.
7. Build ESP32, ESP32-S3, and ESP32-P4 when shared native code changes. P4 must
   compile with the ESP-NOW section excluded.
8. Run `tests/device/test_espnow.py` and the provisioning test on hardware when
   the change affects send/receive behavior.
9. Check that no credentials or private MAC addresses are staged for commit.
