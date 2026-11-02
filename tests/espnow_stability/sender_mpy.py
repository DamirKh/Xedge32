import network
import espnow
import time
import struct
import ubinascii

MAGIC = b"XNW1"
BCAST = b"\xff\xff\xff\xff\xff\xff"
PEER = ubinascii.unhexlify(PEER_MAC) if PEER_MAC else BCAST


def machex(mac):
    return ubinascii.hexlify(mac).decode()


def wlan_reset():
    sta = network.WLAN(network.WLAN.IF_STA)
    ap = network.WLAN(network.WLAN.IF_AP)
    sta.active(False)
    ap.active(False)
    sta.active(True)
    try:
        sta.disconnect()
    except OSError:
        pass
    try:
        sta.config(reconnects=0)
    except (OSError, ValueError):
        pass
    try:
        sta.config(pm=sta.PM_NONE)
    except (OSError, ValueError):
        pass
    return sta


def main():
    sta = wlan_reset()
    sta.config(channel=CHANNEL)
    e = espnow.ESPNow()
    e.active(True)
    try:
        e.add_peer(PEER)
    except OSError as err:
        if len(err.args) < 2 or err.args[1] != "ESP_ERR_ESPNOW_EXIST":
            raise

    msg = bytearray(PAYLOAD_LEN)
    msg[0:4] = MAGIC
    sent = 0
    errs = 0
    start = time.ticks_ms()
    next_send = start
    last_report = start
    print("SENDER|READY|ch=%d|mac=%s|peer=%s|max=%d" % (
        sta.config("channel"), machex(sta.config("mac")), machex(PEER), espnow.MAX_DATA_LEN))

    while True:
        now = time.ticks_ms()
        if DURATION_MS > 0 and time.ticks_diff(now, start) >= DURATION_MS:
            break
        if COUNT > 0 and sent >= COUNT:
            break

        struct.pack_into("<I", msg, 4, sent + 1)
        struct.pack_into("<I", msg, 8, now)
        try:
            if e.send(PEER, msg, False):
                sent += 1
            else:
                errs += 1
        except OSError as err:
            errs += 1
            if errs <= 5:
                print("SENDER|ERR|%s" % (err,))

        if time.ticks_diff(now, last_report) >= 1000:
            print("SENDER|PROG|sent=%d|err=%d|ms=%d" % (
                sent, errs, time.ticks_diff(now, start)))
            last_report = now

        next_send = time.ticks_add(next_send, INTERVAL_MS)
        delay = time.ticks_diff(next_send, time.ticks_ms())
        if delay > 0:
            time.sleep_ms(delay)

    elapsed = time.ticks_diff(time.ticks_ms(), start)
    tx = e.stats()
    print("SENDER|DONE|sent=%d|err=%d|ms=%d|tx=%d|fail=%d" % (
        sent, errs, elapsed, tx[0], tx[2]))
    e.active(False)
    sta.active(False)


try:
    main()
except KeyboardInterrupt:
    print("SENDER|ABORT")
