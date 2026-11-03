#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import time

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "espnow_stability"))
from host import lua_send
from test_xedge32 import Shell

CONTROLLER_PORT = "/dev/ttyACM0"
NODE_PORT = "/dev/ttyUSB0"

CMAC_RE = re.compile(r"(?m)^CMAC\s+([0-9a-fA-F]{12})")
CHAN_RE = re.compile(r"(?m)^CHAN\s+(\d+)")
NODE_DATA_RE = re.compile(r"DATA seq=(\d+) ok=([01]) channel=(\d+)")
NODE_PROV_RE = re.compile(r"provisioned: controller ([0-9a-f:]+) channel (\d+)")


def erase_node_nvs():
    cmd = [
        sys.executable, "-m", "esptool", "--chip", "esp32",
        "-p", NODE_PORT, "-b", "115200",
        "--before", "default-reset", "--after", "hard-reset",
        "erase-region", "0x9000", "0x6000",
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.STDOUT)
    time.sleep(0.5)


def reset_node():
    ser = serial.Serial(NODE_PORT, 115200, timeout=0.1)
    ser.dtr = False
    ser.rts = True
    time.sleep(0.15)
    ser.rts = False
    time.sleep(0.2)
    return ser


def open_controller():
    last = None
    for attempt in range(3):
        shell = Shell(CONTROLLER_PORT)
        try:
            shell.open(30.0)
            time.sleep(1.0)
            if not shell.sync(20.0):
                raise RuntimeError("shell not ready")
            return shell
        except Exception as exc:
            last = exc
            shell.close()
            print("controller open attempt %d failed: %s" % (attempt + 1, exc))
            time.sleep(3.0)
    raise last


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-config", action="store_true",
                        help="do not erase the ESP32 NVS before testing")
    args = parser.parse_args()

    if not args.keep_config:
        print("erasing transmitter NVS")
        erase_node_nvs()
    shell = open_controller()
    node = reset_node()
    controller_text = ""
    node_text = ""
    provok = None
    data = []
    node_data = []
    node_prov = None
    try:
        lua_send(
            shell,
            'function onRx(d,s,i) print("PKT",#d,d:sub(1,4)) end',
            1.5,
        )
        lua_send(shell, 'rx=assert(esp32.espnow{callback=onRx})', 3.0)
        out = lua_send(
            shell,
            'local m=esp32.mac():gsub(".",function(c)'
            'return string.format("%02x",c:byte())end);print("CMAC",m)',
            1.5,
        )
        cmac_match = CMAC_RE.search(out)
        out = lua_send(shell, 'print("CHAN",rx:info().channel)', 1.5)
        chan_match = CHAN_RE.search(out)
        if not cmac_match or not chan_match:
            raise RuntimeError("controller MAC/channel not available")
        cmac = cmac_match.group(1)
        channel = int(chan_match.group(1))
        print("controller mac=%s channel=%d" % (cmac, channel))
        lua_send(
            shell,
            'provpkt="PROV"..string.char(1)..esp32.mac()..'
            'string.char(18,52,86,120)..string.char(rx:info().channel)',
            1.5,
        )

        start = time.time()
        next_send = start
        deadline = start + 45.0
        while time.time() < deadline:
            now = time.time()
            if provok is None and now >= next_send:
                shell.ser.write(b"rx:broadcast(provpkt)\n")
                shell.ser.flush()
                next_send += 0.1

            chunk = node.read(4096)
            if chunk:
                text = chunk.decode("utf-8", "replace")
                node_text += text
                for line in text.splitlines():
                    if "PROV:" in line or "PROVOK" in line or "DATA" in line:
                        print("node:", line)
                for match in NODE_PROV_RE.finditer(node_text):
                    node_prov = match.groups()
                for match in NODE_DATA_RE.finditer(node_text):
                    item = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
                    if item not in node_data:
                        node_data.append(item)

            out = shell.drain(0.02)
            if out:
                controller_text += out
                for line in out.splitlines():
                    if line.startswith("PKT"):
                        print("controller:", line)
                if "PKT\t14\tPOK" in controller_text or "PKT 14 POK" in controller_text:
                    provok = True
                for match in re.finditer(r"(?m)^PKT\s+32\s+XNW1", controller_text):
                    data.append(match.start())

            if provok and len(data) >= 3 and len(node_data) >= 3:
                break

        if provok is None:
            print("controller did not see PROVOK")
        if not data:
            print("controller did not see DATA")
        if not node_data:
            print("node did not log DATA")
    finally:
        try:
            node.close()
        except Exception:
            pass
        try:
            shell.send("if rx then pcall(function() rx:close() end) rx=nil end", 1.0)
        except Exception:
            pass
        shell.close()

    print("")
    print("node provisioned: %s" % (node_prov,))
    print("controller POK: %s" % (provok,))
    print("node data: %s" % (node_data[:5],))
    print("controller data: %s" % (data[:5],))
    ok = (
        bool(provok)
        and len(node_data) >= 3
        and len(data) >= 3
        and all(ok == 1 for _, ok, _ in node_data[:3])
    )
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
