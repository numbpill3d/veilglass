#!/usr/bin/env python3
"""airwatch: the root-side ear for veilglass.

Opens a Wi-Fi monitor interface and reads only radiotap and 802.11 headers
(who transmitted, how loud, on which channel, which network a probe asks for).
Frame bodies are never read. Sightings go to veilglass over localhost UDP.

  sudo ./airwatch.py              share mode: stay connected, hear your own channel
  sudo ./airwatch.py --hop        hop mode: drop the connection, sweep all channels
"""
import argparse
import json
import signal
import socket
import struct
import subprocess
import sys
import time

MON = "veilmon"
HOP_CHANNELS = [1, 6, 11, 36, 2, 7, 44, 3, 8, 149, 4, 9, 157, 5, 10, 40, 48, 153, 161]
# radiotap fields that precede the signal byte: bit -> (size, alignment)
RT_FIELDS = {0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (4, 2), 4: (2, 2), 5: (1, 1)}


def mac(b):
    return ":".join(f"{x:02X}" for x in b)


def chan_of(mhz):
    if mhz == 2484:
        return 14
    return (mhz - 2407) // 5 if mhz < 2484 else (mhz - 5000) // 5


def parse(pkt):
    """Return a sighting dict from one monitor-mode frame, or None."""
    if len(pkt) < 8 or pkt[0] != 0:
        return None
    rt_len, present = struct.unpack_from("<HI", pkt, 2)
    off, word = 8, present
    while word & 0x80000000:  # extended present bitmaps
        if off + 4 > len(pkt):
            return None
        word = struct.unpack_from("<I", pkt, off)[0]
        off += 4
    rssi = mhz = None
    for bit in range(6):
        if not present & (1 << bit):
            continue
        size, align = RT_FIELDS[bit]
        off = (off + align - 1) & ~(align - 1)
        if off + size > rt_len:
            return None
        if bit == 1 and pkt[off] & 0x40:  # failed checksum: addresses are noise
            return None
        if bit == 3:
            mhz = struct.unpack_from("<H", pkt, off)[0]
        if bit == 5:
            rssi = struct.unpack_from("<b", pkt, off)[0]
        off += size
    f = pkt[rt_len:]
    if rssi is None or len(f) < 16:
        return None
    ftype, sub = (f[0] >> 2) & 3, f[0] >> 4
    to_ds, from_ds = f[1] & 1, f[1] & 2
    a1, a2 = f[4:10], f[10:16]
    if a2[0] & 1:  # a transmitter is never a group address
        return None
    out = {"addr": mac(a2), "rssi": rssi, "chan": chan_of(mhz) if mhz else None}
    if ftype == 0 and len(f) >= 24:
        if sub in (8, 5):  # beacon, probe response: an access point
            out.update(role="ap", addr=mac(f[16:22]), ssid=ssid_of(f, 36))
        elif sub == 4:     # probe request: a device calling out
            out.update(role="seeker", ssid=ssid_of(f, 24))
        elif sub in (0, 2, 11):  # association, reassociation, authentication
            out.update(role="client", bssid=mac(a1))
        else:
            return None
    elif ftype == 2:
        if to_ds and not from_ds:
            out.update(role="client", bssid=mac(a1))
        elif from_ds and not to_ds:
            out.update(role="ap")
        else:
            return None
    else:
        return None
    return out


def ssid_of(f, off):
    """First tagged parameter is the SSID when present."""
    if off + 2 <= len(f) and f[off] == 0:
        n = f[off + 1]
        raw = f[off + 2:off + 2 + n]
        if len(raw) == n and any(raw):
            return raw.decode("utf-8", "replace")
    return ""


def run(*cmd, check=False):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iface", default="wlan0", help="your normal wifi interface")
    ap.add_argument("--hop", action="store_true",
                    help="sweep channels; disconnects wifi until you stop")
    ap.add_argument("--dwell", type=float, default=0.35, help="seconds per channel when hopping")
    ap.add_argument("--port", type=int, default=7734)
    args = ap.parse_args()

    def bail(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, bail)

    try:
        run("iw", "dev", MON, "del")
        made = run("iw", "dev", args.iface, "interface", "add", MON, "type", "monitor")
        if made.returncode:
            sys.exit(f"could not open a monitor interface: {made.stderr.strip()} (run with sudo)")
        if args.hop:
            run("nmcli", "dev", "set", args.iface, "managed", "no")
            run("ip", "link", "set", args.iface, "down")
        up = run("ip", "link", "set", MON, "up")
        if up.returncode:
            sys.exit(f"could not raise {MON}: {up.stderr.strip()}")
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((MON, 0))
        sock.settimeout(0.1)
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        print(f"airwatch listening on {MON} ({'hopping' if args.hop else 'sharing ' + args.iface + chr(39) + 's channel'}); ctrl-c to stop")

        seen, hop_i, hop_at, flush_at = {}, 0, 0.0, time.time() + 1
        while True:
            now = time.time()
            if args.hop and now >= hop_at:
                run("iw", "dev", MON, "set", "channel", str(HOP_CHANNELS[hop_i % len(HOP_CHANNELS)]))
                hop_i, hop_at = hop_i + 1, now + args.dwell
            try:
                s = parse(sock.recv(4096))
            except socket.timeout:
                s = None
            if s:
                key = (s["role"] == "ap", s["addr"])
                old = seen.get(key)
                if old:  # keep the loudest reading and anything newly learned
                    s["rssi"] = max(s["rssi"], old["rssi"])
                    for k in ("ssid", "bssid"):
                        s[k] = s.get(k) or old.get(k)
                    if old["role"] == "client":
                        s["role"] = "client" if s["role"] != "ap" else s["role"]
                seen[key] = s
            if now >= flush_at:
                batch = list(seen.values())
                for i in range(0, max(len(batch), 1), 40):
                    out.sendto(json.dumps(batch[i:i + 40]).encode(), ("127.0.0.1", args.port))
                seen, flush_at = {}, now + 1
    except KeyboardInterrupt:
        pass
    finally:
        run("iw", "dev", MON, "del")
        if args.hop:
            run("ip", "link", "set", args.iface, "up")
            run("nmcli", "dev", "set", args.iface, "managed", "yes")
        print("airwatch closed; wifi restored")


if __name__ == "__main__":
    main()
