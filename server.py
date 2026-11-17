#!/usr/bin/env python3
"""veilglass: a plain report of the radio devices around you.

Listens to Bluetooth LE advertisements and Wi-Fi beacons (plus Wi-Fi client
frames, if airwatch.py runs), works out what each device probably is and says
why, estimates a distance range from signal strength, and streams it all to a
local web page. It never connects to, probes or transmits at any device, and
keeps nothing on disk except the addresses you mark as yours.
"""
import argparse
import asyncio
import hashlib
import json
import re
import shutil
import time
from collections import deque
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).parent
CONF = Path.home() / ".config" / "veilglass"
MINE = CONF / "mine.json"
OLD_MINE = CONF / "familiars.json"  # earlier versions kept the list here

# distance bands, outer bound in feet
BANDS = [6, 20, 60, 1e9]
BAND_TEXT = ["under 6 ft", "6-20 ft", "20-60 ft", "over 60 ft"]
KINDS = {
    "phone": "Phone / tablet", "audio": "Audio", "tracker": "Tracker / Find My",
    "wearable": "Wearable", "computer": "Computer / TV", "beacon": "Beacon",
    "peripheral": "Peripheral", "ap": "Access point", "hotspot": "Hotspot",
    "client": "Wi-Fi client", "prober": "Probing device", "unknown": "Unknown",
}
TTL = {"ble": 40, "wifi": 150, "air": 90}
AIR_PORT = 7734  # airwatch.py sends its sightings here
OUI_FILE = Path("/usr/share/hwdata/oui.txt")
_oui = None
WARMUP_SECONDS = 20  # no events while the first sweep fills the list
LINGER_SECONDS = 600
EVENT_COOLDOWN = 45  # one device may not log more often than this
HISTORY = 90         # seconds of per-device signal history
GLOBAL_HISTORY = 120

# Bluetooth SIG company identifiers (manufacturer data)
COMPANIES = {
    0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google",
    0x0087: "Garmin", 0x009E: "Bose", 0x012D: "Sony", 0x038F: "Xiaomi",
    0x027D: "Huawei", 0x0171: "Amazon", 0x0059: "Nordic Semiconductor",
    0x00C4: "LG", 0x01DA: "Logitech", 0x0067: "GN Audio (Jabra)",
    0x000F: "Broadcom", 0x000A: "Qualcomm", 0x0499: "Ruuvi", 0x0157: "Anhui Huami",
    0x0310: "SGL Italia", 0x02E5: "Espressif", 0x0131: "Cypress", 0x0822: "Adafruit",
    0x0000: "Ericsson", 0x0002: "Intel", 0x001D: "Qualcomm", 0x0046: "MediaTek",
    0x00D2: "Dialog Semiconductor", 0x0078: "Nike", 0x0110: "Nippon Seiki",
    0x01AB: "Meta (Facebook)", 0x058E: "Meta Platforms", 0x0397: "LEGO",
    0x0961: "Tile", 0x08A4: "Govee", 0x03DA: "Amazfit", 0x0D3F: "Withings",
}
COMPANY_KIND = {
    0x0006: ("computer", "Microsoft beacon, sent by Windows PCs"),
    0x0087: ("wearable", "Garmin manufacturer data"),
    0x009E: ("audio", "Bose manufacturer data"),
    0x0067: ("audio", "Jabra manufacturer data"),
    0x01DA: ("peripheral", "Logitech manufacturer data"),
    0x0157: ("wearable", "Huami (Amazfit / Mi Band) manufacturer data"),
    0x03DA: ("wearable", "Amazfit manufacturer data"),
    0x0961: ("tracker", "Tile manufacturer data"),
}
# Apple continuity message types: type -> (kind, what it is)
APPLE_TYPES = {
    0x02: ("beacon", "iBeacon"),
    0x05: ("phone", "AirDrop (iPhone, iPad or Mac)"),
    0x07: ("audio", "Proximity Pairing (AirPods or Beats)"),
    0x09: ("computer", "AirPlay target (Apple TV, HomePod or Mac)"),
    0x0A: ("phone", "AirPlay source"),
    0x0B: ("wearable", "Magic Switch (Apple Watch)"),
    0x0C: ("phone", "Handoff (iPhone, iPad or Mac)"),
    0x0D: ("phone", "Instant Hotspot target"),
    0x0E: ("phone", "Instant Hotspot source"),
    0x0F: ("phone", "Nearby Action (iPhone, iPad or Mac)"),
    0x10: ("phone", "Nearby Info (iPhone, iPad, Mac or Watch)"),
    0x12: ("tracker", "Find My broadcast (AirTag, or an Apple device away from its owner)"),
}
# 16-bit service UUIDs: short -> (name, kind or None)
SERVICES = {
    "180d": ("Heart Rate", "wearable"), "180f": ("Battery", None),
    "180a": ("Device Information", None), "1812": ("HID (keyboard, mouse, remote)", "peripheral"),
    "1816": ("Cycling Speed and Cadence", "wearable"), "1818": ("Cycling Power", "wearable"),
    "1826": ("Fitness Machine", "wearable"), "181a": ("Environmental Sensing", "beacon"),
    "1809": ("Health Thermometer", None), "1810": ("Blood Pressure", "wearable"),
    "fd6f": ("Exposure Notification", "phone"), "fe2c": ("Google Fast Pair", "audio"),
    "feed": ("Tile", "tracker"), "feec": ("Tile", "tracker"), "fd5a": ("Samsung SmartTag", "tracker"),
    "fd59": ("Samsung SmartThings Find", "tracker"), "fd69": ("Samsung", None),
    "feaa": ("Eddystone", "beacon"), "fee0": ("Huami (Mi Band)", "wearable"),
    "fe9f": ("Google", None), "fef3": ("Google", None), "fcb2": ("Apple (Continuity)", None),
    "fe03": ("Amazon Alexa", None), "fe07": ("Sonos", "audio"), "fdf0": ("Google Cast", None),
    "fd44": ("Apple", None), "fcf1": ("Google", None), "fe0f": ("Signify (Philips Hue)", None),
    "1802": ("Immediate Alert", None), "1803": ("Link Loss", None), "1804": ("Tx Power", None),
}
NAME_KIND = [
    (r"tag|tile|tracker|airtag", "tracker"),
    (r"bud|pod|headphone|wh-|wf-|jbl|bose|beats|soundcore|speaker|sonos", "audio"),
    (r"watch|band|fit|garmin|whoop|ring|polar|amazfit", "wearable"),
    (r"tv|bravia|roku|chromecast|desktop|laptop|book|\bpc\b", "computer"),
    (r"keyboard|mouse|mx |trackpad|controller|gamepad|pen", "peripheral"),
    (r"phone|pixel|galaxy|ipad", "phone"),
]
HOTSPOT = re.compile(r"iphone|android|galaxy|pixel|hotspot|oneplus|redmi|moto ", re.I)


def classify_ble(name, mfr, uuids, svc_data):
    """Return (kind, company, evidence list) from what an advertisement gives away."""
    kind, company, why = "unknown", "", []
    for cid, data in mfr.items():
        company = COMPANIES.get(cid, f"company 0x{cid:04X}")
        if cid == 0x004C and data:
            k, what = APPLE_TYPES.get(data[0], (None, f"Apple continuity type 0x{data[0]:02X}"))
            why.append(f"Apple continuity: {what}")
            if k:
                kind = k
        elif cid in COMPANY_KIND:
            kind, w = COMPANY_KIND[cid]
            why.append(w)
        else:
            why.append(f"manufacturer data from {company}")
    shorts = [u[4:8].lower() for u in list(uuids) + list(svc_data)
              if u.lower().endswith("-0000-1000-8000-00805f9b34fb")]
    for short in dict.fromkeys(shorts):
        sname, k = SERVICES.get(short, (None, None))
        why.append(f"service 0x{short.upper()}" + (f" {sname}" if sname else ""))
        if k and (kind == "unknown" or k == "tracker"):
            kind = k
    for u, data in svc_data.items():  # Eddystone: a beacon, or a Find My Device tag
        if u[4:8].lower() == "feaa":
            if data and data[0] in (0x40, 0x41):
                kind = "tracker"
                why.append("Eddystone frame 0x%02X: Google Find My Device tag" % data[0])
    if kind == "unknown" and name:
        for pat, k in NAME_KIND:
            m = re.search(pat, name, re.I)
            if m:
                kind = k
                why.append(f'name contains "{m.group(0).strip()}"')
                break
    if not why:
        why.append("advertisement carries no identifying data")
    return kind, company, why


def oui_vendor(addr):
    """Vendor from a MAC prefix, or None when the address is locally administered."""
    global _oui
    if int(addr[:2], 16) & 2:
        return None
    if _oui is None:
        _oui = {}
        try:
            for line in OUI_FILE.read_text(errors="replace").splitlines():
                if "(base 16)" in line:
                    _oui[line[:6]] = line.split("\t")[-1].strip()
        except OSError:
            pass
    return _oui.get(addr.replace(":", "")[:6].upper(), "")


def ble_addr_type(addr, bluez_type):
    """Describe a BLE address: public, or which flavour of random."""
    if bluez_type == "public":
        return "public (fixed, assigned by maker)"
    if bluez_type != "random":
        return "unknown"
    top = int(addr[:2], 16) >> 6
    return {3: "random static (fixed until reset)",
            1: "resolvable private (rotates about every 15 min)",
            0: "non-resolvable private (rotates)"}.get(top, "random (reserved type)")


def dbm_from_percent(pct):
    """NetworkManager maps -100..-40 dBm linearly onto 0..100 %."""
    return -100 + 0.6 * pct


def feet_range(medium, rssi, tx_power=None):
    """Log-distance path loss: (estimate, low, high) in feet.

    Exponent 2 is open air, 3.5 is a cluttered room; the truth is usually in
    between and walls or bodies can push it outside.
    """
    if medium == "ble":
        ref = tx_power - 41 if tx_power is not None else -59
    else:
        ref = -38 if medium == "wifi" else -45
    def ft(n):
        return max(1.0, min(1000.0, 10 ** ((ref - rssi) / (10 * n)) * 3.281))
    a, b = ft(3.5), ft(2.0)
    return ft(2.7), min(a, b), max(a, b)


def band_of(feet):
    for i, bound in enumerate(BANDS):
        if feet <= bound:
            return i
    return len(BANDS) - 1


class Device:
    def __init__(self, medium, addr, rssi, now):
        self.id = hashlib.sha1(f"{medium}{addr}".encode()).hexdigest()[:10]
        self.medium, self.addr = medium, addr
        self.kind, self.why = "unknown", []
        self.name, self.company, self.chan = "", "", None
        # BLE random addresses have no maker prefix; only public ones are looked up
        self.oui = oui_vendor(addr) if medium != "ble" else None
        self.addr_type = ""
        self.tx_power = None
        self.pct = None            # wifi: NetworkManager signal percent
        self.security = self.rate = self.bssid = ""
        self.rssi = self.raw = float(rssi)
        self.feet, self.lo, self.hi = feet_range(medium, rssi)
        self.band = band_of(self.feet)
        self.closest = self.band
        self.first = self.last = now
        self.hits = 0
        self.sec_hits = 0
        self.sec_best = None
        self.hist = deque(maxlen=HISTORY)
        self.pkts = 0
        self.announced = False
        self.linger_told = 0.0
        self.alarmed = 0.0
        self.spoke = 0.0
        self.trend = "steady"

    def describe(self):
        k = KINDS[self.kind]
        if self.name:
            return f'{k} "{self.name}"'
        who = self.company or self.oui
        tail = self.addr[-5:]
        return f"{k} ({who}, ..{tail})" if who else f"{k} ..{tail}"


class World:
    def __init__(self, notify=False):
        self.devs = {}
        self.events = deque(maxlen=120)
        self.event_id = 0
        self.started = time.time()
        self.adverts = 0
        self.rate_hist = deque(maxlen=GLOBAL_HISTORY)
        self.count_hist = deque(maxlen=GLOBAL_HISTORY)
        self.crowd_mark = None
        self.air_heard = 0.0
        self.status = {"ble": "listening", "wifi": "listening", "air": "not running"}
        self.notify = notify and shutil.which("notify-send")
        self.mine = set()
        for f in (MINE, OLD_MINE):
            try:
                self.mine = set(json.loads(f.read_text()))
                break
            except (OSError, ValueError):
                continue

    def event(self, level, text, dev=None):
        if time.time() - self.started < WARMUP_SECONDS:
            return
        if dev is not None:
            if level < 2 and time.time() - dev.spoke < EVENT_COOLDOWN:
                return
            dev.spoke = time.time()
        self.event_id += 1
        self.events.append({"id": self.event_id, "t": time.time(), "lv": level,
                            "text": text, "dev": dev.id if dev else None})
        if level >= 2 and self.notify:
            asyncio.ensure_future(asyncio.create_subprocess_exec(
                "notify-send", "-a", "veilglass", "veilglass", text))

    def sense(self, medium, addr, rssi, **info):
        now = time.time()
        key = (medium, addr)
        d = self.devs.get(key)
        if d is None:
            d = self.devs[key] = Device(medium, addr, rssi, now)
        else:
            d.rssi += 0.2 * (rssi - d.rssi)
        d.raw = rssi
        d.sec_best = rssi if d.sec_best is None else max(d.sec_best, rssi)
        d.last = now
        d.hits += 1
        d.sec_hits += 1
        for k in ("name", "company", "chan", "bssid", "security", "rate", "addr_type", "pct"):
            v = info.get(k)
            if v not in (None, ""):
                setattr(d, k, v)
        if medium == "ble" and info.get("addr_type", "").startswith("public") and d.oui is None:
            d.oui = oui_vendor(addr) or ""
        if info.get("tx_power") is not None:
            d.tx_power = info["tx_power"]
        if info.get("why"):
            d.why = info["why"]
        kind = info.get("kind", "unknown")
        if kind != "unknown" and not (d.kind == "client" and kind == "prober"):
            d.kind = kind
        d.feet, d.lo, d.hi = feet_range(medium, d.rssi, d.tx_power)
        quiet = addr in self.mine or medium == "wifi"  # access points do not move
        need = 3 if medium == "ble" else 1

        if not d.announced and d.hits >= need:
            d.announced = True
            if not quiet and d.band <= 1:
                self.event(1 if d.band == 0 else 0,
                           f"New: {d.describe()}, about {d.feet:.0f} ft ({d.raw:.0f} dBm)", d)
            return

        cand = band_of(d.feet)
        if cand < d.band and d.feet <= BANDS[cand] * 0.85:
            d.band = cand
            d.closest = min(d.closest, cand)
            if d.announced and not quiet:
                if cand == 0:  # full alert once per 5 min; a device hovering at 6 ft only notes
                    level = 2 if now - d.alarmed > 300 else 1
                    if level == 2:
                        d.alarmed = now
                    self.event(level, f"{d.describe()} is within 6 ft (est. {d.feet:.0f} ft)", d)
                else:
                    self.event(1, f"{d.describe()} came within {BANDS[cand]} ft "
                                  f"(est. {d.feet:.0f} ft)", d)
        elif cand > d.band and d.feet >= BANDS[d.band] * 1.4:
            bound = BANDS[d.band]
            d.band = cand
            if d.announced and not quiet and d.closest <= 1:
                self.event(0, f"{d.describe()} moved beyond {bound} ft", d)

    def tick(self):
        """Once a second: sample history, expire, trends, slow alerts."""
        now = time.time()
        self.rate_hist.append(self.adverts)
        self.adverts = 0
        for key, d in list(self.devs.items()):
            quiet = d.addr in self.mine or d.medium == "wifi"
            if now - d.last > TTL[d.medium]:
                del self.devs[key]
                if d.announced and not quiet and d.closest <= 1:
                    self.event(0, f"{d.describe()} not heard for {TTL[d.medium]} s, dropped", d)
                continue
            sample = d.sec_best
            if sample is None and d.medium == "wifi" and now - d.last < 6:
                sample = d.raw  # nmcli is polled every 5 s; hold its last reading
            d.hist.append(None if sample is None else round(sample))
            d.pkts, d.sec_hits, d.sec_best = d.sec_hits, 0, None
            heard = [v for v in d.hist if v is not None]
            if len(heard) >= 6:
                old, new = heard[-6:-3], heard[-3:]
                delta = sum(new) / 3 - sum(old) / 3
                d.trend = "closer" if delta >= 4 else "farther" if delta <= -4 else "steady"
            if (d.kind == "tracker" and not quiet and d.band <= 1
                    and now - d.first > LINGER_SECONDS
                    and now - d.linger_told > LINGER_SECONDS):
                d.linger_told = now
                self.event(2, f"{d.describe()} has stayed within 20 ft for "
                              f"{(now - d.first) / 60:.0f} min", d)
        by = {"ble": 0, "wifi": 0, "air": 0}
        for d in self.devs.values():
            if d.hits >= (3 if d.medium == "ble" else 1):
                by[d.medium] += 1
        self.count_hist.append(by)
        crowd = sum(1 for d in self.devs.values()
                    if d.medium != "wifi" and d.band <= 2 and d.addr not in self.mine)
        if self.crowd_mark is None:
            self.crowd_mark = crowd
        elif abs(crowd - self.crowd_mark) >= 5:
            self.event(1, f"Devices within 60 ft: {self.crowd_mark} -> {crowd}")
            self.crowd_mark = crowd

    def snapshot(self):
        now = time.time()
        chans = {}
        devs = []
        for d in self.devs.values():
            if d.hits < (3 if d.medium == "ble" else 1):
                continue
            if d.medium == "wifi" and d.chan:
                c = chans.setdefault(d.chan, {"aps": 0, "best": None})
                c["aps"] += 1
                c["best"] = d.raw if c["best"] is None else max(c["best"], d.raw)
            ap = self.devs.get(("wifi", d.bssid)) if d.bssid else None
            devs.append({
                "id": d.id, "m": d.medium, "kind": d.kind, "why": d.why,
                "name": d.name, "company": d.company, "oui": d.oui,
                "addr": d.addr, "atype": d.addr_type, "tx": d.tx_power,
                "rssi": round(d.rssi), "raw": round(d.raw), "pct": d.pct,
                "feet": round(d.feet, 1), "lo": round(d.lo, 1), "hi": round(d.hi, 1),
                "band": d.band, "trend": d.trend, "age": int(now - d.first),
                "ago": round(now - d.last, 1), "hits": d.hits, "pkts": d.pkts,
                "hist": list(d.hist), "mine": d.addr in self.mine,
                "chan": d.chan, "sec": d.security, "rate": d.rate,
                "bssid": (ap.name or d.bssid) if ap else d.bssid,
            })
        return {"t": now, "warming": now - self.started < WARMUP_SECONDS,
                "status": self.status, "devs": devs, "chans": chans,
                "rate": list(self.rate_hist), "counts": list(self.count_hist),
                "events": list(self.events)}

    def set_mine(self, dev_id, on):
        for d in self.devs.values():
            if d.id == dev_id:
                (self.mine.add if on else self.mine.discard)(d.addr)
                MINE.parent.mkdir(parents=True, exist_ok=True)
                MINE.write_text(json.dumps(sorted(self.mine)))
                return True
        return False


async def ble_task(world):
    from bleak import BleakScanner

    def heard(dev, adv):
        name = adv.local_name or dev.name or ""
        kind, company, why = classify_ble(name, adv.manufacturer_data,
                                          adv.service_uuids, adv.service_data)
        props = dev.details.get("props", {}) if isinstance(dev.details, dict) else {}
        world.adverts += 1
        world.sense("ble", dev.address, adv.rssi, name=name, company=company, kind=kind,
                    why=why, tx_power=adv.tx_power,
                    addr_type=ble_addr_type(dev.address, props.get("AddressType")))

    while True:
        try:
            async with BleakScanner(heard):
                world.status["ble"] = "listening"
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # radio off, no adapter, bluez restarting
            world.status["ble"] = f"unavailable: {ex}"
            await asyncio.sleep(10)


async def nmcli(*args):
    proc = await asyncio.create_subprocess_exec(
        "nmcli", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


def nm_split(line):
    parts = line.replace("\\:", "\0").split(":")
    return [p.replace("\0", ":") for p in parts]


async def wifi_task(world):
    if not shutil.which("nmcli"):
        world.status["wifi"] = "unavailable: nmcli not found"
        return
    sweep = 0
    while True:
        if sweep % 6 == 0:  # ask for a fresh scan every ~30 s
            await nmcli("dev", "wifi", "rescan")
        sweep += 1
        out = await nmcli("-t", "-f", "BSSID,SSID,SIGNAL,CHAN,SECURITY,RATE",
                          "dev", "wifi", "list", "--rescan", "no")
        seen = 0
        for line in out.splitlines():
            parts = nm_split(line)
            if len(parts) < 6:
                continue
            bssid, ssid, sig, chan, sec, rate = parts[:6]
            try:
                pct, chan = int(sig), int(chan)
            except ValueError:
                continue
            why = ["beacon listed by NetworkManager"]
            kind = "ap"
            if HOTSPOT.search(ssid):
                kind = "hotspot"
                why.append("network name looks like a phone hotspot")
            vendor = oui_vendor(bssid)
            if vendor is None:
                why.append("locally administered BSSID (common for hotspots and mesh)")
            world.sense("wifi", bssid, dbm_from_percent(pct), name=ssid, kind=kind,
                        chan=chan, pct=pct, security=sec or "open", rate=rate, why=why)
            seen += 1
        world.status["wifi"] = "listening" if seen else "unavailable: no wifi radio answering"
        await asyncio.sleep(5)


class AirEar(asyncio.DatagramProtocol):
    """Receives sightings from airwatch.py (wifi clients and beacons)."""

    def __init__(self, world):
        self.world = world

    def datagram_received(self, data, _addr):
        w = self.world
        w.air_heard = time.time()
        try:
            batch = json.loads(data)
        except ValueError:
            return
        for s in batch:
            try:
                addr, rssi, role = s["addr"], float(s["rssi"]), s["role"]
            except (KeyError, TypeError, ValueError):
                continue
            ssid = s.get("ssid") or ""
            if role == "ap":
                w.sense("wifi", addr, rssi, name=ssid, chan=s.get("chan"),
                        kind="hotspot" if HOTSPOT.search(ssid) else "ap",
                        why=["beacon heard by airwatch"])
                continue
            w.adverts += 1
            why = (["sent data or association frames to an access point"] if role == "client"
                   else ["sent probe requests" + (f' asking for "{ssid}"' if ssid else " (any network)")])
            w.sense("air", addr, rssi, chan=s.get("chan"), bssid=s.get("bssid"),
                    name=f"probing for {ssid}" if ssid else "", why=why,
                    kind="client" if role == "client" else "prober")


async def air_task(world):
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: AirEar(world), local_addr=("127.0.0.1", AIR_PORT))
    while True:
        await asyncio.sleep(5)
        if world.air_heard:
            quiet = time.time() - world.air_heard > 15
            world.status["air"] = "unavailable: airwatch stopped" if quiet else "listening"


async def ticker(world):
    while True:
        world.tick()
        await asyncio.sleep(1)


async def events(request):
    world = request.app["world"]
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache"})
    await resp.prepare(request)
    try:
        while True:
            await resp.write(f"data: {json.dumps(world.snapshot())}\n\n".encode())
            await asyncio.sleep(1)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def mine(request):
    body = await request.json()
    ok = request.app["world"].set_mine(str(body.get("id")), bool(body.get("on")))
    return web.json_response({"ok": ok})


async def index(_request):
    return web.FileResponse(HERE / "index.html")


async def veil_check(_request):
    return web.FileResponse(HERE / "veil" / "check.html")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 lets another machine watch")
    ap.add_argument("--port", type=int, default=7733)
    ap.add_argument("--no-radio", action="store_true",
                    help="serve the page without listening (for tests)")
    ap.add_argument("--notify", action="store_true", help="desktop notification on alerts")
    args = ap.parse_args()

    world = World(notify=args.notify)
    if args.no_radio:
        world.status = {k: "off (--no-radio)" for k in world.status}

    async def background(app):
        jobs = [] if args.no_radio else [ble_task, wifi_task, air_task]
        tasks = [asyncio.create_task(job(world)) for job in jobs + [ticker]]
        yield
        for t in tasks:
            t.cancel()

    app = web.Application()
    app["world"] = world
    app.cleanup_ctx.append(background)
    app.add_routes([web.get("/", index), web.get("/events", events),
                    web.post("/api/mine", mine),
                    web.get("/veil-check", veil_check),
                    web.static("/fonts", HERE / "fonts")])
    web.run_app(app, host=args.host, port=args.port, print=lambda *_: print(
        f"veilglass open at http://{args.host}:{args.port}"))


if __name__ == "__main__":
    main()
