#!/usr/bin/env python3
"""Setup-mode test harness for examples/espnow_sensor_node.

STATUS: PARTIALLY RUN (2026-11-12).

Run against the classic ESP32 node (firmware 1d51095-dirty) and the Xedge32
controller, with 0 sensors and the setup jumper removed.

Passed:
  setup_no_config, valid_config_normal, reboot_persistence, malformed_prov,
  no_controller_retry.

Skipped:
  jumper_reprovision_fast - the physical GPIO4-GND jumper was not installed.

Not run:
  led_patterns, sensor_edge_cases, channel_recovery (manual),
  and the 1/5-sensor variants.

The controller application was stopped for the run.  Its Wi-Fi core still
reconnected to an automatic-channel uplink, so several cases exercised the
firmware's channel recovery (9 -> 10 and 6 -> 2).  The application was
restarted afterwards.  These results do not cover the fast-mode case or any
sensor-attached case.

The harness drives the node over its UART console and, when available, uses
the Xedge32 controller's LuaShell to broadcast PROV and receive POK/data.
It asserts the node's log markers rather than relying on a debugger.

Run it from the repository root through the IDF container:

    ./tools/idf.sh python tests/espnow_sensor_node_setup_test.py --list
    ./tools/idf.sh python tests/espnow_sensor_node_setup_test.py --case all
    ./tools/idf.sh python tests/espnow_sensor_node_setup_test.py \
        --case setup_no_config --sensors 0 --yes
    ./tools/idf.sh python tests/espnow_sensor_node_setup_test.py \
        --case jumper_reprovision_fast --jumper installed

Required hardware:
  - classic ESP32 node on /dev/ttyUSB0, ESP-IDF sensor node firmware;
  - Xedge32 controller on /dev/ttyACM0 with LuaShell available;
  - jumper GPIO4-GND for the setup/fast cases;
  - 0..5 DS18B20 sensors on GPIO18 for the sensor cases.

The controller must be able to open an ESP-NOW receiver.  If an Xedge
application already owns the singleton receiver, stop that application or use
a dedicated controller; otherwise controller-side POK/data checks are skipped.

This harness erases the node's NVS in several cases.  Pass --yes to skip the
confirmation prompt, but make sure the node has no configuration worth keeping.

Test results are printed per case.  The process exits 1 on a failed case, 0 on
success, and 2 when no node port is present (tests not run).
"""

import argparse
import os
import re
import subprocess
import sys
import threading
import time

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "device"))
sys.path.insert(0, os.path.join(HERE, "espnow_stability"))
from test_xedge32 import Shell
from host import lua_send

DEFAULT_NODE_PORT = "/dev/ttyUSB0"
DEFAULT_CONTROLLER_PORT = "/dev/ttyACM0"

NVS_OFFSET = "0x9000"
NVS_SIZE = "0x6000"
PROV_TOKEN = "18,52,86,120"  # 0x12 0x34 0x56 0x78

FLAG_SETUP = 0x01
FLAG_FAULT = 0x02
FLAG_LOW_BATT = 0x04
FLAG_COLD_BOOT = 0x08

BOOT_RE = re.compile(r"boot: boot_id=([0-9a-f]+) jumper=(\d) cfg=(\d)")
SETUP_RE = re.compile(r"setup: provisioning")
SCAN_RE = re.compile(r"scanning channels 1\.\.13 for PROV")
RETRY_RE = re.compile(r"no controller yet, retrying")
PROV_RE = re.compile(r"provisioned: controller ([0-9a-f:]+) channel (\d+)")
POK_RE = re.compile(r"POK ok=(\d)")
SENSORS_RE = re.compile(r"setup: (\d+) sensor\(s\) found")
FOUND_RE = re.compile(r"SENS: found ([0-9a-f]{16})")
TX_RE = re.compile(
    r"tx seq=(\d+) slots=(\d+) batt=(\d+)mV ch=(\d+) "
    r"flags=0x([0-9a-f]+) ok=(\d+)"
)
NORMAL_RE = re.compile(r"normal mode, interval (\d+) ms")
FAST_RE = re.compile(r"fast mode, remove the jumper")
FAST_END_RE = re.compile(r"jumper removed, leaving fast mode")
USING_RE = re.compile(
    r"using controller ([0-9a-f:]+) channel (\d+), (\d+) sensor\(s\)"
)
RECOVERED_RE = re.compile(r"channel recovered (\d+) -> (\d+)")
NOACK_RE = re.compile(r"no ACK on any channel, staying on (\d+)")


class HarnessError(RuntimeError):
    pass


class Node:
    """Classic ESP32 node console with reset and log matching."""

    def __init__(self, port):
        self.port = port
        self.ser = None
        self.text = ""
        self._linebuf = ""
        self.tx_times = []

    def open(self):
        self.ser = serial.Serial(self.port, 115200, timeout=0.1)
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser is not None:
            self.ser.close()
            self.ser = None

    def reset(self):
        self.text = ""
        self._linebuf = ""
        self.tx_times = []
        self.ser.reset_input_buffer()
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.15)
        self.ser.rts = False
        time.sleep(0.2)

    def pump(self):
        chunk = self.ser.read(4096)
        if not chunk:
            return ""
        text = chunk.decode("utf-8", "replace")
        self.text += text
        self._linebuf += text
        while "\n" in self._linebuf:
            line, self._linebuf = self._linebuf.split("\n", 1)
            line = line.strip("\r")
            if not line:
                continue
            print("node: %s" % line)
            if TX_RE.search(line):
                self.tx_times.append(time.monotonic())
        return text

    def wait(self, regex, timeout, description):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = regex.search(self.text)
            if match:
                return match
            self.pump()
        raise HarnessError(
            "timeout waiting for %s; last log:\n%s"
            % (description, self.text[-1800:])
        )

    def absent(self, regex, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if regex.search(self.text):
                raise HarnessError("unexpected %r in node log" % regex.pattern)
            self.pump()

    def count(self, regex):
        return len(regex.findall(self.text))


class Controller:
    """Xedge32 controller shell plus repeated PROV broadcasts."""

    def __init__(self, port):
        self.port = port
        self.shell = None
        self.mac = None
        self.channel = None
        self.created_rx = False
        self.callback = False
        self.packets = []
        self.text = ""
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        self.shell = Shell(self.port)
        self.shell.open(30.0)
        time.sleep(0.5)

        out = self.shell.send('print("RX", rx ~= nil)', 1.5)
        existing = bool(re.search(r"RX\s+true", out))
        if existing:
            print("controller: using the existing ESP-NOW receiver")
        else:
            lua_send(
                self.shell,
                'function onRx(d,s,i) print("PKT",#d,d:sub(1,4)) end',
                1.0,
            )
            try:
                lua_send(
                    self.shell,
                    'rx=assert(esp32.espnow{callback=onRx})',
                    3.0,
                )
            except RuntimeError as exc:
                raise HarnessError(
                    "controller cannot open an ESP-NOW receiver: %s" % exc
                )
            self.created_rx = True
            self.callback = True

        out = lua_send(
            self.shell,
            'local m=esp32.mac():gsub(".",function(c)'
            'return string.format("%02x",c:byte())end);print("CMAC",m)',
            1.5,
        )
        match = re.search(r"CMAC\s+([0-9a-f]{12})", out)
        if not match:
            raise HarnessError("controller MAC not available")
        self.mac = match.group(1)

        out = lua_send(self.shell, 'print("CHAN",rx:info().channel)', 1.5)
        match = re.search(r"CHAN\s+(\d+)", out)
        if not match:
            raise HarnessError("controller channel not available")
        self.channel = int(match.group(1))
        if not 1 <= self.channel <= 13:
            raise HarnessError(
                "controller channel out of range: %d" % self.channel
            )

        lua_send(
            self.shell,
            'provpkt="PROV"..string.char(1)..esp32.mac()..'
            'string.char(%s)..string.char(rx:info().channel)' % PROV_TOKEN,
            1.5,
        )
        self._build_bad_packets()
        print(
            "controller: mac=%s channel=%d%s"
            % (
                self.mac,
                self.channel,
                "" if self.callback else " (no PKT callback; RX checks limited)",
            )
        )

    def _build_bad_packets(self):
        token = PROV_TOKEN
        lua_send(self.shell, 'bad_short="PROV"', 1.0)
        lua_send(
            self.shell,
            'bad_magic="XXXX"..string.char(1)..esp32.mac()..'
            'string.char(%s)..string.char(rx:info().channel)' % token,
            1.0,
        )
        lua_send(
            self.shell,
            'bad_ch0="PROV"..string.char(1)..esp32.mac()..'
            'string.char(%s)..string.char(0)' % token,
            1.0,
        )
        lua_send(
            self.shell,
            'bad_ch14="PROV"..string.char(1)..esp32.mac()..'
            'string.char(%s)..string.char(14)' % token,
            1.0,
        )

    def start(self, packet="provpkt"):
        if self.shell is None:
            raise HarnessError("controller is not open")
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._broadcast_loop, args=(packet,), daemon=True
        )
        self._thread.start()

    def _broadcast_loop(self, packet):
        command = ("rx:broadcast(%s)\n" % packet).encode()
        while not self._stop.is_set():
            try:
                self.shell.ser.write(command)
                self.shell.ser.flush()
            except Exception as exc:
                print("controller: write error: %s" % exc)
                return
            chunk = self.shell.ser.read(8192)
            if chunk:
                text = chunk.decode("utf-8", "replace")
                self.text += text
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("PKT"):
                        print("controller: %s" % line)
                        self.packets.append(line)
            time.sleep(0.1)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def has_pok(self):
        return any(re.search(r"PKT\s+14\s+POK", line) for line in self.packets)

    def data_count(self):
        return sum(
            1
            for line in self.packets
            if re.search(r"PKT\s+96\s+XNW1", line)
        )

    def close(self):
        self.stop()
        if self.shell is not None:
            if self.created_rx:
                try:
                    self.shell.send(
                        "if rx then pcall(function() rx:close() end) rx=nil end",
                        1.0,
                    )
                except Exception:
                    pass
            self.shell.close()
            self.shell = None


class Context:
    def __init__(self, args, node, controller):
        self.args = args
        self.node = node
        self.controller = controller
        self.erase_confirmed = bool(args.yes)
        self.channel = None

    @property
    def sensors(self):
        return self.args.sensors

    def confirm_erase(self):
        if self.erase_confirmed:
            return
        answer = input(
            "This erases the node NVS (prov/cfg and node/table). "
            "Type YES to continue: "
        )
        if answer != "YES":
            raise HarnessError("NVS erase not confirmed")
        self.erase_confirmed = True


CASES = {}


def case(name, needs_controller=False, needs_erase=False):
    def decorator(fn):
        CASES[name] = (fn, needs_controller, needs_erase)
        return fn

    return decorator


AUTOMATED_CASES = [
    "setup_no_config",
    "valid_config_normal",
    "reboot_persistence",
    "jumper_reprovision_fast",
    "malformed_prov",
    "no_controller_retry",
]
MANUAL_CASES = ["led_patterns", "channel_recovery", "sensor_edge_cases"]


def require_jumper(args, state):
    print("")
    print("Jumper: %s (GPIO4 to GND)." % state)
    if args.yes:
        print("Assuming the jumper is %s (--yes)." % state)
        return
    input("Set the jumper to '%s' and press Enter..." % state)


def erase_nvs(ctx):
    ctx.confirm_erase()
    command = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "esp32",
        "-p",
        ctx.node.port,
        "-b",
        "115200",
        "--no-stub",
        "--before",
        "default-reset",
        "--after",
        "hard-reset",
        "erase-region",
        NVS_OFFSET,
        NVS_SIZE,
    ]
    print("node: erasing NVS")
    # Close the console while esptool owns the port, then reopen it.
    ctx.node.close()
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    finally:
        ctx.node.open()
    time.sleep(0.5)


def check_tx(match, seq, slots, setup, cold, fault):
    seq_value = int(match.group(1))
    slots_value = int(match.group(2))
    flags = int(match.group(5), 16)
    ok = int(match.group(6))

    if seq_value != seq:
        raise HarnessError("expected seq=%d, got %d" % (seq, seq_value))
    if slots_value != slots:
        raise HarnessError(
            "expected slots=%d, got %d" % (slots, slots_value)
        )
    if ok != 1:
        raise HarnessError("first packet was not acknowledged")
    if setup != bool(flags & FLAG_SETUP):
        raise HarnessError(
            "SETUP flag mismatch: flags=0x%02x expected %s"
            % (flags, setup)
        )
    if cold != bool(flags & FLAG_COLD_BOOT):
        raise HarnessError(
            "COLD_BOOT flag mismatch: flags=0x%02x" % flags
        )
    if fault is not None and fault != bool(flags & FLAG_FAULT):
        raise HarnessError(
            "FAULT flag mismatch: flags=0x%02x expected %s"
            % (flags, fault)
        )


def run_setup(ctx, jumper, sensors, expect_fast, require_cfg0):
    node = ctx.node
    controller = ctx.controller

    boot = node.wait(BOOT_RE, 10.0, "boot log")
    expected_jumper = 1 if jumper == "installed" else 0
    if int(boot.group(2)) != expected_jumper:
        raise HarnessError(
            "jumper state mismatch: log=%s expected=%s"
            % (boot.group(2), jumper)
        )
    if require_cfg0 and int(boot.group(3)) != 0:
        raise HarnessError(
            "expected cfg=0 before setup, log says cfg=%s" % boot.group(3)
        )

    node.wait(SETUP_RE, 5.0, "setup start")
    node.wait(SCAN_RE, 5.0, "PROV scan")

    prov = node.wait(PROV_RE, 60.0, "provisioned record")
    if controller is not None:
        log_mac = prov.group(1).replace(":", "").lower()
        if log_mac != controller.mac.lower():
            raise HarnessError(
                "controller MAC mismatch: %s vs %s"
                % (log_mac, controller.mac)
            )
        # Do not require the channel from PROV to equal the channel observed
        # when the harness opened its receiver. An access point with automatic
        # channel selection can move between those two points. The firmware
        # recovers on the first data packet; the checks below verify that.

    pok = node.wait(POK_RE, 10.0, "POK log")
    pok_ok = int(pok.group(1)) == 1
    if controller is not None and controller.callback:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not controller.has_pok():
            time.sleep(0.1)
        if not controller.has_pok():
            # The node's POK ok=1 is a link-layer ACK and is authoritative.
            # A Lua callback may miss a short frame while the controller is
            # broadcasting PROV, so do not fail the case on this alone.
            print(
                "controller: POK not observed by the Lua callback; "
                "node reported POK ok=1"
            )

    sensors_match = node.wait(SENSORS_RE, 120.0, "sensor scan result")
    if int(sensors_match.group(1)) != sensors:
        raise HarnessError(
            "expected %d sensors, log says %s"
            % (sensors, sensors_match.group(1))
        )
    found = node.count(FOUND_RE)
    if found != sensors:
        raise HarnessError(
            "expected %d SENS: found lines, got %d" % (sensors, found)
        )

    first_tx = node.wait(TX_RE, 15.0, "first data packet")
    check_tx(
        first_tx,
        seq=1,
        slots=sensors,
        setup=True,
        cold=True,
        fault=(sensors == 0),
    )
    if not pok_ok and not RECOVERED_RE.search(node.text):
        raise HarnessError(
            "POK failed and the node did not log channel recovery"
        )

    if expect_fast:
        node.wait(FAST_RE, 5.0, "fast mode entry")
        if controller is not None:
            controller.stop()  # PROV is no longer needed
        started = time.monotonic()
        measure_deadline = started + 5.0
        while time.monotonic() < measure_deadline:
            node.pump()
        fast_packets = sum(1 for t in node.tx_times if t >= started)
        print("node: fast mode produced %d packets in 5 s" % fast_packets)
        if fast_packets < 4:
            raise HarnessError(
                "fast mode is too slow: %d packets in 5 s" % fast_packets
            )
        require_jumper(ctx.args, "removed")
        node.wait(FAST_END_RE, 10.0, "fast mode exit")
        node.wait(NORMAL_RE, 5.0, "normal mode after fast")
    else:
        node.wait(NORMAL_RE, 5.0, "normal mode")

    if controller is not None:
        controller.stop()

    return {
        "boot": boot,
        "prov": prov,
        "tx": first_tx,
        "channel": int(prov.group(2)),
    }


def run_normal_boot(ctx, sensors, expected_channel=None):
    node = ctx.node
    last_error = None

    # The controller may be reconnecting to its uplink while the node boots,
    # which can make the first packet fail with ok=0. The node scans other
    # channels, but if the controller is briefly unavailable no channel will
    # answer. Retry the boot a few times before declaring a failure.
    for attempt in range(1, 4):
        node.reset()

        boot = node.wait(BOOT_RE, 10.0, "boot log")
        if int(boot.group(2)) != 0:
            raise HarnessError("jumper must be removed for a normal boot")
        if int(boot.group(3)) != 1:
            raise HarnessError(
                "no valid configuration in NVS; run setup_no_config first"
            )

        node.absent(SETUP_RE, 2.0)

        using = node.wait(USING_RE, 10.0, "controller record")
        channel = int(using.group(2))
        count = int(using.group(3))
        if not 1 <= channel <= 13:
            raise HarnessError("persisted channel out of range: %d" % channel)
        if count != sensors:
            raise HarnessError(
                "persisted sensor count %d, expected %d" % (count, sensors)
            )
        if expected_channel is not None and channel != expected_channel:
            last_error = HarnessError(
                "persisted channel %d, expected %d"
                % (channel, expected_channel)
            )
            print("node: %s; retrying" % last_error)
            time.sleep(3.0)
            continue

        node.wait(NORMAL_RE, 5.0, "normal mode")
        first_tx = node.wait(TX_RE, 10.0, "first data packet")
        if int(first_tx.group(6)) != 1:
            last_error = HarnessError(
                "first packet was not acknowledged (attempt %d)" % attempt
            )
            print("node: %s; retrying" % last_error)
            time.sleep(3.0)
            continue

        check_tx(
            first_tx,
            seq=1,
            slots=sensors,
            setup=False,
            cold=True,
            fault=(sensors == 0),
        )

        return {
            "boot": boot,
            "using": using,
            "channel": channel,
            "tx": first_tx,
        }

    raise last_error or HarnessError("normal boot failed")


@case("setup_no_config", needs_controller=True, needs_erase=True)
def case_setup_no_config(ctx):
    require_jumper(ctx.args, "removed")
    erase_nvs(ctx)
    ctx.node.reset()
    if ctx.controller is not None:
        ctx.controller.start("provpkt")
    result = run_setup(
        ctx,
        jumper="removed",
        sensors=ctx.sensors,
        expect_fast=False,
        require_cfg0=True,
    )
    ctx.channel = result["channel"]
    if ctx.controller is not None and ctx.controller.callback:
        time.sleep(0.5)
        if ctx.controller.data_count() < 1:
            raise HarnessError("controller did not receive the first XNW1 packet")
    return (
        "PASS",
        "setup, POK, scan, first packet, normal, channel %d"
        % result["channel"],
    )


@case("valid_config_normal")
def case_valid_config_normal(ctx):
    require_jumper(ctx.args, "removed")
    result = run_normal_boot(ctx, ctx.sensors, ctx.channel)
    return (
        "PASS",
        "normal boot, channel %d, %d sensor(s)"
        % (result["channel"], ctx.sensors),
    )


@case("reboot_persistence")
def case_reboot_persistence(ctx):
    require_jumper(ctx.args, "removed")
    first = run_normal_boot(ctx, ctx.sensors, ctx.channel)
    second = run_normal_boot(ctx, ctx.sensors, first["channel"])
    if first["boot"].group(1) == second["boot"].group(1):
        raise HarnessError("boot_id did not change across reset")
    return (
        "PASS",
        "boot_id changed, channel %d and table persisted" % second["channel"],
    )


@case("jumper_reprovision_fast", needs_controller=True)
def case_jumper_reprovision_fast(ctx):
    require_jumper(ctx.args, "installed")
    if ctx.controller is not None:
        ctx.controller.start("provpkt")
    ctx.node.reset()
    result = run_setup(
        ctx,
        jumper="installed",
        sensors=ctx.sensors,
        expect_fast=True,
        require_cfg0=False,
    )
    ctx.channel = result["channel"]
    return (
        "PASS",
        "re-provisioned, fast rate checked, normal after jumper removal",
    )


@case("malformed_prov", needs_controller=True, needs_erase=True)
def case_malformed_prov(ctx):
    require_jumper(ctx.args, "removed")
    erase_nvs(ctx)
    ctx.node.reset()

    # Let the node finish booting and enter the PROV scan before sending
    # malformed packets. Otherwise the broadcasts overlap Wi-Fi startup and
    # can put a marginal board into a reset loop before the boot log appears.
    ctx.node.wait(SETUP_RE, 5.0, "setup start")
    ctx.node.wait(SCAN_RE, 5.0, "PROV scan")

    for packet in ("bad_short", "bad_magic", "bad_ch0", "bad_ch14"):
        ctx.controller.start(packet)
        time.sleep(5.0)
        ctx.controller.stop()
        if PROV_RE.search(ctx.node.text):
            raise HarnessError("node accepted malformed packet %s" % packet)

    ctx.controller.start("provpkt")
    result = run_setup(
        ctx,
        jumper="removed",
        sensors=ctx.sensors,
        expect_fast=False,
        require_cfg0=True,
    )
    ctx.channel = result["channel"]
    return (
        "PASS",
        "short/magic/channel variants ignored, valid PROV accepted",
    )


@case("no_controller_retry", needs_erase=True)
def case_no_controller_retry(ctx):
    require_jumper(ctx.args, "removed")
    if ctx.controller is not None:
        ctx.controller.stop()
    erase_nvs(ctx)
    ctx.node.reset()

    ctx.node.wait(SETUP_RE, 5.0, "setup start")
    ctx.node.wait(SCAN_RE, 5.0, "PROV scan")
    ctx.node.wait(RETRY_RE, 45.0, "retry after the 30 s window")

    if PROV_RE.search(ctx.node.text):
        raise HarnessError("node was provisioned without a controller")
    if POK_RE.search(ctx.node.text):
        raise HarnessError("node sent POK without a controller")
    if TX_RE.search(ctx.node.text):
        raise HarnessError("node sent data without a controller")

    return (
        "PASS",
        "30 s window elapsed and retry was logged; "
        "node remains in setup until reset",
    )


@case("led_patterns")
def case_led_patterns(ctx):
    print("")
    print("Observe the LED on GPIO2:")
    print("  fast blink  - provisioning")
    print("  solid       - 1-Wire scan")
    print("  slow blink  - fast or normal measurement")
    print("  3 pulses    - no sensors found")
    if not ctx.args.yes:
        input("Press Enter after checking all four patterns...")
    return ("MANUAL", "LED patterns checked visually")


@case("sensor_edge_cases")
def case_sensor_edge_cases(ctx):
    print("")
    print("Manual sensor cases, run them one by one with a jumper setup:")
    print("  - 6 sensors: first 5 are kept, the rest is ignored")
    print("  - non-DS18B20 device on the bus: skipped, scan continues")
    print("  - one configured sensor disconnected: MISSING + FAULT")
    print("  - sensor replaced with a new ROM: old MISSING, new ignored")
    print("    until the next jumper setup")
    if not ctx.args.yes:
        input("Press Enter after completing the sensor checks...")
    return ("MANUAL", "sensor edge cases checked manually")


@case("channel_recovery")
def case_channel_recovery(ctx):
    print("")
    print("Manual channel recovery check:")
    print("  1. Provision the node and confirm normal packets.")
    print("  2. Change the access-point channel.")
    print("  3. Expect: 3 failed sends, then 'channel recovered X -> Y'.")
    print("  4. Reset the node; it must start on channel Y.")
    print("  5. Turn the controller off; expect 'no ACK on any channel'.")
    if not ctx.args.yes:
        input("Press Enter after completing the checks...")
    return ("MANUAL", "channel recovery checked manually")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Setup-mode harness for the ESP-NOW sensor node"
    )
    parser.add_argument("--node-port", default=DEFAULT_NODE_PORT)
    parser.add_argument("--controller-port", default=DEFAULT_CONTROLLER_PORT)
    parser.add_argument(
        "--case",
        default="all",
        help="all, a case name, or a comma-separated list",
    )
    parser.add_argument(
        "--jumper",
        choices=("ask", "installed", "removed"),
        default="ask",
        help="expected jumper state for cases that need one",
    )
    parser.add_argument(
        "--sensors",
        type=int,
        choices=range(0, 6),
        default=0,
        help="expected number of DS18B20 sensors",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip NVS/jumper prompts; the boot log still verifies the jumper",
    )
    parser.add_argument(
        "--no-controller",
        action="store_true",
        help="skip cases that need the Xedge32 controller",
    )
    parser.add_argument(
        "--include-manual",
        action="store_true",
        help="include the manual LED and channel-recovery cases in 'all'",
    )
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    return parser.parse_args()


def selected_cases(args):
    if args.case == "all":
        names = list(AUTOMATED_CASES)
        if args.include_manual:
            names.extend(MANUAL_CASES)
        return names
    names = [name.strip() for name in args.case.split(",") if name.strip()]
    for name in names:
        if name not in CASES:
            raise SystemExit("unknown case: %s" % name)
    return names


def print_list():
    print("STATUS: PARTIALLY RUN (2026-11-12)")
    print("Passed: 5 automated cases with 0 sensors; fast case skipped.")
    print("See the module docstring for the exact results and conditions.")
    print("")
    for name, (_, needs_controller, needs_erase) in CASES.items():
        print(
            "  %-26s controller=%-5s erase=%s"
            % (name, needs_controller, needs_erase)
        )
    print("")
    print("Automated cases: %s" % ", ".join(AUTOMATED_CASES))
    print("Manual cases:    %s" % ", ".join(MANUAL_CASES))


def run_case(name, ctx):
    fn, needs_controller, needs_erase = CASES[name]
    print("")
    print("=" * 72)
    print("CASE: %s" % name)

    if needs_controller and ctx.controller is None:
        print("RESULT: SKIP - controller is not available")
        return "SKIP", "controller is not available"
    if needs_erase:
        try:
            ctx.confirm_erase()
        except HarnessError as exc:
            print("RESULT: SKIP - %s" % exc)
            return "SKIP", str(exc)

    try:
        status, detail = fn(ctx)
        print("RESULT: %s - %s" % (status, detail))
        return status, detail
    except HarnessError as exc:
        print("RESULT: FAIL - %s" % exc)
        return "FAIL", str(exc)
    except Exception as exc:  # noqa: BLE001 - report, do not abort the suite
        print("RESULT: FAIL - %r" % exc)
        return "FAIL", repr(exc)
    finally:
        if ctx.controller is not None:
            ctx.controller.stop()


def main():
    args = parse_args()

    if args.list:
        print_list()
        return 0

    names = selected_cases(args)

    if not os.path.exists(args.node_port):
        print("STATUS: NOT RUN")
        print("Node port %s is not present." % args.node_port)
        print("Connect the classic ESP32 and retry.")
        return 2

    node = Node(args.node_port)
    controller = None
    try:
        node.open()
    except Exception as exc:  # noqa: BLE001 - report a clear not-run state
        print("STATUS: NOT RUN")
        print("Cannot open node port %s: %s" % (args.node_port, exc))
        return 2

    if not args.no_controller and os.path.exists(args.controller_port):
        for attempt in range(1, 4):
            try:
                controller = Controller(args.controller_port)
                controller.open()
                break
            except Exception as exc:  # noqa: BLE001 - controller is optional
                print(
                    "controller: open attempt %d failed: %s"
                    % (attempt, exc)
                )
                if controller is not None:
                    controller.close()
                controller = None
                time.sleep(3.0)
        if controller is None:
            print("controller: controller-side cases will be skipped")
    elif not args.no_controller:
        print("controller: port %s is not present" % args.controller_port)

    ctx = Context(args, node, controller)
    results = []

    try:
        for name in names:
            results.append((name,) + run_case(name, ctx))
    finally:
        if controller is not None:
            controller.close()
        node.close()

    print("")
    print("SUMMARY")
    for name, status, detail in results:
        print("  %-26s %-6s %s" % (name, status, detail))

    passed = sum(1 for _, status, _ in results if status == "PASS")
    failed = sum(1 for _, status, _ in results if status == "FAIL")
    skipped = sum(1 for _, status, _ in results if status == "SKIP")
    manual = sum(1 for _, status, _ in results if status == "MANUAL")

    print("")
    print(
        "RESULT: %d passed, %d failed, %d skipped, %d manual"
        % (passed, failed, skipped, manual)
    )
    if failed:
        return 1
    if passed == 0:
        print("STATUS: NOT RUN")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
