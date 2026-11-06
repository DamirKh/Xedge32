# ESP-NOW sensor node packet format: `XNW1` v2

This document defines the application payload sent by the battery sensor node
(`examples/espnow_sensor_node`) to the Xedge32 controller.

Authoritative sources:

- `examples/espnow_sensor_node/main/payload.h` — C layout.
- `examples/espnow_sensor_node/tools/decode.py` — host decoder.
- `doc/AI/ESPNow-Agent-Guide.md` — provisioning and the controller-side Lua API.

The provisioning packets (`PROV`, `POK`) are separate and unchanged. See the
agent guide for those.

## Status

| Item | Value |
| --- | --- |
| Payload magic | `XNW1` |
| Version | 2 |
| Fixed size | 96 bytes |
| Byte order | Little-endian |
| Transport | ESP-NOW unicast, node to controller |
| Encryption | None |
| Application ACK | None; link-layer ACK only |
| Node retries | Up to 3 attempts, 150 ms each |

The packet is a fixed-size structure, not a TLV stream. Slots that are not used
are zero-filled.

## Layout

Header, 28 bytes:

| Offset | Size | Type | Field |
| ---: | ---: | --- | --- |
| 0 | 4 | bytes | magic `"XNW1"` (`58 4e 57 31`) |
| 4 | 1 | u8 | version, always `2` |
| 5 | 1 | u8 | flags |
| 6 | 1 | u8 | `slot_count`, 0..5 |
| 7 | 1 | u8 | reserved, zero |
| 8 | 4 | u32 | `seq` |
| 12 | 4 | u32 | `boot_id` |
| 16 | 4 | u32 | `awake_ms` |
| 20 | 2 | u16 | `battery_mv` |
| 22 | 2 | i16 | `chip_temp`, 1/16 °C |
| 24 | 1 | u8 | `reset_reason` |
| 25 | 1 | u8 | `wake_cause` |
| 26 | 2 | u16 | reserved, zero |

Sensor table, 60 bytes:

| Offset | Size | Type | Field |
| ---: | ---: | --- | --- |
| 28 | 8 | bytes | ROM code, least significant byte first |
| 36 | 2 | i16 | temperature, 1/16 °C |
| 38 | 1 | u8 | status |
| 39 | 1 | u8 | reserved, zero |

The table holds five 12-byte entries at offsets 28, 40, 52, 64 and 76.

Extension area, 8 bytes:

| Offset | Size | Field |
| ---: | ---: | --- |
| 88 | 8 | reserved for future humidity/pressure data, zero in v2 |

Total: `28 + 5 * 12 + 8 = 96` bytes.

## Flags

| Bit | Mask | Name | Meaning |
| ---: | ---: | --- | --- |
| 0 | `0x01` | `SETUP` | Packet was sent by the setup flow or in fast mode while the jumper was installed. |
| 1 | `0x02` | `FAULT` | `slot_count == 0`, or at least one configured slot is not `OK`. |
| 2 | `0x04` | `LOW_BATT` | `battery_mv != 0` and below 3200 mV. |
| 3 | `0x08` | `COLD_BOOT` | First packet after power-on. |
| 4..7 | — | — | Reserved, zero. |

## Sensor slot status

| Value | Name | Meaning |
| ---: | --- | --- |
| 0 | `ABSENT` | Slot is not used. The whole 12-byte slot is zero. |
| 1 | `OK` | Temperature was read and passed the CRC check. |
| 2 | `MISSING` | ROM is configured, but the device did not respond. |
| 3 | `ERROR` | The device responded, but the read or CRC check failed. |

A slot with `status == ABSENT` must have an all-zero ROM. A configured slot keeps
its ROM even when the sensor is missing, so the controller can still attribute
the fault to a known sensor.

## Field semantics

`seq`
: Increments by one for every transmitted packet, starting at 1 after power-on.
  It is not persisted across power cycles. At 300 s per packet it wraps after
  roughly 40 000 years, so wraparound is not a practical concern.

`boot_id`
: Random non-zero 32-bit value generated at power-on. Together with the source
  MAC it identifies a boot session.

Deduplication
: The controller should key packets by source MAC and accept a packet when
  `boot_id` differs from the stored one, or when `boot_id` matches and `seq` is
  greater than the last accepted `seq`. Duplicates and older packets are
  ignored. The node retries on link-layer failure, so duplicates are possible
  in principle.

`awake_ms`
: Milliseconds since boot, from `esp_timer_get_time()`. Wraps after about
  49.7 days. In v2 the node stays awake, so this is simply uptime. If deep sleep
  is added later, it becomes time since wake and the meaning of `boot_id`/`seq`
  changes to RTC-retained values.

`battery_mv`
: Supply voltage in millivolts, measured once per cycle before the packet is
  built. `0` means "not measured" or "not supported". The reference hardware
  uses a 1:2 divider on GPIO35 (ADC1 channel 7), so the ADC value is doubled in
  firmware.

`chip_temp`
: Internal chip temperature in 1/16 °C. On the classic ESP32, IDF 6.1 does not
  expose an internal temperature sensor, so the node sends `0x8000`
  (`-32768`). The field is kept so that a future board or SoC can populate it
  without changing the layout.

`reset_reason`
: `esp_reset_reason_t` value from ESP-IDF. Common values:

  | Value | Reason |
  | ---: | --- |
  | 0 | unknown |
  | 1 | power-on |
  | 2 | external pin |
  | 3 | software reset |
  | 4 | panic |
  | 5 | interrupt watchdog |
  | 6 | task watchdog |
  | 7 | other watchdog |
  | 8 | deep-sleep wake |
  | 9 | brownout |
  | 10 | SDIO |
  | 11 | USB peripheral |
  | 12 | JTAG |

`wake_cause`
: `0` in v2, because the node does not sleep. Reserved for
  `esp_sleep_get_wakeup_cause()` values when deep sleep is added.

`slot_count`
: Number of configured slots, 0..5. Slots at indexes `slot_count` and above are
  all zero.

`SETUP` flag
: Set for packets sent after provisioning while the jumper is still installed.
  This is the fast commissioning loop. It is not a security indicator.

`FAULT` flag
: Set when `slot_count == 0` or any configured slot has status `MISSING` or
  `ERROR`. The controller can use the per-slot status for details.

## Temperature encoding

Temperatures are signed 16-bit integers in units of 1/16 °C, which is exactly
the DS18B20 scratchpad format:

| Temperature | Raw value | Bytes |
| --- | ---: | --- |
| 25.0625 °C | 401 | `91 01` |
| 23.5 °C | 376 | `78 01` |
| 0 °C | 0 | `00 00` |
| -0.0625 °C | -1 | `ff ff` |
| -10.125 °C | -162 | `5e ff` |
| unavailable / error | `0x8000` | `00 80` |

The representable range is -2048.0 to +2047.9375 °C. DS18B20 itself is limited
to -55..+125 °C. A read error is encoded as `0x8000`, and the slot status is
`ERROR` or `MISSING`.

## Example packet

Two sensors: one at 23.5 °C, one at -10.125 °C. Normal mode, no faults, battery
3372 mV, fifth packet of the boot session.

```text
584e5731020002000500000078563412
102700002c0d00800100000028ff641e
5b16040d78010100283c2a1122334455
5eff0100000000000000000000000000
00000000000000000000000000000000
00000000000000000000000000000000
```

Decoded:

```text
magic          XNW1
version        2
flags          0x00 (normal, no fault)
slot_count     2
seq            5
boot_id        0x12345678
awake_ms       10000
battery_mv     3372
chip_temp      0x8000 (unavailable)
reset_reason   1 (power-on)
wake_cause     0
sensor[0]      rom 28ff641e5b16040d, 23.500 C, status 1 (OK)
sensor[1]      rom 283c2a1122334455, -10.125 C, status 1 (OK)
sensor[2..4]   absent
ext            all zero
```

## Parsing

### Python

```python
import struct

HEADER = "<4sBBBBIIIHhBBH"
SLOT = "<8shBB"
SIZE = 96
STATUS = {0: "ABSENT", 1: "OK", 2: "MISSING", 3: "ERROR"}


def decode(buf: bytes) -> dict:
    if len(buf) != SIZE:
        raise ValueError(f"expected {SIZE} bytes, got {len(buf)}")
    (magic, version, flags, slot_count, _r0, seq, boot_id,
     awake_ms, battery_mv, chip_temp, reset_reason, wake_cause, _r1) = (
        struct.unpack_from(HEADER, buf, 0)
    )
    if magic != b"XNW1":
        raise ValueError("bad magic")
    if version != 2:
        raise ValueError(f"unsupported version {version}")
    sensors = []
    for index in range(5):
        rom, temp, status, _ = struct.unpack_from(SLOT, buf, 28 + index * 12)
        sensors.append({
            "rom": rom.hex(),
            "temp_c": None if temp == -32768 else temp / 16.0,
            "status": STATUS.get(status, f"UNKNOWN({status})"),
        })
    return {
        "flags": flags,
        "slot_count": slot_count,
        "seq": seq,
        "boot_id": boot_id,
        "awake_ms": awake_ms,
        "battery_mv": battery_mv,
        "chip_temp_c": None if chip_temp == -32768 else chip_temp / 16.0,
        "reset_reason": reset_reason,
        "wake_cause": wake_cause,
        "sensors": sensors,
    }
```

`examples/espnow_sensor_node/tools/decode.py` wraps this in a command-line tool
that accepts a hex string or a binary file.

### Lua (Xedge32 controller)

```lua
local function u16(s, o) return s:byte(o) + s:byte(o + 1) * 256 end
local function u32(s, o) return u16(s, o) + u16(s, o + 2) * 65536 end

local function i16(s, o)
   local v = u16(s, o)
   if v >= 32768 then v = v - 65536 end
   return v
end

local function hex(s)
   return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

local function decodeXNW1(data)
   if #data ~= 96 or data:sub(1, 4) ~= "XNW1" or data:byte(5) ~= 2 then
      return nil
   end
   local packet = {
      flags = data:byte(6),
      slot_count = data:byte(7),
      seq = u32(data, 9),
      boot_id = u32(data, 13),
      awake_ms = u32(data, 17),
      battery_mv = u16(data, 21),
      chip_temp = i16(data, 23),
      reset_reason = data:byte(25),
      wake_cause = data:byte(26),
      sensors = {},
   }
   for i = 0, 4 do
      local offset = 29 + i * 12
      local raw = i16(data, offset + 8)
      packet.sensors[i + 1] = {
         rom = hex(data:sub(offset, offset + 7)),
         temp = raw == -32768 and nil or raw / 16,
         status = data:byte(offset + 10),
      }
   end
   return packet
end
```

## Versioning and compatibility

- The magic stays `XNW1` for every version. The `version` byte selects the
  layout.
- The controller must check both the length (96) and `version == 2` before
  parsing. Unknown lengths or versions are ignored, not guessed.
- The 32-byte `XNW1` packet used by
  `tests/espnow_provision_esp32` is a test-only v1 format: it has no version
  byte and no sensor table. It is not produced by the sensor node. Length is the
  reliable discriminator.
- Adding humidity or pressure data requires a layout change. Reserved bytes in
  `ext` are zero in v2; using them requires a version bump and an update to this
  document, `payload.h`, `decode.py`, and the controller parser.
- Never change the meaning of an existing field without bumping `version`.

## Controller-side handling rules

1. Accept only 96-byte packets with magic `XNW1` and version 2.
2. Key state by source MAC.
3. Deduplicate by `(boot_id, seq)` as described above.
4. Key sensors by ROM, not by slot index. Slot order is the discovery order and
   can change after re-provisioning.
5. Treat a slot as absent when its status is 0 or its ROM is all zero.
6. Mark data stale when no packet has arrived for a configured timeout. The node
   sends every 300 s in normal mode.
7. Use `battery_mv == 0` as "unknown", not as an empty battery.

## Security notes

The payload is unencrypted and has no authentication. The provisioning token is
a matching aid, not a secret. Anyone in radio range can observe the packets.
Do not place credentials or private data in the payload. Sensor ROM codes are
public device identifiers.

## Change log

| Date | Version | Change |
| --- | --- | --- |
| 2026-11-06 | 2 | Initial sensor node format: header, five sensor slots, battery, reserved extension. |
