#!/usr/bin/env python3
"""veilglass: a scrying slate for the radio presences around you.

Listens to Bluetooth LE advertisements and Wi-Fi beacons, turns each emitter
into a veiled "entity", estimates its distance from signal strength, and
streams the result to a local web page. It never connects to, probes or
transmits at anyone's device, and it keeps nothing on disk except the
addresses you bind as familiars.
"""
import argparse
import asyncio
import hashlib
import json
import random
import re
import shutil
import time
from collections import deque
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).parent
FAMILIARS = Path.home() / ".config" / "veilglass" / "familiars.json"

# (name, outer bound in feet)
ZONES = [("touch", 6), ("near", 20), ("far", 60), ("veil", 1e9)]
KIND_NAMES = {
    "walker": "Walker", "listener": "Listener", "tether": "Tether",
    "pulse": "Pulse", "engine": "Engine", "lantern": "Lantern",
    "relic": "Relic", "pillar": "Pillar", "flame": "Flamebearer",
    "drifter": "Drifter", "seeker": "Seeker", "shade": "Shade",
}
TTL = {"ble": 40, "wifi": 150, "air": 90}
AIR_PORT = 7734  # airwatch.py sends its sightings here
OUI_FILE = Path("/usr/share/hwdata/oui.txt")
_oui = None
ATTUNE_SECONDS = 20  # stay quiet while the first sweep fills the glass
LINGER_SECONDS = 600
OMEN_COOLDOWN = 45  # one entity may not mutter more often than this

VENDORS = {
    0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google",
    0x0087: "Garmin", 0x009E: "Bose", 0x012D: "Sony", 0x038F: "Xiaomi",
    0x027D: "Huawei", 0x0171: "Amazon", 0x0059: "Nordic", 0x00C4: "LG",
    0x01DA: "Logitech", 0x0067: "GN/Jabra",
}
VENDOR_KIND = {
    0x0006: "engine", 0x0075: "walker", 0x00E0: "walker", 0x0087: "pulse",
    0x009E: "listener", 0x012D: "listener", 0x0067: "listener",
    0x038F: "walker", 0x027D: "walker", 0x01DA: "relic",
}
# Apple continuity message type -> kind
APPLE_KIND = {
    0x12: "tether", 0x07: "listener", 0x02: "lantern", 0x09: "engine",
    0x10: "walker", 0x0F: "walker", 0x0C: "walker",
}
UUID_KIND = {
    "feed": "tether", "feec": "tether", "fd5a": "tether",  # Tile, SmartTag
    "fe2c": "listener",  # Fast Pair
    "fd6f": "walker",    # exposure notification
    "180d": "pulse", "fee0": "pulse",
    "1812": "relic",
}
NAME_KIND = [
    (r"tag|tile|tracker", "tether"),
    (r"bud|pod|headphone|wh-|wf-|jbl|bose|beats|soundcore", "listener"),
    (r"watch|band|fit|garmin|whoop|ring", "pulse"),
    (r"tv|bravia|roku|chromecast|desktop|laptop|book", "engine"),
    (r"phone|pixel|galaxy", "walker"),
]
HOTSPOT = re.compile(r"iphone|android|galaxy|pixel|hotspot|oneplus|redmi|moto ", re.I)

SYL_A = ["vex", "sai", "xal", "mor", "nyx", "lux", "zex", "ael", "dem", "lar",
         "xig", "xem", "rox", "ax", "mar", "xi", "ul", "or", "ith", "kae",
         "sor", "ven", "dru", "yx", "ob", "thren", "qua", "is"]
SYL_B = ["ion", "ord", "en", "as", "eth", "ix", "ur", "ael", "oth", "yn",
         "ar", "is", "um", "ex", "aun", "or"]


def veiled_name(seed):
    return (SYL_A[seed % len(SYL_A)] + SYL_B[(seed >> 8) % len(SYL_B)]).capitalize()


def classify_ble(name, mfr, uuids, svc_data):
    """Return (kind, vendor) from what an advertisement gives away."""
    vendor, kind = "", "shade"
    for cid, data in mfr.items():
        vendor = VENDORS.get(cid, vendor)
        if cid == 0x004C:
            kind = APPLE_KIND.get(data[0], "walker") if data else "walker"
        elif cid in VENDOR_KIND:
            kind = VENDOR_KIND[cid]
    shorts = {u[4:8].lower() for u in list(uuids) + list(svc_data)
              if u.lower().endswith("-0000-1000-8000-00805f9b34fb")}
    for short, k in UUID_KIND.items():
        if short in shorts:
            kind = k
    for u, data in svc_data.items():  # Eddystone: beacon, or Find My Device tag
        if u[4:8].lower() == "feaa":
            kind = "tether" if data and data[0] in (0x40, 0x41) else "lantern"
    if kind == "shade" and name:
        for pat, k in NAME_KIND:
            if re.search(pat, name, re.I):
                kind = k
                break
    return kind, vendor


def maker(addr):
    """Vendor from the address prefix; randomized addresses have none."""
    global _oui
    if int(addr[:2], 16) & 2:
        return "masked (randomized address)"
    if _oui is None:
        _oui = {}
        try:
            for line in OUI_FILE.read_text(errors="replace").splitlines():
                if "(base 16)" in line:
                    _oui[line[:6]] = line.split("\t")[-1].strip()
        except OSError:
            pass
    return _oui.get(addr.replace(":", "")[:6].upper(), "")


def feet_from_rssi(medium, rssi):
    """Log-distance path loss. Walls and bodies make this a rough guess."""
    ref, n = {"ble": (-59, 2.5), "wifi": (-38, 3.0), "air": (-45, 3.0)}[medium]
    metres = 10 ** ((ref - rssi) / (10 * n))
    return max(1.0, min(400.0, metres * 3.281))


def zone_index(feet):
    for i, (_, bound) in enumerate(ZONES):
        if feet <= bound:
            return i
    return len(ZONES) - 1


class Entity:
    def __init__(self, medium, addr, rssi, now):
        digest = hashlib.sha1(f"{medium}{addr}".encode()).hexdigest()
        self.id = digest[:10]
        self.seed = int(digest[:8], 16)
        self.medium, self.addr = medium, addr
        self.name = veiled_name(self.seed)
        self.kind, self.label, self.vendor, self.chan = "shade", "", "", None
        self.bound = None  # BSSID of the pillar a wifi client speaks through
        self.rssi = float(rssi)
        self.feet = feet_from_rssi(medium, rssi)
        self.zone = zone_index(self.feet)
        self.closest = self.zone
        self.first = self.last = now
        self.hits = 0
        self.announced = False
        self.linger_told = 0.0
        self.spoke = 0.0
        self.mark_feet, self.mark_t, self.trend = self.feet, now, "still"

    def title(self):
        return f"{KIND_NAMES[self.kind]} {self.name}"


class World:
    def __init__(self, notify=False):
        self.ents = {}
        self.omens = deque(maxlen=80)
        self.omen_id = 0
        self.started = time.time()
        self.adverts = 0
        self.hum = 0.0
        self.crowd_mark = None
        self.air_heard = 0.0
        self.status = {"ble": "listening", "wifi": "listening"}
        self.notify = notify and shutil.which("notify-send")
        try:
            self.familiars = set(json.loads(FAMILIARS.read_text()))
        except (OSError, ValueError):
            self.familiars = set()

    def omen(self, level, text, ent=None):
        if time.time() - self.started < ATTUNE_SECONDS:
            return
        if ent is not None:
            if level < 2 and time.time() - ent.spoke < OMEN_COOLDOWN:
                return
            ent.spoke = time.time()
        self.omen_id += 1
        self.omens.append({"id": self.omen_id, "t": time.time(), "lv": level,
                           "text": text, "ent": ent.id if ent else None})
        if level >= 2 and self.notify:
            asyncio.ensure_future(asyncio.create_subprocess_exec(
                "notify-send", "-a", "veilglass", "veilglass", text))

    def sense(self, medium, addr, rssi, label="", vendor="", kind="shade", chan=None, bound=None):
        now = time.time()
        key = (medium, addr)
        e = self.ents.get(key)
        if e is None:
            e = self.ents[key] = Entity(medium, addr, rssi, now)
        else:
            e.rssi += 0.15 * (rssi - e.rssi)
            e.feet = feet_from_rssi(medium, e.rssi)
        e.last = now
        e.hits += 1
        e.label = label or e.label
        e.vendor = vendor or e.vendor
        e.chan = chan or e.chan
        e.bound = bound or e.bound
        if kind != "shade" and not (e.kind == "drifter" and kind == "seeker"):
            e.kind = kind
        quiet = addr in self.familiars
        need = 3 if medium == "ble" else 1

        if not e.announced and e.hits >= need:
            e.announced = True
            if not quiet and e.zone <= 2:
                self.omen(1 if e.zone <= 1 else 0,
                          f"{e.title()} surfaces at the {ZONES[e.zone][0]} ring · ~{e.feet:.0f} ft", e)
            return

        cand = zone_index(e.feet)
        if cand < e.zone and e.feet <= ZONES[cand][1] * 0.85:
            e.zone = cand
            e.closest = min(e.closest, cand)
            if e.announced and not quiet:
                if cand == 0:
                    self.omen(2, f"{e.title()} stands at your shoulder · ~{e.feet:.0f} ft", e)
                else:
                    self.omen(1, f"{e.title()} resonates within {ZONES[cand][1]} ft", e)
        elif cand > e.zone and e.feet >= ZONES[e.zone][1] * 1.4:
            bound = ZONES[e.zone][1]
            e.zone = cand
            if e.announced and not quiet and e.closest <= 1:
                self.omen(0, f"{e.title()} withdraws past {bound} ft", e)

    def tick(self):
        """Once a second: expire, track trends, raise slow-burn omens."""
        now = time.time()
        self.hum += 0.4 * (self.adverts - self.hum)
        self.adverts = 0
        for key, e in list(self.ents.items()):
            quiet = e.addr in self.familiars
            if now - e.last > TTL[e.medium]:
                del self.ents[key]
                if e.announced and not quiet and e.closest <= 1:
                    self.omen(0, f"{e.title()} slips beyond the veil", e)
                continue
            if now - e.mark_t >= 5:
                ratio = e.feet / e.mark_feet
                e.trend = "approach" if ratio < 0.8 else "recede" if ratio > 1.25 else "still"
                e.mark_feet, e.mark_t = e.feet, now
            if (e.kind == "tether" and not quiet and e.zone <= 1
                    and now - e.first > LINGER_SECONDS
                    and now - e.linger_told > LINGER_SECONDS):
                e.linger_told = now
                self.omen(2, f"A Tether, {e.name}, has clung to you for "
                             f"{(now - e.first) / 60:.0f} min", e)
        crowd = sum(1 for e in self.ents.values()
                    if e.medium != "wifi" and e.zone <= 2 and e.addr not in self.familiars)
        if self.crowd_mark is None:
            self.crowd_mark = crowd
        elif abs(crowd - self.crowd_mark) >= 5:
            word = "thickens" if crowd > self.crowd_mark else "thins"
            self.omen(1, f"The air {word}: {crowd} presences inside the far ring")
            self.crowd_mark = crowd

    def snapshot(self):
        now = time.time()
        waves = {}
        ents = []
        for e in self.ents.values():
            if e.hits < (3 if e.medium == "ble" else 1):
                continue
            if e.medium == "wifi" and e.chan:
                strength = max(0.0, min(1.0, (e.rssi + 95) / 60))
                waves[e.chan] = max(waves.get(e.chan, 0), strength)
            ents.append({
                "id": e.id, "seed": e.seed, "m": e.medium, "kind": e.kind,
                "name": e.name, "feet": round(e.feet, 1), "rssi": round(e.rssi),
                "zone": e.zone, "trend": e.trend, "age": int(now - e.first),
                "fam": e.addr in self.familiars, "addr": e.addr,
                "label": e.label, "vendor": e.vendor, "chan": e.chan,
                "bound": pillar.title() if (pillar := self.ents.get(("wifi", e.bound))) else e.bound,
            })
        return {"t": now, "attuning": now - self.started < ATTUNE_SECONDS,
                "hum": round(self.hum, 1), "status": self.status,
                "ents": ents, "waves": waves, "omens": list(self.omens)}

    def bind(self, ent_id, on):
        for e in self.ents.values():
            if e.id == ent_id:
                (self.familiars.add if on else self.familiars.discard)(e.addr)
                FAMILIARS.parent.mkdir(parents=True, exist_ok=True)
                FAMILIARS.write_text(json.dumps(sorted(self.familiars)))
                return True
        return False


async def ble_task(world):
    from bleak import BleakScanner

    def heard(dev, adv):
        name = adv.local_name or dev.name or ""
        kind, vendor = classify_ble(name, adv.manufacturer_data,
                                    adv.service_uuids, adv.service_data)
        world.adverts += 1
        world.sense("ble", dev.address, adv.rssi, label=name, vendor=vendor, kind=kind)

    while True:
        try:
            async with BleakScanner(heard):
                world.status["ble"] = "listening"
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # radio off, no adapter, bluez restarting
            world.status["ble"] = f"sealed: {ex}"
            await asyncio.sleep(10)


async def nmcli(*args):
    proc = await asyncio.create_subprocess_exec(
        "nmcli", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


async def wifi_task(world):
    if not shutil.which("nmcli"):
        world.status["wifi"] = "sealed: nmcli not found"
        return
    sweep = 0
    while True:
        if sweep % 6 == 0:  # ask for a fresh scan every ~30 s
            await nmcli("dev", "wifi", "rescan")
        sweep += 1
        out = await nmcli("-t", "-f", "BSSID,SSID,SIGNAL,FREQ", "dev", "wifi", "list", "--rescan", "no")
        seen = 0
        for line in out.splitlines():
            parts = line.replace("\\:", "\0").split(":")
            if len(parts) < 4:
                continue
            bssid, ssid = parts[0].replace("\0", ":"), parts[1].replace("\0", ":")
            try:
                pct, mhz = int(parts[2]), int(parts[3].split()[0])
            except ValueError:
                continue
            chan = (mhz - 2407) // 5 if mhz < 2484 else 14 if mhz == 2484 else (mhz - 5000) // 5
            world.sense("wifi", bssid, pct / 2 - 100, label=ssid,
                        kind="flame" if HOTSPOT.search(ssid) else "pillar", chan=chan)
            seen += 1
        world.status["wifi"] = "listening" if seen else "sealed: no wifi radio answering"
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
                w.sense("wifi", addr, rssi, label=ssid, chan=s.get("chan"),
                        kind="flame" if HOTSPOT.search(ssid) else "pillar")
                continue
            w.adverts += 1
            w.sense("air", addr, rssi, vendor=maker(addr), chan=s.get("chan"),
                    label=f"seeks {ssid}" if ssid else "", bound=s.get("bssid"),
                    kind="drifter" if role == "client" else "seeker")


async def air_task(world):
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: AirEar(world), local_addr=("127.0.0.1", AIR_PORT))
    while True:
        await asyncio.sleep(5)
        if world.air_heard:
            quiet = time.time() - world.air_heard > 15
            world.status["air"] = "sealed: airwatch stopped" if quiet else "listening"


async def demo_task(world):
    """Synthetic presences, for trying the glass without a radio."""
    rng = random.Random(2133)
    kinds = ["walker"] * 5 + ["listener", "listener", "tether", "pulse", "engine", "lantern", "relic", "shade", "shade"]
    cast = [{"addr": "DE:M0:%02X:%02X:%02X:%02X" % tuple(rng.randrange(256) for _ in range(4)),
             "kind": k, "rssi": rng.uniform(-98, -55), "v": 0.0} for k in kinds]
    for i, ch in enumerate([1, 1, 6, 6, 6, 11, 36, 44, 149]):
        world.sense("wifi", f"DE:M0:AP:00:00:{i:02X}", rng.uniform(-88, -45),
                    label=f"demo-{i}", kind="flame" if i == 3 else "pillar", chan=ch)
    while True:
        for c in cast:
            c["v"] = max(-1.2, min(1.2, c["v"] + rng.uniform(-0.4, 0.4)))
            c["rssi"] = max(-102, min(-42, c["rssi"] + c["v"]))
            if c["rssi"] > -100:
                world.adverts += 1
                world.sense("ble", c["addr"], c["rssi"] + rng.uniform(-2, 2), kind=c["kind"])
        for i in range(9):
            e = world.ents.get(("wifi", f"DE:M0:AP:00:00:{i:02X}"))
            if e:
                world.sense("wifi", e.addr, e.rssi + rng.uniform(-1.5, 1.5))
        await asyncio.sleep(0.5)


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


async def familiar(request):
    body = await request.json()
    ok = request.app["world"].bind(str(body.get("id")), bool(body.get("on")))
    return web.json_response({"ok": ok})


async def index(_request):
    return web.FileResponse(HERE / "index.html")


async def veil_check(_request):
    return web.FileResponse(HERE / "veil" / "check.html")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 lets another machine watch this glass")
    ap.add_argument("--port", type=int, default=7733)
    ap.add_argument("--demo", action="store_true", help="synthetic presences, no radios")
    ap.add_argument("--notify", action="store_true", help="desktop notification on alarms")
    args = ap.parse_args()

    world = World(notify=args.notify)

    async def background(app):
        jobs = [demo_task] if args.demo else [ble_task, wifi_task, air_task]
        tasks = [asyncio.create_task(job(world)) for job in jobs + [ticker]]
        yield
        for t in tasks:
            t.cancel()

    app = web.Application()
    app["world"] = world
    app.cleanup_ctx.append(background)
    app.add_routes([web.get("/", index), web.get("/events", events),
                    web.post("/api/familiar", familiar),
                    web.get("/veil-check", veil_check)])
    web.run_app(app, host=args.host, port=args.port, print=lambda *_: print(
        f"veilglass open at http://{args.host}:{args.port}"))


if __name__ == "__main__":
    main()
