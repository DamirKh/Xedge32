#!/usr/bin/env python3
"""Decode an XNW1 v2 sensor node packet.

Examples:
    decode.py --hex 584e5731...
    decode.py packet.bin
    decode.py --hex ... --json
"""

import argparse
import json
import struct
import sys

HEADER = "<4sBBBBIIIHhBBH"
SLOT = "<8shBB"
SIZE = 96
STATUS = {
    0: "ABSENT",
    1: "OK",
    2: "MISSING",
    3: "ERROR",
}
FLAGS = (
    (0x01, "SETUP"),
    (0x02, "FAULT"),
    (0x04, "LOW_BATT"),
    (0x08, "COLD_BOOT"),
)


def flag_names(flags):
    return [name for mask, name in FLAGS if flags & mask]


def decode(buf):
    if len(buf) != SIZE:
        raise ValueError("expected %d bytes, got %d" % (SIZE, len(buf)))

    (magic, version, flags, slot_count, reserved0, seq, boot_id,
     awake_ms, battery_mv, chip_temp, reset_reason, wake_cause,
     reserved1) = struct.unpack_from(HEADER, buf, 0)

    if magic != b"XNW1":
        raise ValueError("bad magic: %r" % magic)
    if version != 2:
        raise ValueError("unsupported version: %d" % version)
    if slot_count > 5:
        raise ValueError("slot_count out of range: %d" % slot_count)

    sensors = []
    for index in range(5):
        rom, temp, status, _reserved = struct.unpack_from(SLOT, buf, 28 + index * 12)
        sensors.append({
            "index": index,
            "rom": rom.hex(),
            "temp_c": None if temp == -32768 else temp / 16.0,
            "status_code": status,
            "status": STATUS.get(status, "UNKNOWN(%d)" % status),
            "configured": index < slot_count,
        })

    return {
        "magic": magic.decode("ascii"),
        "version": version,
        "flags": flags,
        "flag_names": flag_names(flags),
        "slot_count": slot_count,
        "seq": seq,
        "boot_id": boot_id,
        "boot_id_hex": "0x%08x" % boot_id,
        "awake_ms": awake_ms,
        "battery_mv": battery_mv,
        "chip_temp_c": None if chip_temp == -32768 else chip_temp / 16.0,
        "reset_reason": reset_reason,
        "wake_cause": wake_cause,
        "reserved0": reserved0,
        "reserved1": reserved1,
        "sensors": sensors,
    }


def read_hex(text):
    cleaned = "".join(text.split())
    cleaned = cleaned.replace(":", "").replace(",", "").replace("0x", "")
    return bytes.fromhex(cleaned)


def print_text(packet):
    print("magic          %s" % packet["magic"])
    print("version        %d" % packet["version"])
    print("flags          0x%02x %s" % (
        packet["flags"], " ".join(packet["flag_names"]) or "(none)"))
    print("slot_count     %d" % packet["slot_count"])
    print("seq            %d" % packet["seq"])
    print("boot_id        %s" % packet["boot_id_hex"])
    print("awake_ms       %d" % packet["awake_ms"])
    print("battery_mv     %d" % packet["battery_mv"])
    print("chip_temp      %s" % (
        "unavailable" if packet["chip_temp_c"] is None
        else "%.4f C" % packet["chip_temp_c"]))
    print("reset_reason   %d" % packet["reset_reason"])
    print("wake_cause     %d" % packet["wake_cause"])

    for sensor in packet["sensors"]:
        if not sensor["configured"]:
            continue
        temp = ("unavailable" if sensor["temp_c"] is None
                else "%.4f C" % sensor["temp_c"])
        print("sensor[%d]      rom %s, %s, status %d (%s)" % (
            sensor["index"], sensor["rom"], temp,
            sensor["status_code"], sensor["status"]))

    for sensor in packet["sensors"]:
        if sensor["configured"]:
            continue
        if sensor["rom"] != "0000000000000000" or sensor["status_code"] != 0:
            print("sensor[%d]      unexpected data outside slot_count" % sensor["index"])


def main():
    parser = argparse.ArgumentParser(description="Decode an XNW1 v2 packet")
    parser.add_argument("file", nargs="?", help="binary packet file")
    parser.add_argument("--hex", metavar="HEX", help="hex-encoded packet")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    try:
        if args.hex is not None:
            buf = read_hex(args.hex)
        elif args.file is not None:
            with open(args.file, "rb") as handle:
                buf = handle.read()
        else:
            buf = read_hex(sys.stdin.read())
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    try:
        packet = decode(buf)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(packet, indent=2))
    else:
        print_text(packet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
