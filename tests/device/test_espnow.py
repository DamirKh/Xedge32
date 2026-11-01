#!/usr/bin/env python3
import argparse
import os
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(__file__))
from test_xedge32 import MARK_RE, Shell, lua_marker


def reset_sender(port):
    with serial.Serial(port, 115200, timeout=0.1) as ser:
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.15)
        ser.setDTR(True)
        ser.setRTS(False)
        ser.setDTR(False)
        ser.setRTS(False)
        time.sleep(6.0)


def wait_marker(shell, name, timeout):
    end = time.time() + timeout
    data = ""
    while time.time() < end:
        data += shell.drain(0.5)
        for match in MARK_RE.finditer(data):
            if match.group(1) == name:
                return match, data
    return None, data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--sender-port")
    parser.add_argument("--expect-mac")
    parser.add_argument("--payload-len", type=int, default=0)
    parser.add_argument("--rx-timeout", type=float, default=30.0)
    args = parser.parse_args()

    shell = Shell(args.port)
    results = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        print("%s %-18s %s" % ("PASS" if ok else "FAIL", name, detail))

    def skip(name, detail=""):
        results.append((name, None, detail))
        print("SKIP %-18s %s" % (name, detail))

    try:
        shell.open()

        match, _ = lua_marker(
            shell,
            'if type(esp32.espnow)~="function" then '
            'print("TEST|espnow_nocb|FAIL|missing") else '
            'local ok,err=pcall(esp32.espnow,{});'
            'print("TEST|espnow_nocb|"..(not ok and "OK" or "FAIL")'
            '.."|"..tostring(err)) end',
        )
        record(
            "espnow_nocb",
            bool(match and match.group(2) == "OK"),
            match.group(3) if match else "no marker",
        )

        match, _ = lua_marker(
            shell,
            'local a=assert(esp32.espnow{callback=function() end});'
            'local b,err=esp32.espnow{callback=function() end};'
            'print("TEST|espnow_dup|"..(b==nil and err and "OK" or "FAIL")'
            '.."|"..tostring(err));a:close()',
        )
        record(
            "espnow_dup",
            bool(match and match.group(2) == "OK"),
            match.group(3) if match else "no marker",
        )

        shell.send(
            'function rxmac(s) return (s:gsub(".",function(c)'
            'return string.format("%02x",c:byte())end)) end',
            timeout=3.0,
        )
        shell.send(
            'function onRx(d,s,i) print("TEST|espnow_rx|OK|"..#d.."|"'
            '..rxmac(s).."|"..tostring(i.rssi).."|"'
            '..tostring(i.channel).."|"..tostring(i.len)) end',
            timeout=3.0,
        )
        shell.send('rx=assert(esp32.espnow{callback=onRx})', timeout=3.0)
        match, _ = lua_marker(
            shell,
            'i=rx:info();print("TEST|espnow_open|OK|"'
            '..tostring(i.channel).."|"..tostring(i.version).."|"'
            '..tostring(i.drops))',
        )
        open_match = match
        open_fields = match.group(3).split("|") if match else []
        channel = open_fields[0] if len(open_fields) > 0 else "?"
        version = open_fields[1] if len(open_fields) > 1 else "?"
        record(
            "espnow_open",
            bool(open_match and open_match.group(2) == "OK"),
            "channel=%s version=%s" % (channel, version),
        )

        if args.sender_port:
            try:
                reset_sender(args.sender_port)
            except serial.SerialException as exc:
                print("sender reset failed: %s" % exc)

            match, output = wait_marker(shell, "espnow_rx", args.rx_timeout)
            fields = match.group(3).split("|") if match else []
            detail = "no marker"
            ok = bool(match and match.group(2) == "OK")
            if match:
                detail = "len=%s src=%s rssi=%s channel=%s info_len=%s" % tuple(
                    (fields + ["?"] * 5)[:5]
                )
                if args.payload_len and fields and int(fields[0]) != args.payload_len:
                    ok = False
                if args.expect_mac and len(fields) > 1:
                    ok = ok and fields[1].lower() == args.expect_mac.lower().replace(":", "")
            record("espnow_rx", ok, detail)
        else:
            skip("espnow_rx", "no --sender-port")

        match, _ = lua_marker(
            shell,
            'print("TEST|espnow_close|"..(rx:close() and "OK" or "FAIL"))',
        )
        record(
            "espnow_close",
            bool(match and match.group(2) == "OK"),
            match.group(3) if match else "no marker",
        )
        match, _ = lua_marker(
            shell,
            'print("TEST|espnow_close2|"..(rx:close() and "OK" or "FAIL"))',
        )
        record(
            "espnow_close2",
            bool(match and match.group(2) == "OK"),
            match.group(3) if match else "no marker",
        )

        match, _ = lua_marker(
            shell,
            'local ok,err=pcall(function() return rx:info() end);'
            'print("TEST|espnow_closedinfo|"..(not ok and "OK" or "FAIL")'
            '.."|"..tostring(err))',
        )
        record(
            "espnow_closedinfo",
            bool(match and match.group(2) == "OK"),
            match.group(3) if match else "no marker",
        )
    finally:
        shell.close()

    failed = [name for name, ok, _ in results if ok is False]
    passed = sum(1 for _, ok, _ in results if ok is True)
    skipped = sum(1 for _, ok, _ in results if ok is None)
    print(
        "\n%d passed, %d failed, %d skipped"
        % (passed, len(failed), skipped)
    )
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
