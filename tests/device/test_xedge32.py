#!/usr/bin/env python3
import argparse
import os
import re
import serial
import sys
import time
import urllib.error
import urllib.request

ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\[5n|\x00")
IP_RE = re.compile(r"\bip:\s*(\d{1,3}(?:\.\d{1,3}){3})")
MARK_RE = re.compile(r"(?m)^TEST\|([^|\r\n]+)\|(OK|FAIL)(?:\|([^\r\n]*))?")


class Shell:
    def __init__(self, port):
        self.port = port
        self.ser = None
        self.sync_output = ""

    def open(self, timeout=10.0):
        deadline = time.time() + timeout
        while True:
            try:
                self.ser = serial.Serial(self.port, 115200, timeout=0.1)
                self.ser.dtr = False
                self.ser.rts = False
                self.ser.reset_input_buffer()
                break
            except serial.SerialException:
                if time.time() >= deadline:
                    raise
                time.sleep(0.5)
        self.ser.write(b"\n")
        self.ser.flush()
        boot = self.read_prompt(timeout)
        if not self.sync(8.0):
            raise RuntimeError("LuaShell32 did not respond")
        return boot + self.sync_output

    def sync(self, timeout=8.0):
        end = time.time() + timeout
        self.sync_output = ""
        while time.time() < end:
            out = self.send('print("SHELL_SYNC")', timeout=1.5)
            self.sync_output += out
            if re.search(r"(?m)^SHELL_SYNC$", out):
                return True
            time.sleep(0.3)
        return False

    def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None

    @staticmethod
    def clean(data):
        text = ANSI.sub(b"", data).decode("utf-8", "replace")
        return text.replace("\r", "")

    def read_prompt(self, timeout):
        data = bytearray()
        end = time.time() + timeout
        while time.time() < end:
            chunk = self.ser.read(4096)
            if chunk:
                data.extend(chunk)
                text = self.clean(data)
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if lines and lines[-1] == ">":
                    return text
        return self.clean(data)

    def send(self, code, timeout=8.0):
        self.ser.write(code.encode("utf-8") + b"\n")
        self.ser.flush()
        return self.read_prompt(timeout)

    def drain(self, seconds):
        data = bytearray()
        end = time.time() + seconds
        while time.time() < end:
            chunk = self.ser.read(4096)
            if chunk:
                data.extend(chunk)
        return self.clean(data)

    def reboot(self):
        self.ser.write(b'esp32.execute("restart")\n')
        self.ser.flush()
        time.sleep(0.3)
        self.close()


def lua_marker(shell, code, timeout=8.0):
    out = shell.send(code, timeout)
    match = MARK_RE.search(out)
    return match, out


def http_status(host, path):
    request = urllib.request.Request(
        "http://%s%s" % (host, path),
        headers={"Connection": "close"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = None
    for _ in range(30):
        try:
            with opener.open(request, timeout=15) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
            time.sleep(1.0)
    raise last_error


def reboot_and_get_ip(shell):
    shell.reboot()
    time.sleep(2.0)
    boot = shell.open(20.0)
    end = time.time() + 20.0
    while time.time() < end and not IP_RE.search(boot):
        boot += shell.drain(1.0)
    shell.sync(8.0)
    time.sleep(1.0)
    match = IP_RE.search(boot)
    return (match.group(1) if match else None), boot + shell.sync_output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--password-env", default="XEDGE_WIFI_PASS")
    args = parser.parse_args()

    password = os.environ.get(args.password_env, "")
    shell = Shell(args.port)
    results = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        print("%s %-16s %s" % ("PASS" if ok else "FAIL", name, detail))

    try:
        shell.open()
        match, _ = lua_marker(
            shell,
            'local a,b,c=ba.version();print("TEST|version|OK|"..a.."|"..b.."|"..tostring(c))',
        )
        record("firmware", bool(match), match.group(3) if match else "no marker")

        match, _ = lua_marker(
            shell,
            'local m=esp32.mac();print("TEST|mac|OK|"..(m:gsub(".",function(x) return string.format("%02X",string.byte(x)) end)))',
        )
        record("mac", bool(match), match.group(3) if match else "no marker")

        match, _ = lua_marker(
            shell,
            'p=esp32.gpio(4,"OUT");p:value(true);p:value(false);p:close();print("TEST|gpio|OK")',
        )
        record("gpio", bool(match), "")

        match, _ = lua_marker(
            shell,
            'dio=ba.openio"disk";f=assert(dio:open("selftest.txt","w"));f:write("xedge32");f:close()',
        )
        if match:
            record("filesystem", False, "unexpected marker")
        else:
            match, _ = lua_marker(
                shell,
                'g=assert(dio:open("selftest.txt"));d=g:read"*a";g:close();dio:remove"selftest.txt";print("TEST|fs|"..(d=="xedge32" and "OK" or "FAIL").."|"..d)',
            )
            record("filesystem", bool(match and match.group(2) == "OK"), match.group(3) if match else "no marker")

        match, _ = lua_marker(
            shell,
            'local m=ba.mallinfo();print("TEST|mem|OK|"..m.uordblks.."|"..m.fordblks)',
        )
        record("memory", bool(match), match.group(3) if match else "no marker")

        started = time.time()
        match, _ = lua_marker(shell, 'ba.sleep(250);print("TEST|sleep|OK")', timeout=8.0)
        elapsed = time.time() - started
        record("sleep", bool(match) and elapsed >= 0.2, "%.2fs" % elapsed)

        wifi_code = (
            'local a=esp32.apinfo();print("TEST|wifi|"..(a and a.ssid=="%s" and "OK" or "FAIL")'
            '.."|"..tostring(a and a.ssid or "nil").."|"..tostring(a and a.rssi or 0))'
        ) % args.ssid.replace("\\", "\\\\").replace('"', '\\"')
        match, _ = lua_marker(shell, wifi_code)
        if not match or match.group(2) != "OK":
            if not password:
                record("wifi", False, "%s is not set" % args.password_env)
            else:
                command = 'esp32.netconnect("wifi",{ssid="%s",pwd="%s"})' % (
                    args.ssid.replace("\\", "\\\\").replace('"', '\\"'),
                    password.replace("\\", "\\\\").replace('"', '\\"'),
                )
                shell.send(command, timeout=5.0)
                shell.drain(12.0)
                match, _ = lua_marker(shell, wifi_code)
        record("wifi", bool(match and match.group(2) == "OK"), match.group(3) if match else "no marker")

        ip, boot = reboot_and_get_ip(shell)
        match, _ = lua_marker(shell, wifi_code)
        record(
            "wifi_after_reboot",
            bool(ip and match and match.group(2) == "OK"),
            "ip=%s ssid=%s" % (ip, match.group(3) if match else "?"),
        )

        if ip:
            status, body = http_status(ip, "/")
            record("http_root", status == 404 and b"Xedge" in body, "status=%d" % status)
            status, body = http_status(ip, "/rtl/")
            record("http_ide", status == 200 and b"<title>Xedge" in body, "status=%d bytes=%d" % (status, len(body)))
        else:
            record("http_root", False, "no IP after reboot")
            record("http_ide", False, "no IP after reboot")
    finally:
        shell.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\n%d passed, %d failed" % (len(results) - len(failed), len(failed)))
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
