#!/usr/bin/env python3
"""Autonomous ESP-NOW stability test for Xedge32 + MicroPython.

The Xedge32 board runs the receive-side Lua code in receiver.lua.  The second
board runs MicroPython and is controlled through its raw REPL; no mpremote
installation is required.
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
import time

import serial
from serial.tools import list_ports

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "device"))
from test_xedge32 import Shell

LUA_FILE = os.path.join(HERE, "receiver.lua")
SENDER_FILE = os.path.join(HERE, "sender_mpy.py")

MAC_RE = re.compile(r"(?m)^MAC\s+([0-9a-fA-F]{12})")
CHAN_RE = re.compile(r"(?m)^CHAN\s+(\d+)")
MPY_RE = re.compile(r"MPY\s+([^\r\n]+)")
MAX_RE = re.compile(r"MAX\s+(\d+)")


def lua_send(shell, code, timeout=1.5):
    out = shell.send(code, timeout)
    if "Lua error" in out:
        raise RuntimeError("Lua error: %s" % out.strip())
    return out


def load_receiver(shell, payload_len):
    lua_send(shell, "if rx then pcall(function() rx:close() end) end rx=nil", 1.0)
    with open(LUA_FILE, "r", encoding="utf-8") as src:
        for lineno, raw in enumerate(src, 1):
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            if len(line) > 220:
                raise RuntimeError(
                    "receiver.lua:%d is too long for LuaShell32 (%d chars)"
                    % (lineno, len(line))
                )
            lua_send(shell, line, 1.0)
    lua_send(shell, "st.plen=%d" % payload_len, 1.0)


def parse_stats(out):
    pending = None
    reports = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[0] == "STAT":
            pending = {
                "rx": int(parts[2]),
                "uniq": int(parts[3]),
                "dup": int(parts[4]),
                "oor": int(parts[5]),
                "maxgap": int(parts[6]),
            }
        elif len(parts) >= 9 and parts[0] == "STAT2" and pending is not None:
            pending.update(
                {
                    "rssi_min": int(parts[1]),
                    "rssi_avg": float(parts[2]),
                    "rssi_max": int(parts[3]),
                    "bad": int(parts[4]),
                    "lenbad": int(parts[5]),
                    "chan": int(parts[6]),
                    "chanchg": int(parts[7]),
                    "drops": int(parts[8]),
                }
            )
            reports.append(pending)
            pending = None
    if not reports:
        raise RuntimeError("no STAT report in output: %r" % out[-500:])
    return reports[-1]


def query_stats(shell, tag, timeout=1.2, attempts=3):
    last_error = None
    for _ in range(attempts):
        out = shell.send("rxreport('%s')" % tag, timeout)
        try:
            return parse_stats(out)
        except RuntimeError as exc:
            last_error = exc
            time.sleep(0.2)
    raise last_error


def query_marker(shell, code, regex, timeout=1.5, attempts=4):
    last = ""
    for _ in range(attempts):
        out = lua_send(shell, code, timeout)
        match = regex.search(out)
        if match:
            return match
        last = out
        time.sleep(0.4)
    raise RuntimeError(
        "expected %r in shell output, last output: %r"
        % (regex.pattern, last[-500:])
    )


def open_receiver(shell, payload_len):
    load_receiver(shell, payload_len)
    out = lua_send(shell, 'print("LOADED",type(onRx),type(st))', 1.5)
    if not re.search(r"(?m)^LOADED\s+function\s+table", out):
        raise RuntimeError("receiver Lua did not load: %r" % out[-500:])
    lua_send(shell, "rx=assert(esp32.espnow{callback=onRx})", 3.0)
    match = query_marker(
        shell,
        'local m=esp32.mac():gsub(".",function(c)'
        'return string.format("%02x",c:byte())end);print("MAC",m)',
        MAC_RE,
    )
    mac = match.group(1)
    match = query_marker(
        shell,
        'print("CHAN",rx:info().channel)',
        CHAN_RE,
    )
    return {"mac": mac, "channel": int(match.group(1))}


def close_receiver(shell):
    try:
        shell.send("if rx then pcall(function() rx:close() end) rx=nil end", 1.0)
    except Exception:
        pass


def clean_mpy(data):
    return data.replace(b"\r", b"").replace(b"\x04", b"").decode("utf-8", "replace")


class MicroPython:
    def __init__(self, port, baud=115200):
        self.port = port
        self.ser = serial.Serial(port, baud, timeout=0.1)
        try:
            self.ser.dtr = False
            self.ser.rts = False
        except OSError:
            pass
        self.ser.reset_input_buffer()

    def drain(self, seconds):
        data = b""
        end = time.time() + seconds
        while time.time() < end:
            chunk = self.ser.read(4096)
            if chunk:
                data += chunk
            else:
                time.sleep(0.02)
        return data

    def wait_for(self, token, timeout):
        data = b""
        end = time.time() + timeout
        while time.time() < end:
            chunk = self.ser.read(4096)
            if chunk:
                data += chunk
                if token in data:
                    return data
            else:
                time.sleep(0.02)
        raise TimeoutError(
            "MicroPython did not emit %r; last output: %r"
            % (token, clean_mpy(data[-500:]))
        )

    def enter_raw(self):
        self.ser.write(b"\r\x03\x03")
        time.sleep(0.25)
        self.drain(0.25)
        self.ser.write(b"\x02")
        time.sleep(0.15)
        self.drain(0.2)
        self.ser.write(b"\x01")
        self.wait_for(b"raw REPL", 5.0)

    def exec_collect(self, code, timeout):
        self.ser.write(code.encode("utf-8") + b"\x04")
        return self.wait_for(b"\x04", timeout)

    def start(self, code):
        self.ser.reset_input_buffer()
        self.ser.write(code.encode("utf-8") + b"\x04")
        return self.wait_for(b"OK", 8.0)

    def stop(self):
        try:
            self.ser.write(b"\x03")
            time.sleep(0.25)
            self.drain(0.4)
            self.ser.write(b"\x02")
            time.sleep(0.15)
            self.drain(0.2)
        except Exception:
            pass

    def close(self):
        self.stop()
        self.ser.close()

    def preflight(self, payload_len):
        code = (
            "import sys\n"
            "import espnow\n"
            "print('MPY', sys.version)\n"
            "print('MAX', espnow.MAX_DATA_LEN)\n"
        )
        out = clean_mpy(self.exec_collect(code, 6.0))
        mpy = MPY_RE.search(out)
        maximum = MAX_RE.search(out)
        if not mpy or not maximum:
            raise RuntimeError("MicroPython preflight failed: %r" % out[-500:])
        if int(maximum.group(1)) < payload_len:
            raise RuntimeError(
                "MicroPython ESP-NOW max payload is %s, requested %d"
                % (maximum.group(1), payload_len)
            )
        return {
            "version": mpy.group(1).strip(),
            "max_payload": int(maximum.group(1)),
        }


def build_sender_code(args, channel, peer_mac):
    with open(SENDER_FILE, "r", encoding="utf-8") as src:
        body = src.read()
    duration_ms = int(args.duration * 1000) if args.duration > 0 else 0
    header = (
        "CHANNEL=%d\n"
        "DURATION_MS=%d\n"
        "COUNT=%d\n"
        "INTERVAL_MS=%d\n"
        "PAYLOAD_LEN=%d\n"
        "PEER_MAC=%r\n"
        % (channel, duration_ms, args.count, args.interval_ms, args.payload_len,
           peer_mac or "")
    )
    return header + body


def parse_sender_line(line, sender):
    line = line.strip()
    if line.startswith("OKSENDER|"):
        line = line[2:]
    if not line.startswith("SENDER|"):
        return False
    parts = line.split("|")
    kind = parts[1] if len(parts) > 1 else ""
    fields = {}
    for part in parts[2:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    if kind == "READY":
        sender["channel"] = int(fields.get("ch", "0"))
        sender["mac"] = fields.get("mac", "")
        sender["peer"] = fields.get("peer", "")
        sender["max_payload"] = int(fields.get("max", "0"))
        print("sender ready: channel=%s mac=%s peer=%s max=%s"
              % (sender["channel"], sender["mac"], sender["peer"],
                 sender["max_payload"]))
    elif kind == "PROG":
        sender["sent"] = int(fields.get("sent", "0"))
        sender["err"] = int(fields.get("err", "0"))
        sender["elapsed_ms"] = int(fields.get("ms", "0"))
        print("progress: sent=%d err=%d elapsed=%.1fs"
              % (sender["sent"], sender["err"], sender["elapsed_ms"] / 1000.0))
    elif kind == "ERR":
        print("sender error: %s" % line)
    elif kind == "DONE":
        sender["sent"] = int(fields.get("sent", "0"))
        sender["err"] = int(fields.get("err", "0"))
        sender["elapsed_ms"] = int(fields.get("ms", "0"))
        sender["tx"] = int(fields.get("tx", "0"))
        sender["fail"] = int(fields.get("fail", "0"))
        sender["done"] = True
    elif kind == "ABORT":
        sender["aborted"] = True
        sender["done"] = True
    return sender.get("done", False)


def max_runtime(args):
    if args.duration > 0:
        return args.duration + 60.0
    return (args.count * args.interval_ms / 1000.0) + 60.0


def write_csv(path, history):
    if not path or not history:
        return
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    keys = [
        "t", "sent", "rx", "uniq", "dup", "oor", "maxgap",
        "rssi_min", "rssi_avg", "rssi_max", "bad", "lenbad",
        "chan", "chanchg", "drops",
    ]
    with open(path, "w", newline="", encoding="utf-8") as dst:
        writer = csv.DictWriter(dst, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(history)


def write_json(path, report):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as dst:
        json.dump(report, dst, indent=2, ensure_ascii=False)
        dst.write("\n")


def summarize(args, receiver, sender, final, history, errors, started, finished):
    sent = int(sender.get("sent", 0))
    unique = int(final.get("uniq", 0)) if final else 0
    received = int(final.get("rx", 0)) if final else 0
    lost = max(0, sent - unique)
    loss_pct = (100.0 * lost / sent) if sent else 0.0
    duplicate = int(final.get("dup", 0)) if final else 0
    out_of_order = int(final.get("oor", 0)) if final else 0
    max_gap = int(final.get("maxgap", 0)) if final else 0
    drops = int(final.get("drops", 0)) if final else 0
    bad = int(final.get("bad", 0)) if final else 0
    lenbad = int(final.get("lenbad", 0)) if final else 0
    chanchg = int(final.get("chanchg", 0)) if final else 0

    checks = []
    if sent <= 0:
        checks.append("sender sent no packets")
    if final is None:
        checks.append("no final receiver report")
    if sender.get("aborted"):
        checks.append("sender aborted")
    if loss_pct > args.max_loss_pct:
        checks.append("loss %.3f%% > %.3f%%" % (loss_pct, args.max_loss_pct))
    if duplicate > args.max_dups:
        checks.append("duplicates %d > %d" % (duplicate, args.max_dups))
    if drops > args.max_drops:
        checks.append("receiver drops %d > %d" % (drops, args.max_drops))
    if bad:
        checks.append("bad magic packets: %d" % bad)
    if lenbad:
        checks.append("wrong payload length: %d" % lenbad)
    if chanchg and not args.allow_channel_change:
        checks.append("receiver channel changed %d time(s)" % chanchg)
    errors.extend(checks)
    result = "PASS" if not errors else "FAIL"

    report = {
        "started": started,
        "finished": finished,
        "result": result,
        "errors": errors,
        "config": {
            "duration_s": args.duration,
            "count": args.count,
            "interval_ms": args.interval_ms,
            "payload_len": args.payload_len,
            "max_loss_pct": args.max_loss_pct,
            "max_drops": args.max_drops,
            "max_dups": args.max_dups,
            "allow_channel_change": args.allow_channel_change,
            "broadcast": args.broadcast,
        },
        "receiver": receiver,
        "sender": sender,
        "stats": {
            "sent": sent,
            "received": received,
            "unique": unique,
            "lost": lost,
            "loss_pct": round(loss_pct, 4),
            "duplicates": duplicate,
            "out_of_order": out_of_order,
            "max_gap": max_gap,
            "drops": drops,
            "bad_magic": bad,
            "wrong_len": lenbad,
            "channel_changes": chanchg,
            "rssi_min": final.get("rssi_min") if final else None,
            "rssi_avg": final.get("rssi_avg") if final else None,
            "rssi_max": final.get("rssi_max") if final else None,
            "channel": final.get("chan") if final else None,
        },
        "history": history,
    }

    print("")
    print("ESP-NOW stability report")
    print("receiver: mac=%s channel=%s" % (receiver["mac"], receiver["channel"]))
    print("sender:   mac=%s channel=%s" % (
        sender.get("mac", "?"), sender.get("channel", "?")))
    print("sent=%d unique=%d received=%d lost=%d loss=%.3f%%"
          % (sent, unique, received, lost, loss_pct))
    print("duplicates=%d out_of_order=%d max_gap=%d drops=%d bad=%d lenbad=%d"
          % (duplicate, out_of_order, max_gap, drops, bad, lenbad))
    if final:
        print("rssi min=%.1f avg=%.1f max=%.1f channel=%d channel_changes=%d"
              % (final["rssi_min"], final["rssi_avg"], final["rssi_max"],
                 final["chan"], chanchg))
    print("RESULT: %s" % result)
    if errors:
        print("errors: " + "; ".join(errors))
    return report, 0 if result == "PASS" else 1


def run_self_test(shell, receiver, args):
    print("self-test: loading receiver Lua")
    print("self-test: receiver mac=%s channel=%s" % (receiver["mac"], receiver["channel"]))
    lua_send(
        shell,
        'function rxbytes(q) return string.char(q%256,math.floor(q/256)%256,'
        'math.floor(q/65536)%256,math.floor(q/16777216)%256) end',
        1.0,
    )
    lua_send(
        shell,
        'function rxfake(q) local d="XNW1"..rxbytes(q)..string.rep("x",st.plen-8);'
        'onRx(d,string.rep("s",6),{rssi=-40,channel=6,len=st.plen}) end',
        1.0,
    )
    for q in (1, 2, 4, 3, 4, 5, 7):
        lua_send(shell, "rxfake(%d)" % q, 0.6)
    lua_send(
        shell,
        'onRx("BAD!"..string.rep("x",st.plen-4),string.rep("s",6),'
        '{rssi=-40,channel=6,len=st.plen})',
        0.6,
    )
    lua_send(
        shell,
        'onRx("XNW1"..rxbytes(8)..string.rep("x",st.plen-8),'
        'string.rep("s",6),{rssi=-40,channel=6,len=st.plen-1})',
        0.6,
    )
    stats = query_stats(shell, "selftest")
    expected = {
        "rx": 8,
        "uniq": 7,
        "dup": 1,
        "oor": 1,
        "maxgap": 1,
        "bad": 1,
        "lenbad": 1,
        "chan": 6,
        "chanchg": 0,
        "drops": 0,
    }
    errors = []
    for key, want in expected.items():
        got = stats.get(key)
        if got != want:
            errors.append("%s=%r expected %r" % (key, got, want))
    close_receiver(shell)
    print("self-test stats: %s" % json.dumps(stats, sort_keys=True))
    if errors:
        print("SELF-TEST FAIL: " + "; ".join(errors))
        return 1
    print("SELF-TEST PASS")
    return 0


def run_live(shell, args, receiver):
    sender = {}
    history = []
    errors = []
    final = None
    started = datetime.datetime.now().isoformat(timespec="seconds")
    mpy = None
    try:
        mpy = MicroPython(args.sender_port)
        print("connecting to MicroPython on %s" % args.sender_port)
        mpy.enter_raw()
        info = mpy.preflight(args.payload_len)
        sender.update(info)
        print("MicroPython: %s" % info["version"])
        code = build_sender_code(
            args, receiver["channel"], "" if args.broadcast else receiver["mac"]
        )
        print("starting sender: %d pkt/s payload=%d channel=%d"
              % (1000 // max(1, args.interval_ms), args.payload_len,
                 receiver["channel"]))
        prefix = clean_mpy(mpy.start(code))
        start = time.time()
        next_poll = start
        lines = prefix.split("\n")
        mpy_buf = lines.pop()
        for line in lines:
            parse_sender_line(line.strip(), sender)
        while True:
            if sender.get("done"):
                break
            chunk = mpy.ser.read(4096)
            if chunk:
                mpy_buf += clean_mpy(chunk)
                lines = mpy_buf.split("\n")
                mpy_buf = lines.pop()
                for line in lines:
                    if parse_sender_line(line.strip(), sender):
                        break
            if sender.get("done"):
                break

            now = time.time()
            if now >= next_poll:
                stats = query_stats(shell, "p")
                chan_out = lua_send(shell, 'print("CHAN",rx:info().channel)', 1.0)
                chan_match = CHAN_RE.search(chan_out)
                current_channel = (
                    int(chan_match.group(1)) if chan_match else receiver["channel"]
                )
                row = {"t": round(now - start, 3), "sent": sender.get("sent", 0)}
                row.update(stats)
                history.append(row)
                print("live: sent=%d rx=%d uniq=%d lost=%d dup=%d drops=%d ch=%d"
                      % (sender.get("sent", 0), stats["rx"], stats["uniq"],
                         max(0, sender.get("sent", 0) - stats["uniq"]),
                         stats["dup"], stats["drops"], current_channel))
                if current_channel != receiver["channel"]:
                    if not args.allow_channel_change:
                        errors.append(
                            "receiver channel changed from %d to %d during test"
                            % (receiver["channel"], current_channel)
                        )
                        break
                next_poll = now + args.poll_seconds

            if now - start > max_runtime(args):
                errors.append("timeout waiting for sender")
                break
            time.sleep(0.02)
    except Exception as exc:
        errors.append("live test error: %s" % exc)
        print("live test error: %s" % exc)
    finally:
        if mpy:
            mpy.close()
        try:
            time.sleep(max(0.0, args.settle_ms / 1000.0))
            final = query_stats(shell, "final")
        except Exception as exc:
            errors.append("could not read final receiver stats: %s" % exc)
            print("could not read final receiver stats: %s" % exc)
        close_receiver(shell)

    finished = datetime.datetime.now().isoformat(timespec="seconds")
    report, rc = summarize(args, receiver, sender, final, history, errors, started, finished)
    write_json(args.json, report)
    write_csv(args.csv, history)
    print("json: %s" % args.json)
    if args.csv:
        print("csv:  %s" % args.csv)
    return rc


def auto_sender_port(receiver_port):
    ports = [p.device for p in list_ports.comports()]
    candidates = [p for p in ports if p != receiver_port]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        "cannot choose MicroPython port from %r; use --sender-port" % (ports,)
    )


def open_shell(args):
    shell = Shell(args.receiver_port)
    last_error = None
    for attempt in range(1, 4):
        try:
            shell.open(args.receiver_timeout)
            time.sleep(1.0)
            if not shell.sync(args.receiver_timeout):
                raise RuntimeError("LuaShell32 did not respond after open")
            return shell
        except Exception as exc:
            last_error = exc
            shell.close()
            if attempt < 3:
                print("receiver open attempt %d failed: %s; retrying"
                      % (attempt, exc))
                time.sleep(3.0)
    raise last_error


def prepare_receiver(args):
    last_error = None
    for attempt in range(1, 4):
        shell = open_shell(args)
        try:
            receiver = open_receiver(shell, args.payload_len)
            return shell, receiver
        except Exception as exc:
            last_error = exc
            print("receiver setup attempt %d failed: %s; retrying"
                  % (attempt, exc))
            close_receiver(shell)
            shell.close()
            time.sleep(3.0)
    raise last_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receiver-port", default="/dev/ttyACM0")
    parser.add_argument("--sender-port")
    parser.add_argument("--duration", type=float, default=None,
                        help="seconds; default 60, or 0 when --count is used")
    parser.add_argument("--count", type=int, default=0,
                        help="stop after this many sends; 0 means use --duration")
    parser.add_argument("--interval-ms", type=int, default=20)
    parser.add_argument("--payload-len", type=int, default=32)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--settle-ms", type=int, default=700)
    parser.add_argument("--max-loss-pct", type=float, default=1.0)
    parser.add_argument("--max-drops", type=int, default=0)
    parser.add_argument("--max-dups", type=int, default=0)
    parser.add_argument("--allow-channel-change", action="store_true")
    parser.add_argument("--broadcast", action="store_true",
                        help="send to ff:ff:ff:ff:ff:ff instead of the receiver MAC")
    parser.add_argument("--json", default="build/espnow-stability-report.json")
    parser.add_argument("--csv")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--receiver-timeout", type=float, default=20.0)
    parser.add_argument("--channel-check", type=float, default=1.0)
    args = parser.parse_args()

    if args.duration is None:
        args.duration = 0.0 if args.count > 0 else 60.0
    if args.payload_len < 12 or args.payload_len > 250:
        parser.error("--payload-len must be between 12 and 250")
    if args.interval_ms < 1:
        parser.error("--interval-ms must be at least 1")
    if not args.self_test and args.duration <= 0 and args.count <= 0:
        parser.error("use --duration or --count")
    if not args.self_test and not args.sender_port:
        try:
            args.sender_port = auto_sender_port(args.receiver_port)
        except RuntimeError as exc:
            parser.error(str(exc))

    shell = None
    try:
        shell, receiver = prepare_receiver(args)
        if args.self_test:
            return run_self_test(shell, receiver, args)

        print("receiver mac=%s channel=%d" % (receiver["mac"], receiver["channel"]))
        if args.channel_check > 0:
            time.sleep(args.channel_check)
            out = lua_send(shell, 'print("CHAN",rx:info().channel)', 1.2)
            match = CHAN_RE.search(out)
            now_channel = int(match.group(1)) if match else receiver["channel"]
            if now_channel != receiver["channel"] and not args.allow_channel_change:
                raise RuntimeError(
                    "receiver channel changed from %d to %d during preflight; "
                    "fix the Wi-Fi channel or use --allow-channel-change"
                    % (receiver["channel"], now_channel)
                )
            receiver["channel"] = now_channel
        return run_live(shell, args, receiver)
    except KeyboardInterrupt:
        print("\ninterrupted; receiver will be closed")
        if shell:
            close_receiver(shell)
        return 130
    finally:
        if shell:
            shell.close()


if __name__ == "__main__":
    sys.exit(main())
