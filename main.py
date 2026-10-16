__version__ = "3.0.0"
"""
Wi-Fi Mini Scanner v3 (Kivy / Android APK) - full script

HOME     live Wi-Fi list + current connection + quick actions (no need to open Android settings)
RADAR    full circle. Distance from centre = estimated from signal strength (stronger = closer).
         Direction = measured with the phone compass while you turn (Find direction).
         The strongest signal is marked as the device (router) location.
DEVICES  devices on the Wi-Fi you are connected to, each listed ONCE, with names when they answer
VPN      VPN status, shortcut to Android VPN settings, launcher for your VPN apps
SETTINGS scan, filters, radar, units, name sharing, VPN options

Buildozer: requirements = python3,kivy,pyjnius,android
permissions = ACCESS_FINE_LOCATION,NEARBY_WIFI_DEVICES,ACCESS_WIFI_STATE,CHANGE_WIFI_STATE,
              ACCESS_NETWORK_STATE,CHANGE_NETWORK_STATE,CHANGE_WIFI_MULTICAST_STATE,INTERNET
Without Android Wi-Fi access (Pydroid etc.) the app runs in DEMO mode with sample data.
Only scan networks you own or have permission to use.
"""
import json
import math
import os
import random
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================ HELPERS START
def band_of(freq):
    return "2.4" if freq < 3000 else ("5" if freq < 5925 else "6")


def channel_of(freq):
    if freq == 2484:
        return 14
    if freq < 3000:
        return int((freq - 2407) / 5)
    if freq < 5925:
        return int((freq - 5000) / 5)
    return int((freq - 5950) / 5)


def parse_security(caps):
    c = (caps or "").upper()
    if "EAP" in c:
        return "Enterprise"
    if "SAE" in c and "PSK" in c:
        return "WPA2/WPA3"
    if "SAE" in c:
        return "WPA3"
    if "OWE" in c:
        return "Enhanced Open"
    if "WPA2" in c or "RSN" in c:
        return "WPA2"
    if "WPA" in c:
        return "WPA"
    if "WEP" in c:
        return "WEP"
    return "Open"


def bars_of(level):
    return 4 if level >= -55 else 3 if level >= -67 else 2 if level >= -78 else 1 if level >= -88 else 0


def percent_of(level):
    return max(0, min(100, 2 * (level + 100)))


def estimate_distance(level, env=3.0, ref=-40.0):
    """Log-distance model: stronger signal -> smaller distance. Approximate (walls change it a lot)."""
    return max(0.5, min(500.0, 10 ** ((ref - level) / (10.0 * env))))


def fmt_dist(m, unit):
    if unit == "ft":
        return f"{m * 3.281:.0f} ft"
    return f"{m:.0f} m" if m >= 10 else f"{m:.1f} m"


def compass_text(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(((deg % 360) + 22.5) // 45) % 8]


def conf_text(c):
    return "high" if c >= .6 else "medium" if c >= .25 else "low"


def heading_from_rotation(v):
    """Compass heading (0-360, 0 = north) the phone is pointing at, from a rotation-vector sensor."""
    if v is None or len(v) < 3:
        return None
    x, y, z = float(v[0]), float(v[1]), float(v[2])
    w = float(v[3]) if len(v) > 3 else math.sqrt(max(0.0, 1 - x * x - y * y - z * z))
    r1, r2 = 2 * (x * y - z * w), 2 * (x * z + y * w)
    r4, r5 = 1 - 2 * x * x - 2 * z * z, 2 * (y * z - x * w)
    r8 = 1 - 2 * x * x - 2 * y * y
    az = math.atan2(r1, r4) if abs(r8) > 0.7 else math.atan2(-r2, -r5)  # flat -> top edge, upright -> back camera
    return math.degrees(az) % 360


def int_to_ip(n):
    n &= 0xFFFFFFFF
    return ".".join(str((n >> (8 * i)) & 0xFF) for i in range(4))


def subnet_hosts(ip, mask):
    def to_int(s):
        a = [int(x) for x in s.split(".")]
        return (a[0] << 24) | (a[1] << 16) | (a[2] << 8) | a[3]
    i, m = to_int(ip), to_int(mask)
    if bin(m).count("1") < 24:
        m = 0xFFFFFF00
    net = i & m
    size = (~m) & 0xFFFFFFFF
    return [".".join(str(((net + h) >> s) & 255) for s in (24, 16, 8, 0)) for h in range(1, size)]


def device_hint(ports, name=""):
    p = set(ports or [])
    n = (name or "").lower()
    if 62078 in p or "iphone" in n or "ipad" in n:
        return "Apple device"
    if 5555 in p:
        return "Android (debug)"
    if 445 in p or 139 in p:
        return "PC / NAS"
    if 22 in p:
        return "SSH device"
    if 80 in p or 443 in p or 8080 in p:
        return "Has web page"
    return ""


class DirectionTracker:
    """Learns the compass bearing of each network: the heading where its signal is strongest."""
    N = 36

    def __init__(self):
        self.db, self.cnt = {}, {}

    def reset(self):
        self.db.clear()
        self.cnt.clear()

    def add(self, key, heading, level):
        b = int((heading % 360) // 10) % self.N
        s = self.db.setdefault(key, [0.0] * self.N)
        c = self.cnt.setdefault(key, [0] * self.N)
        s[b] += level
        c[b] += 1

    def bearing(self, key):
        c = self.cnt.get(key)
        if not c:
            return None
        cov = [i for i in range(self.N) if c[i]]
        if len(cov) < 6:
            return None
        avg = {i: self.db[key][i] / c[i] for i in cov}
        sm = {}
        for i in cov:
            vals = [avg[i]] + [avg[j] for j in ((i - 1) % self.N, (i + 1) % self.N) if j in avg]
            sm[i] = sum(vals) / len(vals)
        peak = max(sm, key=sm.get)
        mean = sum(sm.values()) / len(sm)
        return (peak * 10 + 5) % 360, max(0.0, min(1.0, (sm[peak] - mean) / 8.0))


# ---- device name discovery (only works when the other device answers) ----
NBNS_QUERY = (b"\x13\x37\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"\x20" + b"CK" + b"A" * 30 + b"\x00" +
              b"\x00\x21\x00\x01")


def parse_nbns(data):
    if len(data) < 57:
        return ""
    off = 57
    for _ in range(data[56]):
        rec = data[off:off + 18]
        if len(rec) < 18:
            break
        name = rec[:15].decode("ascii", "ignore").strip()
        if rec[15] == 0 and not (int.from_bytes(rec[16:18], "big") & 0x8000) and name:
            return name
        off += 18
    return ""


def dns_ptr_query(ip):
    labels = ip.split(".")[::-1] + ["in-addr", "arpa"]
    q = b"".join(bytes([len(x)]) + x.encode() for x in labels) + b"\x00"
    return b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + q + b"\x00\x0c\x00\x01"


def _dns_name(data, off):
    parts, jumped, end, hops = [], False, off, 0
    while off < len(data) and hops < 20:
        ln = data[off]
        if ln == 0:
            if not jumped:
                end = off + 1
            break
        if ln & 0xC0 == 0xC0:
            ptr = ((ln & 0x3F) << 8) | data[off + 1]
            if not jumped:
                end = off + 2
            off, jumped, hops = ptr, True, hops + 1
            continue
        parts.append(data[off + 1:off + 1 + ln].decode("utf-8", "ignore"))
        off += 1 + ln
    return ".".join(parts), end


def parse_dns_ptr(data):
    if len(data) < 12:
        return ""
    qd, an = int.from_bytes(data[4:6], "big"), int.from_bytes(data[6:8], "big")
    off = 12
    for _ in range(qd):
        _n, off = _dns_name(data, off)
        off += 4
    for _ in range(an):
        _n, off = _dns_name(data, off)
        if off + 10 > len(data):
            break
        typ, rdlen = int.from_bytes(data[off:off + 2], "big"), int.from_bytes(data[off + 8:off + 10], "big")
        off += 10
        if typ == 12:
            name, _e = _dns_name(data, off)
            name = name.rstrip(".")
            return name[:-6] if name.endswith(".local") else name
        off += rdlen
    return ""


def netbios_name(ip, timeout=.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(NBNS_QUERY, (ip, 137))
        return parse_nbns(s.recvfrom(1024)[0])
    finally:
        s.close()


def mdns_name(ip, timeout=.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(dns_ptr_query(ip), (ip, 5353))
        return parse_dns_ptr(s.recvfrom(1500)[0])
    finally:
        s.close()


def ssdp_discover(wait=2.0):
    msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
           'MX: 1\r\nST: ssdp:all\r\n\r\n').encode()
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(.5)
    try:
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + wait
        while time.time() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m and addr[0] not in found:
                found[addr[0]] = m.group(1).decode("ascii", "ignore")
    except Exception:
        pass
    finally:
        s.close()
    return found


def ssdp_name(ip, location):
    u = urllib.parse.urlparse(location)
    if u.scheme != "http" or u.hostname != ip:  # only trust descriptions served by the device itself
        return ""
    with urllib.request.urlopen(location, timeout=1.5) as r:
        xml = r.read(30000).decode("utf-8", "ignore")
    m = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S)
    return m.group(1).strip()[:40] if m else ""
# ============================================================== HELPERS END

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import get_color_from_hex, platform

try:
    from jnius import PythonJavaClass, java_method
    JNIUS = True
except Exception:
    JNIUS = False

BG, PANEL, PANEL2 = "#071018", "#0F1E2E", "#172B40"
ACCENT, WHITE, MUTED = "#25E8C8", "#EAF4FF", "#8FA7BF"
GOOD, MID, BAD, BLUE = "#22E06B", "#F2D024", "#FF5A5A", "#2D8CFF"
ANDROID = platform == "android"

DEFAULTS = {
    "auto_scan": True, "scan_interval": 10, "group_ssid": True, "hide_hidden": True,
    "show_24": True, "show_5": True, "show_6": True, "min_signal": -100, "sort_by": "signal",
    "unit": "m", "signal_unit": "dBm", "range": 50, "env": 3.0, "ref_1m": -40,
    "sweep_seconds": 4, "show_labels": True, "radar_mode": "heading", "demo": False,
    "share_name": False, "ask_names": True, "vpn_warn": True, "vpn_apps": [],
}
VPN_PRESETS = [("WireGuard", "com.wireguard.android"), ("OpenVPN Connect", "net.openvpn.openvpn"),
               ("Proton VPN", "ch.protonvpn.android"), ("Cloudflare WARP", "com.cloudflare.onedotonedotonedotone")]


def rgba(h):
    return get_color_from_hex(h)


def level_color(level):
    return GOOD if level >= -60 else MID if level >= -75 else BAD


# ----------------------------------------------------------------- widgets
class Card(BoxLayout):
    def __init__(self, bg=PANEL, radius=14, **kw):
        super().__init__(**kw)
        self._radius = dp(radius)
        self.set_bg(bg)
        self.bind(pos=self._upd, size=self._upd)

    def set_bg(self, hexc):
        self.canvas.before.clear()
        with self.canvas.before:
            Color(*rgba(hexc))
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[self._radius])

    def _upd(self, *_):
        self._rect.pos = self.pos
        self._rect.size = self.size


class Tile(ButtonBehavior, Card):
    pass


class TLabel(Label):
    def __init__(self, **kw):
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "middle")
        kw.setdefault("color", rgba(WHITE))
        super().__init__(**kw)
        self.bind(size=lambda i, v: setattr(i, "text_size", (v[0], v[1])))


class AutoLabel(Label):
    def __init__(self, **kw):
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "top")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("color", rgba(WHITE))
        super().__init__(**kw)
        self.bind(width=lambda i, w: setattr(i, "text_size", (w, None)))
        self.bind(texture_size=lambda i, t: setattr(i, "height", t[1] + dp(2)))


class SignalBars(Widget):
    def __init__(self, level=-100, color=GOOD, **kw):
        kw.setdefault("size_hint", (None, 1))
        kw.setdefault("width", dp(34))
        super().__init__(**kw)
        self.level, self.color = level, color
        self.bind(pos=self.draw, size=self.draw)

    def draw(self, *_):
        self.canvas.clear()
        n = bars_of(self.level)
        w, gap = dp(5), dp(3)
        x0, y0 = self.center_x - (4 * w + 3 * gap) / 2, self.center_y - dp(11)
        with self.canvas:
            for i in range(4):
                Color(*rgba(self.color if i < n else "#2B3D52"))
                Rectangle(pos=(x0 + i * (w + gap), y0), size=(w, dp(6) + i * dp(5)))


class RadarView(Widget):
    """Full circular radar. Radius = estimated distance (strong = near the centre).
    Angle = compass bearing where the signal is strongest (learned while you turn)."""
    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self.items, self.labels, self.letters = [], [], []
        self.best = None
        self.sweep = self.heading = self.phase = 0.0
        self._bt = self._it = 0.0
        for ch in "NESW":
            lb = Label(text=ch, font_size=sp(13), bold=True, color=rgba(BAD if ch == "N" else MUTED), size_hint=(None, None))
            lb.texture_update()
            lb.size = lb.texture_size
            lb.dirn = {"N": 0, "E": 90, "S": 180, "W": 270}[ch]
            self.letters.append(lb)
            self.add_widget(lb)
        self.bind(pos=self.relayout, size=self.relayout, parent=self._parent)
        self._ev = Clock.schedule_interval(self.tick, 1 / 30)

    def _parent(self, _i, p):
        if p is None and self._ev:
            self._ev.cancel()
            self._ev = None

    def geom(self):
        cx, cy = self.center
        return cx, cy, max(dp(40), min(self.width, self.height) / 2 - dp(10))

    def frac(self, d):
        return min(.96, math.log1p(d) / math.log1p(max(5, self.app.cfg["range"])))

    def ref(self):
        return self.heading if (self.app.cfg["radar_mode"] == "heading" and self.app.compass.available) else 0.0

    def set_items(self, items):
        items.sort(key=lambda x: x["key"])
        n = max(1, len(items))
        for i, it in enumerate(items):
            it["spread"] = 90 + 360.0 * i / n
            it["bearing"], it["conf"] = None, 0.0
        self.items = items
        self.best = max(items, key=lambda x: x["level"]) if items else None
        self.refresh_bearings()
        for lb in self.labels:
            self.remove_widget(lb)
        self.labels = []
        rng, unit = self.app.cfg["range"], self.app.cfg["unit"]
        for f in (.25, .5, 1):
            lb = Label(text=fmt_dist(rng * f, unit), font_size=sp(9), color=rgba(MUTED), size_hint=(None, None))
            lb.kind, lb.ring = "ring", f
            self.labels.append(lb)
        if self.app.cfg["show_labels"]:
            for it in items:
                lb = Label(text=f"{it['ssid'][:16]}\n{self.app.sig_text(it['level'])}", font_size=sp(10),
                           color=rgba(WHITE), halign="center", size_hint=(None, None))
                lb.kind, lb.item = "net", it
                self.labels.append(lb)
        for lb in self.labels:
            lb.texture_update()
            lb.size = lb.texture_size
            self.add_widget(lb)
        self.relayout()

    def refresh_bearings(self):
        for it in self.items:
            r = self.app.tracker.bearing(it["key"])
            it["bearing"], it["conf"] = (r[0], r[1]) if r else (None, 0.0)

    def angle_of(self, it):
        return 90 - (it["bearing"] - self.ref()) if it["bearing"] is not None else it["spread"]

    def blip_pos(self, it):
        cx, cy, R = self.geom()
        r, a = R * self.frac(it["dist"]), math.radians(self.angle_of(it))
        return cx + r * math.cos(a), cy + r * math.sin(a)

    def place_labels(self):
        cx, cy, R = self.geom()
        for lb in self.labels:
            if lb.kind == "ring":
                r = R * self.frac(self.app.cfg["range"] * lb.ring)
                lb.center = (cx + r * .707 + dp(12), cy + r * .707)
            else:
                x, y = self.blip_pos(lb.item)
                lb.center = (min(max(x, self.x + lb.width / 2), self.right - lb.width / 2),
                             min(max(y + dp(22), self.y + lb.height / 2), self.top - lb.height / 2))
        for lb in self.letters:
            a = math.radians(90 - (lb.dirn - self.ref()))
            lb.center = (cx + (R - dp(12)) * math.cos(a), cy + (R - dp(12)) * math.sin(a))

    def relayout(self, *_):
        self.place_labels()
        self.draw()

    def tick(self, dt):
        self.sweep = (self.sweep + dt * 360.0 / max(1, self.app.cfg["sweep_seconds"])) % 360
        self.phase += dt
        h = self.app.compass.heading
        if h is not None:
            self.heading = (self.heading + (((h - self.heading + 540) % 360) - 180) * .25) % 360
        self._bt += dt
        self._it += dt
        if self._bt > .5:
            self._bt = 0
            self.refresh_bearings()
        if self._it > 1:
            self._it = 0
            self.app.update_radar_info()
        self.place_labels()
        self.draw()

    def draw(self, *_):
        cx, cy, R = self.geom()
        self.canvas.before.clear()
        with self.canvas.before:
            Color(*rgba("#04201C"))
            Ellipse(pos=(cx - R, cy - R), size=(2 * R, 2 * R))
            Color(*rgba(ACCENT), .55)
            for f in (.25, .5, .75):
                Line(circle=(cx, cy, R * f), width=dp(1))
            Color(*rgba(ACCENT), .9)
            Line(circle=(cx, cy, R), width=dp(2.2))
            ref = self.ref()
            Color(*rgba(ACCENT), .35)
            for k in range(0, 360, 90):  # compass cross follows the real directions
                a = math.radians(90 - (k - ref))
                Line(points=[cx, cy, cx + R * math.cos(a), cy + R * math.sin(a)], width=dp(1))
            for k in range(0, 360, 30):
                a = math.radians(90 - (k - ref))
                Line(points=[cx + R * .95 * math.cos(a), cy + R * .95 * math.sin(a),
                             cx + R * math.cos(a), cy + R * math.sin(a)], width=dp(1.2))
            for k in range(0, 36, 2):
                a = math.radians(self.sweep + k * 1.6)
                Color(*rgba(ACCENT), max(0.0, .36 - k * .01))
                Line(points=[cx, cy, cx + R * math.cos(a), cy + R * math.sin(a)], width=dp(3))
            a = math.radians(self.sweep)
            Color(*rgba(ACCENT), .95)
            Line(points=[cx, cy, cx + R * math.cos(a), cy + R * math.sin(a)], width=dp(2))
            if self.app.compass.available and self.app.cfg["radar_mode"] == "heading":  # "you face this way"
                Color(*rgba(WHITE))
                Line(points=[cx - dp(7), cy + R - dp(2), cx, cy + R - dp(14), cx + dp(7), cy + R - dp(2)],
                     close=True, width=dp(1.6))
            for it in self.items:
                x, y = self.blip_pos(it)
                glow = max(0.0, 1 - ((self.angle_of(it) - self.sweep) % 360) / 140.0)
                size = dp(9) + bars_of(it["level"]) * dp(1.6)
                known = it["bearing"] is not None
                col = rgba(level_color(it["level"]))
                Color(*col[:3], (.18 + .3 * glow) * (1 if known else .5))
                Ellipse(pos=(x - size, y - size), size=(2 * size, 2 * size))
                Color(*col[:3], (.55 + .45 * glow) * (1 if known else .55))
                if known:
                    Ellipse(pos=(x - size / 2, y - size / 2), size=(size, size))
                else:
                    Line(circle=(x, y, size / 2), width=dp(1.6))
                if it.get("connected"):
                    Color(*rgba(WHITE))
                    Line(circle=(x, y, size * .9), width=dp(1.5))
            b = self.best
            if b:  # strongest signal = device (router) location
                x, y = self.blip_pos(b)
                pulse = (math.sin(self.phase * 3) + 1) / 2
                Color(*rgba(ACCENT), .9)
                if b["bearing"] is not None:
                    Line(points=[cx, cy, x, y], width=dp(1.8))
                else:
                    Line(points=[cx, cy, x, y], width=dp(1.4), dash_length=dp(4), dash_offset=dp(4))
                Color(*rgba(ACCENT), .25 + .55 * (1 - pulse))
                Line(circle=(x, y, dp(15) + pulse * dp(10)), width=dp(2))
                Color(*rgba(WHITE))
                Line(points=[x - dp(6), y, x, y + dp(6), x + dp(6), y, x, y - dp(6)], close=True, width=dp(1.6))
            Color(*rgba(WHITE))
            Ellipse(pos=(cx - dp(4), cy - dp(4)), size=(dp(8), dp(8)))

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            best, bd = None, dp(30)
            for it in self.items:
                x, y = self.blip_pos(it)
                d = math.hypot(touch.x - x, touch.y - y)
                if d < bd:
                    best, bd = it, d
            if best:
                self.app.show_network(best)
                return True
        return super().on_touch_down(touch)


# ----------------------------------------------------------------- compass
class NoCompass:
    available, heading = False, None

    def start(self):
        pass

    def stop(self):
        pass

    def set_turning(self, on):
        pass


class DemoCompass(NoCompass):
    """Simulated compass: holds still, rotates once while 'Find direction' runs."""
    available = True

    def __init__(self):
        self._base, self._t0, self.turning = 0.0, 0.0, False

    @property
    def heading(self):
        return (self._base + (time.time() - self._t0) * 18) % 360 if self.turning else self._base

    def set_turning(self, on):
        if on:
            self._t0 = time.time()
        elif self.turning:
            self._base = self.heading
        self.turning = on


if JNIUS:
    class _SensorListener(PythonJavaClass):
        __javainterfaces__ = ["android/hardware/SensorEventListener"]
        __javacontext__ = "app"

        def __init__(self, cb):
            super().__init__()
            self.cb = cb

        @java_method("(Landroid/hardware/SensorEvent;)V")
        def onSensorChanged(self, event):
            try:
                self.cb(list(event.values))
            except Exception:
                pass

        @java_method("(Landroid/hardware/Sensor;I)V")
        def onAccuracyChanged(self, sensor, accuracy):
            pass

    class _NsdListener(PythonJavaClass):
        __javainterfaces__ = ["android/net/nsd/NsdManager$RegistrationListener"]
        __javacontext__ = "app"

        @java_method("(Landroid/net/nsd/NsdServiceInfo;I)V")
        def onRegistrationFailed(self, info, code):
            pass

        @java_method("(Landroid/net/nsd/NsdServiceInfo;I)V")
        def onUnregistrationFailed(self, info, code):
            pass

        @java_method("(Landroid/net/nsd/NsdServiceInfo;)V")
        def onServiceRegistered(self, info):
            pass

        @java_method("(Landroid/net/nsd/NsdServiceInfo;)V")
        def onServiceUnregistered(self, info):
            pass


class AndroidCompass(NoCompass):
    def __init__(self):
        self.heading, self.available, self.on = None, False, False
        try:
            from jnius import autoclass
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            self.sm = act.getSystemService(autoclass("android.content.Context").SENSOR_SERVICE)
            self.sensor = self.sm.getDefaultSensor(11)  # TYPE_ROTATION_VECTOR
            if self.sensor is not None and JNIUS:
                self.listener = _SensorListener(self._on)
                self.available = True
        except Exception:
            self.available = False

    def _on(self, values):
        h = heading_from_rotation(values)
        if h is not None:
            self.heading = h

    def start(self):
        if self.available and not self.on:
            try:
                self.sm.registerListener(self.listener, self.sensor, 2)
                self.on = True
            except Exception:
                pass

    def stop(self):
        if self.on:
            try:
                self.sm.unregisterListener(self.listener)
            except Exception:
                pass
        self.on, self.heading = False, None


# ---------------------------------------------------------------- backends
class AndroidWifi:
    demo = False

    def __init__(self):
        from jnius import autoclass
        self.ac = autoclass
        self.act = autoclass("org.kivy.android.PythonActivity").mActivity
        self.ctx = autoclass("android.content.Context")
        self.wm = self.act.getApplicationContext().getSystemService(self.ctx.WIFI_SERVICE)
        self.sdk = autoclass("android.os.Build$VERSION").SDK_INT
        self._nsd = None

    def enabled(self):
        return bool(self.wm.isWifiEnabled())

    def start_scan(self):
        try:
            self.wm.startScan()
        except Exception:
            pass

    def results(self):
        out = []
        for it in self.wm.getScanResults():
            out.append({"ssid": str(it.SSID or ""), "bssid": str(it.BSSID or ""), "level": int(it.level),
                        "freq": int(it.frequency), "caps": str(it.capabilities or ""), "ts": int(it.timestamp)})
        return out

    def rssi(self):
        try:
            return int(self.wm.getConnectionInfo().getRssi())
        except Exception:
            return None

    def current(self):
        try:
            info = self.wm.getConnectionInfo()
            ssid, rssi = str(info.getSSID() or "").strip('"'), int(info.getRssi())
            if rssi <= -127 or ssid in ("", "<unknown ssid>", "0x"):
                return None
            return {"ssid": ssid, "bssid": str(info.getBSSID() or ""), "level": rssi, "speed": int(info.getLinkSpeed()),
                    "freq": int(info.getFrequency()), "ip": int_to_ip(info.getIpAddress())}
        except Exception:
            return None

    def dhcp(self):
        try:
            d = self.wm.getDhcpInfo()
            if not d.ipAddress:
                return None
            return {"ip": int_to_ip(d.ipAddress), "gateway": int_to_ip(d.gateway), "mask": int_to_ip(d.netmask)}
        except Exception:
            return None

    def device_name(self):
        try:
            n = self.ac("android.provider.Settings$Global").getString(self.act.getContentResolver(), "device_name")
            if n:
                return str(n)
        except Exception:
            pass
        try:
            return str(self.ac("android.os.Build").MODEL)
        except Exception:
            return "This phone"

    def share_name(self, on):
        """Announce this phone's name on the network (mDNS/NSD). Only when the user allows it."""
        try:
            if on and self._nsd is None:
                srv = socket.socket()
                srv.bind(("", 0))
                srv.listen(2)

                def loop():
                    while True:
                        try:
                            c, _ = srv.accept()
                            c.close()
                        except Exception:
                            break
                threading.Thread(target=loop, daemon=True).start()
                info = self.ac("android.net.nsd.NsdServiceInfo")()
                info.setServiceName(self.device_name())
                info.setServiceType("_wifiscan._tcp.")
                info.setPort(srv.getsockname()[1])
                mgr = self.act.getSystemService(self.ctx.NSD_SERVICE)
                lis = _NsdListener()
                mgr.registerService(info, 1, lis)
                self._nsd = (mgr, lis, srv)
            elif not on and self._nsd:
                mgr, lis, srv = self._nsd
                self._nsd = None
                mgr.unregisterService(lis)
                srv.close()
            return True
        except Exception:
            return False

    def open_panel(self):
        Intent = self.ac("android.content.Intent")
        try:
            self.act.startActivity(Intent("android.settings.panel.action.WIFI"))
        except Exception:
            self.act.startActivity(Intent("android.settings.WIFI_SETTINGS"))

    def vpn_active(self):
        try:
            cm = self.act.getSystemService(self.ctx.CONNECTIVITY_SERVICE)
            for n in cm.getAllNetworks():
                caps = cm.getNetworkCapabilities(n)
                if caps is not None and caps.hasTransport(4):  # TRANSPORT_VPN
                    return True
        except Exception:
            pass
        return False

    def open_vpn_settings(self):
        self.act.startActivity(self.ac("android.content.Intent")("android.settings.VPN_SETTINGS"))

    def launch_app(self, pkg):
        Intent = self.ac("android.content.Intent")
        it = self.act.getPackageManager().getLaunchIntentForPackage(pkg)
        if it is not None:
            self.act.startActivity(it)
            return "launched"
        Uri = self.ac("android.net.Uri")
        self.act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=" + pkg)))
        return "store"

    def connect(self, ssid, password, security):
        if security in ("Enterprise", "WEP"):
            self.open_panel()
            return False, f"{security} networks cannot be added by apps. Android's Wi-Fi panel was opened instead."
        if self.sdk >= 29:
            B = self.ac("android.net.wifi.WifiNetworkSuggestion$Builder")
            b = B().setSsid(ssid)
            if security == "WPA3":
                b = b.setWpa3Passphrase(password)
            elif security != "Open":
                b = b.setWpa2Passphrase(password)
            lst = self.ac("java.util.ArrayList")()
            lst.add(b.build())
            status = int(self.wm.addNetworkSuggestions(lst))
            if status in (0, 3):
                return True, ("Network added. Android may show a notification asking you to allow it; after you "
                              "approve, the phone connects automatically when the network is in range.")
            msgs = {2: "Too many saved suggestions from this app.", 4: "Android blocked this app from adding networks.",
                    1: "Android could not add the network."}
            self.open_panel()
            return False, msgs.get(status, f"Android refused (code {status}).") + " Wi-Fi panel opened instead."
        WC = self.ac("android.net.wifi.WifiConfiguration")
        KM = self.ac("android.net.wifi.WifiConfiguration$KeyMgmt")
        cfg = WC()
        cfg.SSID = '"%s"' % ssid
        if security == "Open":
            cfg.allowedKeyManagement.set(KM.NONE)
        else:
            cfg.preSharedKey = '"%s"' % password
        nid = int(self.wm.addNetwork(cfg))
        if nid == -1:
            return False, "Android could not add this network."
        self.wm.disconnect()
        self.wm.enableNetwork(nid, True)
        self.wm.reconnect()
        return True, "Connecting... this can take a few seconds."


class DemoWifi:
    demo = True
    NETS = [("Home-WiFi", "WPA2", 2437, -48, 40), ("Home-WiFi-5G", "WPA2/WPA3", 5180, -55, 45),
            ("Neighbour_2G", "WPA2", 2412, -72, 200), ("CoffeeShop Free", "Open", 2462, -66, 300),
            ("Office-Net", "Enterprise", 5745, -80, 150), ("TP-Link_A1B2", "WPA2", 2447, -84, 260),
            ("Phone Hotspot", "WPA3", 5220, -61, 100), ("Guest", "WPA2", 2437, -90, 340)]

    def __init__(self, compass):
        self.compass = compass
        self.vpn = False

    def _lvl(self, base, bearing):
        h = self.compass.heading or 0.0
        return int(base + 7 * math.cos(math.radians(h - bearing)) + random.uniform(-1, 1))

    def enabled(self):
        return True

    def start_scan(self):
        pass

    def results(self):
        out = []
        for i, (s, sec, f, lv, br) in enumerate(self.NETS):
            caps = {"Open": "[ESS]", "Enterprise": "[WPA2-EAP-CCMP][ESS]", "WPA3": "[RSN-SAE-CCMP][ESS]",
                    "WPA2/WPA3": "[RSN-PSK+SAE-CCMP][ESS]"}.get(sec, "[WPA2-PSK-CCMP][ESS]")
            out.append({"ssid": s, "bssid": "aa:bb:cc:00:00:%02x" % i, "level": self._lvl(lv, br), "freq": f,
                        "caps": caps, "ts": int(time.time() * 1e6)})
        return out

    def rssi(self):
        return self._lvl(-48, 40)

    def current(self):
        return {"ssid": "Home-WiFi", "bssid": "aa:bb:cc:00:00:00", "level": self._lvl(-48, 40), "speed": 433,
                "freq": 2437, "ip": "192.168.1.23"}

    def dhcp(self):
        return {"ip": "192.168.1.23", "gateway": "192.168.1.1", "mask": "255.255.255.0"}

    def device_name(self):
        return "My Phone"

    def share_name(self, on):
        return True

    def open_panel(self):
        pass

    def vpn_active(self):
        return self.vpn

    def open_vpn_settings(self):
        self.vpn = not self.vpn  # demo: toggles the status so you can see both states

    def launch_app(self, pkg):
        return "launched"

    def connect(self, ssid, password, security):
        return True, "Demo mode: pretending to connect to " + ssid + "."


# -------------------------------------------------------------------- app
class WifiMiniApp(App):
    def build(self):
        self.title = "Wi-Fi Mini Scanner"
        Window.clearcolor = rgba(BG)
        self.cfg_path = os.path.join(self.user_data_dir, "wifi_settings.json")
        self.cfg = dict(DEFAULTS)
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                self.cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except Exception:
            pass
        self.cfg["vpn_apps"] = list(self.cfg.get("vpn_apps") or [])
        self.backend, self.compass, self.tracker = None, NoCompass(), DirectionTracker()
        self.networks, self.current, self.last_ts = [], None, {}
        self.devices, self.dev_busy, self.dev_text = {}, False, "Tap Find devices to scan this Wi-Fi."
        self.scanning, self.last_scan, self.clock_s, self.sweep_until = False, -999, 0, 0
        self.vpn_on = False
        self.status_text = "Starting..."
        self.status_label = self.radar_info = self.dev_label = self.dev_box = None
        self.screen, self.radar, self.track = "home", None, None
        self.net_box = self.cur_box = self.net_sv = self.vpn_tile = None
        root = BoxLayout(orientation="vertical")
        self.body = BoxLayout()
        root.add_widget(self.body)
        nav = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(3), padding=(dp(4), dp(3)))
        self.nav_tiles = {}
        for key, label in (("home", "Home"), ("radar", "Radar"), ("devices", "Devices"), ("vpn", "VPN"),
                           ("settings", "Settings")):
            t = Tile(bg="#0A1626", radius=10)
            t.lbl = TLabel(text=label, halign="center", font_size=sp(11), color=rgba(MUTED))
            t.add_widget(t.lbl)
            t.bind(on_release=lambda _i, k=key: self.go(k))
            nav.add_widget(t)
            self.nav_tiles[key] = t
        root.add_widget(nav)
        self.go("home")
        Clock.schedule_once(lambda *_: self.start_backend(), .4)
        Clock.schedule_interval(self.tick, 1)
        Clock.schedule_interval(self.sample, .5)
        return root

    # -------------------------------------------------------- basic helpers
    def save_cfg(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f)
        except Exception:
            pass

    def btn(self, text, cb=None, bg=PANEL2, fg=WHITE, fs=14, **kw):
        t = Tile(bg=bg, padding=dp(4), **kw)
        t.lbl = TLabel(text=text, halign="center", color=rgba(fg), font_size=sp(fs), bold=True)
        t.add_widget(t.lbl)
        if cb:
            t.bind(on_release=cb)
        return t

    def notice(self, title, message):
        box = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(12))
        box.add_widget(TLabel(text=message, halign="center", font_size=sp(14)))
        ok = self.btn("OK", None, bg=ACCENT, fg=BG, size_hint_y=None, height=dp(44))
        box.add_widget(ok)
        pop = Popup(title=title, content=box, size_hint=(.88, .5), background_color=rgba(PANEL))
        ok.bind(on_release=pop.dismiss)
        pop.open()

    def sig_text(self, level):
        return f"{level} dBm" if self.cfg["signal_unit"] == "dBm" else f"{percent_of(level)}%"

    def set_status(self, text):
        self.status_text = text
        if self.status_label is not None:
            self.status_label.text = text

    def go(self, name):
        self.screen = name
        self.status_label = self.net_box = self.cur_box = self.net_sv = self.radar = None
        self.radar_info = self.dev_label = self.dev_box = self.vpn_tile = None
        self.body.clear_widgets()
        if name == "radar":
            self.compass.start()
        else:
            self.compass.stop()
            if self.sweep_until:
                self.finish_sweep(False)
        getattr(self, "build_" + name)()
        for k, t in self.nav_tiles.items():
            t.lbl.color = rgba(ACCENT if k == name else MUTED)
        if name in ("home", "radar"):
            self.refresh_screen()

    def header(self, title, scan=True):
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        row.add_widget(TLabel(text=f"[b]{title}[/b]", markup=True, font_size=sp(21)))
        if scan:
            row.add_widget(self.btn("Scan", lambda *_: self.scan(True), bg=ACCENT, fg=BG, size_hint=(None, 1), width=dp(80)))
        return row

    def page(self):
        p = BoxLayout(orientation="vertical", padding=(dp(12), dp(8)), spacing=dp(8))
        self.body.add_widget(p)
        return p

    # ----------------------------------------------------------- backend
    def start_backend(self):
        use_demo = self.cfg["demo"] or not ANDROID
        if not use_demo:
            try:
                self.backend = AndroidWifi()
                self.compass = AndroidCompass()
            except Exception:
                use_demo = True
        if use_demo:
            self.compass = DemoCompass()
            self.backend = DemoWifi(self.compass)
            self.set_status("Demo mode - sample networks (Android Wi-Fi access not available here).")
            self.scan(True)
            return
        if self.screen == "radar":
            self.compass.start()
        try:
            from android.permissions import request_permissions
            request_permissions(["android.permission.ACCESS_FINE_LOCATION", "android.permission.NEARBY_WIFI_DEVICES"],
                                lambda p, g: Clock.schedule_once(lambda *_: self.on_perms(g), 0))
        except Exception:
            self.scan(True)
        if self.cfg["share_name"]:
            self.backend.share_name(True)

    def on_perms(self, grants):
        if grants and any(grants):
            self.scan(True)
        else:
            self.set_status("Permission not granted. Allow Location / Nearby devices for this app in Android Settings.")

    def tick(self, _dt):
        self.clock_s += 1
        if self.sweep_until:
            left = int(self.sweep_until - time.time())
            if left <= 0:
                self.finish_sweep(True)
            else:
                self.set_status(f"Turn slowly in a full circle... {left}s")
                if self.clock_s - self.last_scan >= (1 if (self.backend and self.backend.demo) else 3):
                    self.scan(False)
            return
        if self.track:
            if self.clock_s - self.last_scan >= 4:
                self.scan(False)
        elif self.cfg["auto_scan"] and self.screen in ("home", "radar") and \
                self.clock_s - self.last_scan >= self.cfg["scan_interval"]:
            self.scan(False)

    def sample(self, _dt):
        """Every 0.5 s: log (compass heading, signal) of the connected network for direction finding."""
        h = self.compass.heading
        if h is None or self.screen != "radar" or not self.backend or not self.current:
            return
        lvl = self.backend.rssi()
        if lvl is not None and lvl > -127:
            cur = self.current
            self.tracker.add(cur["ssid"] if self.cfg["group_ssid"] else cur["bssid"], h, lvl)

    def scan(self, manual=False):
        if self.backend is None or self.scanning:
            return
        try:
            if not self.backend.enabled():
                self.networks = []
                self.set_status("Wi-Fi is turned off. Turn it on to see networks.")
                self.refresh_screen()
                if manual:
                    self.backend.open_panel()
                return
        except Exception:
            pass
        self.scanning = True
        self.last_scan = self.clock_s
        if not self.sweep_until:
            self.set_status("Scanning...")
        self.backend.start_scan()
        Clock.schedule_once(self.read_scan, .3 if self.backend.demo else 1.5)

    def read_scan(self, *_):
        self.scanning = False
        try:
            raw = self.backend.results()
            self.current = self.backend.current()
            self.vpn_on = self.backend.vpn_active()
        except Exception as exc:
            self.set_status("Could not read scan results: " + str(exc))
            return
        self.networks = self.process(raw)
        h = self.compass.heading
        if h is not None:
            for it in self.networks:
                if self.last_ts.get(it["key"]) != it["ts"]:
                    self.last_ts[it["key"]] = it["ts"]
                    self.tracker.add(it["key"], h, it["level"])
        if not self.sweep_until:
            self.set_status(f"{len(self.networks)} network(s) - updated {time.strftime('%H:%M:%S')}" if self.networks
                            else "No networks found. Check permissions and that Location services are on, then scan again.")
        self.refresh_screen()
        self.update_track()

    def process(self, raw):
        c, cur = self.cfg, self.current or {}
        uniq = {}
        for r in raw:  # one entry per access point address
            k = r["bssid"] or r["ssid"]
            if k not in uniq or r["level"] > uniq[k]["level"]:
                uniq[k] = r
        items = []
        for r in uniq.values():
            ssid = r["ssid"].strip()
            if not ssid and c["hide_hidden"]:
                continue
            band = band_of(r["freq"])
            if not c["show_" + {"2.4": "24", "5": "5", "6": "6"}[band]] or r["level"] < c["min_signal"]:
                continue
            hidden = not ssid
            items.append({"ssid": ssid or "(hidden network)", "bssid": r["bssid"] or ssid, "level": r["level"],
                          "freq": r["freq"], "band": band, "channel": channel_of(r["freq"]), "ts": r.get("ts", 0),
                          "security": parse_security(r["caps"]), "aps": 1,
                          "dist": estimate_distance(r["level"], c["env"], c["ref_1m"]),
                          "key": (ssid if (c["group_ssid"] and not hidden) else (r["bssid"] or ssid)),
                          "connected": bool(cur) and ssid == cur.get("ssid") and
                                       (c["group_ssid"] or not cur.get("bssid") or r["bssid"] == cur.get("bssid"))})
        if c["group_ssid"]:
            best = {}
            for it in items:
                if it["key"] in best:
                    best[it["key"]]["aps"] += 1
                    if it["level"] > best[it["key"]]["level"]:
                        it["aps"] = best[it["key"]]["aps"]
                        best[it["key"]] = it
                else:
                    best[it["key"]] = it
            items = list(best.values())
        key = {"signal": lambda x: -x["level"], "name": lambda x: x["ssid"].lower(),
               "security": lambda x: (x["security"] == "Open", -x["level"])}[c["sort_by"]]
        items.sort(key=key)
        return items

    def refresh_screen(self):
        if self.screen == "home" and self.net_box is not None:
            self.fill_home()
        elif self.screen == "radar" and self.radar is not None:
            self.radar.set_items([dict(n) for n in self.networks])
            self.update_radar_info()
        elif self.screen == "vpn":
            self.go("vpn")

    # --------------------------------------------------------------- home
    def build_home(self):
        p = self.page()
        hdr = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        hdr.add_widget(TLabel(text="[b]Wi-Fi Scanner[/b]", markup=True, font_size=sp(22)))
        hdr.add_widget(self.btn("Scan", lambda *_: self.scan(True), bg=ACCENT, fg=BG, size_hint=(None, 1), width=dp(80)))
        p.add_widget(hdr)
        self.status_label = TLabel(text=self.status_text, font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(22))
        p.add_widget(self.status_label)
        self.cur_box = BoxLayout(size_hint_y=None, height=dp(138))
        p.add_widget(self.cur_box)
        quick = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        quick.add_widget(self.btn("Radar", lambda *_: self.go("radar"), bg="#123B4A", fg=ACCENT))
        quick.add_widget(self.btn("Devices", lambda *_: self.go("devices"), bg="#16314F", fg=BLUE))
        self.vpn_tile = self.btn("VPN", lambda *_: self.go("vpn"), bg=PANEL2)
        quick.add_widget(self.vpn_tile)
        p.add_widget(quick)
        p.add_widget(TLabel(text="[b]Nearby networks[/b]", markup=True, font_size=sp(15), size_hint_y=None, height=dp(28)))
        self.net_sv = ScrollView(do_scroll_x=False, bar_width=dp(3))
        self.net_box = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        self.net_box.bind(minimum_height=self.net_box.setter("height"))
        self.net_sv.add_widget(self.net_box)
        p.add_widget(self.net_sv)

    def fill_home(self):
        self.vpn_tile.lbl.text = "VPN: " + ("ON" if self.vpn_on else "OFF")
        self.vpn_tile.lbl.color = rgba(GOOD if self.vpn_on else MUTED)
        self.cur_box.clear_widgets()
        cur = self.current
        card = Card(bg="#10343A" if cur else PANEL, orientation="vertical", padding=dp(12), spacing=dp(6))
        top = BoxLayout(spacing=dp(12), size_hint_y=.55)
        if cur:
            top.add_widget(SignalBars(cur["level"], level_color(cur["level"]), width=dp(44)))
            col = BoxLayout(orientation="vertical")
            col.add_widget(TLabel(text=f"[b]{cur['ssid']}[/b]", markup=True, font_size=sp(19)))
            col.add_widget(TLabel(text="Connected - " + ["Poor", "Weak", "Fair", "Good", "Excellent"][bars_of(cur["level"])] + " signal",
                                  font_size=sp(12), color=rgba(GOOD)))
            top.add_widget(col)
            card.add_widget(top)
            stats = BoxLayout(size_hint_y=.45)
            for val, lab in ((self.sig_text(cur["level"]), "Signal"), (f"{cur['speed']} Mbps", "Speed"),
                             (f"{band_of(cur['freq'])} GHz", "Band"), (cur["ip"], "IP")):
                c = BoxLayout(orientation="vertical")
                c.add_widget(TLabel(text=f"[b]{val}[/b]", markup=True, halign="center", font_size=sp(12)))
                c.add_widget(TLabel(text=lab, halign="center", font_size=sp(10), color=rgba(MUTED)))
                stats.add_widget(c)
            card.add_widget(stats)
        else:
            col = BoxLayout(orientation="vertical")
            col.add_widget(TLabel(text="[b]Not connected[/b]", markup=True, font_size=sp(19)))
            col.add_widget(TLabel(text="Tap a network below to connect.", font_size=sp(12), color=rgba(MUTED)))
            top.add_widget(col)
            card.add_widget(top)
        if cur and self.cfg["vpn_warn"] and not self.vpn_on:
            sec = next((n["security"] for n in self.networks if n["connected"]), "")
            if sec in ("Open", "WEP"):
                card.add_widget(TLabel(text="This network is not encrypted. Use a VPN.", font_size=sp(11),
                                       color=rgba(BAD), size_hint_y=None, height=dp(18)))
        self.cur_box.add_widget(card)
        y = self.net_sv.scroll_y
        self.net_box.clear_widgets()
        if not self.networks:
            self.net_box.add_widget(AutoLabel(text="No networks to show yet.", color=rgba(MUTED)))
        strongest = max(self.networks, key=lambda n: n["level"]) if self.networks else None
        for it in self.networks:
            row = Tile(bg="#10343A" if it["connected"] else PANEL, size_hint_y=None, height=dp(78), padding=dp(10), spacing=dp(10))
            row.add_widget(SignalBars(it["level"], level_color(it["level"])))
            col = BoxLayout(orientation="vertical")
            name = it["ssid"] + ("   BEST" if it is strongest and not it["connected"] else "")
            col.add_widget(TLabel(text=f"[b]{name}[/b]", markup=True, font_size=sp(15)))
            sec_col = BAD if it["security"] in ("Open", "WEP") else GOOD
            aps = f"  |  {it['aps']} APs" if it["aps"] > 1 else ""
            col.add_widget(TLabel(text=f"[color={sec_col[1:]}]{it['security']}[/color]  |  {it['band']} GHz ch {it['channel']}{aps}",
                                  markup=True, font_size=sp(11), color=rgba(MUTED)))
            col.add_widget(TLabel(text="Connected" if it["connected"] else "Tap to connect", font_size=sp(10),
                                  color=rgba(ACCENT if it["connected"] else MUTED)))
            row.add_widget(col)
            right = BoxLayout(orientation="vertical", size_hint_x=None, width=dp(78))
            right.add_widget(TLabel(text=f"[b]{self.sig_text(it['level'])}[/b]", markup=True, halign="right",
                                    font_size=sp(13), color=rgba(level_color(it["level"]))))
            right.add_widget(TLabel(text="~" + fmt_dist(it["dist"], self.cfg["unit"]), halign="right",
                                    font_size=sp(11), color=rgba(MUTED)))
            row.add_widget(right)
            row.bind(on_release=lambda _i, it=it: self.show_network(it))
            self.net_box.add_widget(row)
        Clock.schedule_once(lambda *_: setattr(self.net_sv, "scroll_y", y), 0)

    # ---------------------------------------------------- network details
    def show_network(self, it):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(6))
        pop = Popup(title=it["ssid"], content=box, size_hint=(.92, .78), background_color=rgba(PANEL))
        bearing = self.tracker.bearing(it["key"])
        rows = [("Signal", f"{self.sig_text(it['level'])}  ({bars_of(it['level'])}/4 bars)"),
                ("Estimated distance", "~" + fmt_dist(it["dist"], self.cfg["unit"]) + " (approx.)"),
                ("Direction", f"{bearing[0]:.0f} deg {compass_text(bearing[0])} ({conf_text(bearing[1])})" if bearing
                 else "unknown - use Find direction on Radar"),
                ("Security", it["security"]), ("Band / channel", f"{it['band']} GHz / {it['channel']}"),
                ("Address (BSSID)", it["bssid"])]
        for k, v in rows:
            r = BoxLayout(size_hint_y=None, height=dp(30))
            r.add_widget(TLabel(text=k, color=rgba(MUTED), font_size=sp(12)))
            r.add_widget(TLabel(text=str(v), halign="right", font_size=sp(12)))
            box.add_widget(r)
        box.add_widget(Widget())
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        if not it["connected"]:
            row.add_widget(self.btn("Connect", lambda *_: (pop.dismiss(), self.connect_flow(it)), bg=ACCENT, fg=BG))
        row.add_widget(self.btn("Find it (live)", lambda *_: (pop.dismiss(), self.open_track(it)), bg=BLUE))
        box.add_widget(row)
        box.add_widget(self.btn("Close", pop.dismiss, size_hint_y=None, height=dp(42)))
        pop.open()

    def connect_flow(self, it):
        sec = it["security"]
        if sec in ("Open", "Enterprise", "WEP"):
            self.do_connect(it, "")
            return
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        pop = Popup(title=f"Connect to {it['ssid']}", content=box, size_hint=(.92, .5), background_color=rgba(PANEL))
        pw = TextInput(hint_text="Wi-Fi password", password=True, multiline=False, background_normal="",
                       background_active="", background_color=rgba(PANEL2), foreground_color=rgba(WHITE),
                       hint_text_color=rgba(MUTED), cursor_color=rgba(ACCENT), padding=(dp(12), dp(12)), write_tab=False)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        show = self.btn("Show", None, fs=12, size_hint_x=None, width=dp(64))
        show.bind(on_release=lambda *_: (setattr(pw, "password", not pw.password),
                                         setattr(show.lbl, "text", "Hide" if not pw.password else "Show")))
        row.add_widget(pw)
        row.add_widget(show)
        box.add_widget(row)
        box.add_widget(TLabel(text=f"Security: {sec}. The password is sent only to Android and is not saved by this app.",
                              font_size=sp(11), color=rgba(MUTED), size_hint_y=None, height=dp(40)))

        def go(*_):
            if len(pw.text) < 8:
                self.notice("Password", "Wi-Fi passwords are at least 8 characters.")
                return
            pop.dismiss()
            self.do_connect(it, pw.text)
        pw.bind(on_text_validate=go)
        box.add_widget(self.btn("Connect", go, bg=ACCENT, fg=BG, size_hint_y=None, height=dp(48)))
        box.add_widget(self.btn("Cancel", pop.dismiss, size_hint_y=None, height=dp(42)))
        pop.open()

    def do_connect(self, it, password):
        try:
            ok, msg = self.backend.connect(it["ssid"], password, it["security"])
        except Exception as exc:
            ok, msg = False, "Connect failed: " + str(exc)
        self.notice("Connecting" if ok else "Could not connect", msg)
        Clock.schedule_once(lambda *_: self.scan(True), 4)

    # ----------------------------------------------------------- live find
    def open_track(self, it):
        box = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
        pop = Popup(title=f"Find: {it['ssid']}", content=box, size_hint=(.92, .6), background_color=rgba(PANEL),
                    on_dismiss=lambda *_: setattr(self, "track", None))
        big = TLabel(text="--", halign="center", font_size=sp(38), bold=True, size_hint_y=None, height=dp(60))
        dist = TLabel(text="", halign="center", font_size=sp(14), color=rgba(MUTED), size_hint_y=None, height=dp(28))
        trend = TLabel(text="Walk around slowly. The stronger the signal, the closer you are.", halign="center",
                       font_size=sp(13), size_hint_y=None, height=dp(52))
        barbg = Card(bg=PANEL2, size_hint_y=None, height=dp(22), radius=11)
        fill = Card(bg=GOOD, size_hint=(None, 1), width=0, radius=11)
        barbg.add_widget(fill)
        for w in (big, dist, barbg, trend):
            box.add_widget(w)
        box.add_widget(Widget())
        box.add_widget(self.btn("Close", pop.dismiss, size_hint_y=None, height=dp(44)))
        self.track = {"key": it["key"], "big": big, "dist": dist, "trend": trend, "bg": barbg, "fill": fill,
                      "prev": it["level"]}
        pop.open()
        self.update_track(it)
        self.scan(True)

    def update_track(self, base=None):
        t = self.track
        if not t:
            return
        it = base or next((n for n in self.networks if n["key"] == t["key"]), None)
        if not it:
            t["big"].text = "Lost"
            return
        t["big"].text = self.sig_text(it["level"])
        t["big"].color = rgba(level_color(it["level"]))
        t["dist"].text = "about " + fmt_dist(it["dist"], self.cfg["unit"]) + " away (approx.)"
        diff = it["level"] - t["prev"]
        if base is None:
            t["trend"].text = ("Warmer - getting closer" if diff >= 2 else "Colder - moving away" if diff <= -2
                               else "About the same")
        t["prev"] = it["level"]
        t["fill"].set_bg(level_color(it["level"]))
        t["fill"].width = max(dp(8), t["bg"].width * percent_of(it["level"]) / 100.0)

    # -------------------------------------------------------------- radar
    def build_radar(self):
        p = self.page()
        hdr = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        hdr.add_widget(TLabel(text="[b]Wi-Fi Radar[/b]", markup=True, font_size=sp(21)))
        hdr.add_widget(self.btn("Find direction", lambda *_: self.begin_sweep(), bg=BLUE, fs=12, size_hint=(None, 1), width=dp(112)))
        hdr.add_widget(self.btn("Scan", lambda *_: self.scan(True), bg=ACCENT, fg=BG, size_hint=(None, 1), width=dp(66)))
        p.add_widget(hdr)
        self.status_label = TLabel(text=self.status_text, font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(22))
        p.add_widget(self.status_label)
        self.radar = RadarView(self)
        p.add_widget(self.radar)
        self.radar_info = TLabel(text="", font_size=sp(12), size_hint_y=None, height=dp(66))
        p.add_widget(self.radar_info)
        p.add_widget(TLabel(text="Closer to the centre = stronger signal = nearer. Direction comes from the compass while "
                                 "you turn. Dim hollow dots: direction not measured yet. Tap a dot for details.",
                            font_size=sp(10), color=rgba(MUTED), size_hint_y=None, height=dp(40)))

    def update_radar_info(self):
        if self.screen != "radar" or self.radar_info is None or self.radar is None:
            return
        b, lines = self.radar.best, []
        if b:
            lines.append(f"Strongest: {b['ssid']}  {self.sig_text(b['level'])}  ~{fmt_dist(b['dist'], self.cfg['unit'])}")
            if b["bearing"] is not None:
                lines.append(f"It is toward {b['bearing']:.0f} deg {compass_text(b['bearing'])} ({conf_text(b['conf'])} confidence)")
            else:
                lines.append("Direction unknown - tap Find direction and turn slowly.")
        else:
            lines.append("No networks yet - tap Scan.")
        h = self.compass.heading
        if h is not None:
            lines.append(f"You face {h:.0f} deg {compass_text(h)}")
        elif not self.compass.available:
            lines.append("No compass available - directions can't be measured, distance still works.")
        self.radar_info.text = "\n".join(lines)

    def begin_sweep(self):
        if not self.compass.available:
            self.notice("Direction", "This phone has no usable compass sensor, so direction can't be measured. "
                                     "Distance on the radar still works.")
            return
        self.compass.start()
        self.tracker.reset()
        self.last_ts = {}
        self.sweep_until = time.time() + 22
        self.compass.set_turning(True)
        self.set_status("Turn slowly in a full circle... 22s")
        self.scan(False)

    def finish_sweep(self, notify):
        self.sweep_until = 0
        self.compass.set_turning(False)
        if self.radar is not None:
            self.radar.refresh_bearings()
        if notify:
            known = sum(1 for n in self.networks if self.tracker.bearing(n["key"]))
            self.set_status(f"Direction found for {known} of {len(self.networks)} network(s).")
            self.update_radar_info()
            if known == 0:
                self.notice("Direction", "Not enough readings. Turn more slowly through a full circle and try again. "
                                         "Android limits how often other networks can be re-measured.")

    # ------------------------------------------------------------ devices
    def build_devices(self):
        p = self.page()
        p.add_widget(self.header("Devices on this Wi-Fi", scan=False))
        d = self.backend.dhcp() if self.backend else None
        p.add_widget(TLabel(text=f"This phone: {d['ip']}    Router: {d['gateway']}" if d else "Connect to a Wi-Fi network first.",
                            font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(24)))
        p.add_widget(self.btn("Find devices", lambda *_: self.scan_devices(), bg=ACCENT, fg=BG, size_hint_y=None, height=dp(48)))
        self.dev_label = TLabel(text=self.dev_text, font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(40))
        p.add_widget(self.dev_label)
        sv = ScrollView(do_scroll_x=False)
        self.dev_box = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        self.dev_box.bind(minimum_height=self.dev_box.setter("height"))
        sv.add_widget(self.dev_box)
        p.add_widget(sv)
        self.fill_devices()

    def add_device(self, r):
        """Each IP address appears exactly once; later info only fills in missing fields."""
        cur = self.devices.get(r["ip"])
        if cur is None:
            self.devices[r["ip"]] = r
        else:
            for k, v in r.items():
                if v and not cur.get(k):
                    cur[k] = v

    def fill_devices(self):
        if self.screen != "devices" or self.dev_box is None:
            return
        self.dev_label.text = self.dev_text
        self.dev_box.clear_widgets()

        def order(dv):
            return (0 if dv.get("tag") == "you" else 1 if dv.get("tag") == "router" else 2,
                    tuple(int(v) for v in dv["ip"].split(".")))
        for dv in sorted(self.devices.values(), key=order):
            r = Tile(bg="#10343A" if dv.get("tag") == "you" else PANEL, size_hint_y=None, height=dp(80), padding=dp(10))
            col = BoxLayout(orientation="vertical")
            title = dv.get("name") or "Unknown device"
            if dv.get("tag"):
                title += f"  ({dv['tag']})"
            col.add_widget(TLabel(text=f"[b]{title}[/b]", markup=True, font_size=sp(15)))
            extra = [dv["ip"]]
            if dv.get("mac"):
                extra.append(dv["mac"])
            if dv.get("rtt"):
                extra.append(f"{dv['rtt']} ms")
            col.add_widget(TLabel(text="  |  ".join(extra), font_size=sp(11), color=rgba(MUTED)))
            notes = [x for x in (device_hint(dv.get("ports"), dv.get("name")),
                                 f"name via {dv['src']}" if dv.get("src") else "") if x]
            if not dv.get("name") and dv.get("tag") != "you":
                notes.append("name hidden by device")
            col.add_widget(TLabel(text="  |  ".join(notes), font_size=sp(10), color=rgba(ACCENT)))
            r.add_widget(col)
            r.bind(on_release=lambda _i, ip=dv["ip"]: (Clipboard.copy(ip), self.set_dev_text(f"Copied {ip}")))
            self.dev_box.add_widget(r)

    def set_dev_text(self, t):
        self.dev_text = t
        if self.screen == "devices" and self.dev_label is not None:
            self.dev_label.text = t

    def scan_devices(self):
        if self.dev_busy:
            return
        d = self.backend.dhcp() if self.backend else None
        if not d:
            self.notice("Devices", "You need to be connected to a Wi-Fi network to find its devices.")
            return
        self.dev_busy = True
        self.devices = {}
        hosts = [h for h in subnet_hosts(d["ip"], d["mask"]) if h != d["ip"]]
        demo, ask, me, gw = self.backend.demo, self.cfg["ask_names"], d["ip"], d["gateway"]
        self.add_device({"ip": me, "name": self.backend.device_name(), "tag": "you", "src": "this phone"})
        self.set_dev_text(f"Scanning {len(hosts)} addresses...")
        self.fill_devices()

        def probe(ip):
            if demo:
                time.sleep(random.random() * .6)
                if random.random() < .3 or ip == gw:
                    nm = "Router" if ip == gw else random.choice(["Living-room TV", "Laptop", "iPhone", "", "Printer", ""])
                    return {"ip": ip, "name": nm, "src": "mDNS" if nm else "", "rtt": random.randint(2, 40),
                            "ports": [80] if ip == gw else random.choice([[], [443], [62078]])}
                return None
            alive, rtt = False, None
            try:
                out = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True, timeout=3)
                if out.returncode == 0:
                    alive = True
                    m = re.search(r"time[=<]([\d.]+)", out.stdout)
                    rtt = int(float(m.group(1))) if m else None
            except Exception:
                pass
            ports = []
            for port in ((80, 443, 22, 53, 8080, 445, 62078) if alive else (80, 443, 62078)):
                s = socket.socket()
                s.settimeout(.25)
                try:
                    rc = s.connect_ex((ip, port))
                    if rc == 0:
                        ports.append(port)
                        alive = True
                    elif rc == 111:
                        alive = True
                except Exception:
                    pass
                finally:
                    s.close()
            if not alive:
                return None
            name = src = ""
            if ask:  # devices only answer if their own settings allow it
                for fn, label in ((mdns_name, "mDNS"), (netbios_name, "NetBIOS")):
                    try:
                        n = fn(ip)
                    except Exception:
                        n = ""
                    if n:
                        name, src = n, label
                        break
                if not name:
                    try:
                        n = socket.gethostbyaddr(ip)[0]
                        if n and not re.match(r"^\d+[-.]\d+", n):
                            name, src = n.split(".")[0], "DNS"
                    except Exception:
                        pass
            return {"ip": ip, "name": name, "src": src, "rtt": rtt, "ports": ports}

        def work():
            ssdp = {}
            if ask and not demo:
                t = threading.Thread(target=lambda: ssdp.update(ssdp_discover(2.5)), daemon=True)
                t.start()
            else:
                t = None
            with ThreadPoolExecutor(max_workers=48) as ex:
                for f in as_completed([ex.submit(probe, h) for h in hosts]):
                    try:
                        r = f.result()
                    except Exception:
                        r = None
                    if r:
                        if r["ip"] == gw:
                            r["tag"] = "router"
                            r["name"] = r["name"] or "Router"
                        Clock.schedule_once(lambda *_, r=r: (self.add_device(r), self.fill_devices()), 0)
            if t:
                t.join(3)
                for ip, loc in list(ssdp.items()):
                    try:
                        n = ssdp_name(ip, loc)
                    except Exception:
                        n = ""
                    if n:
                        Clock.schedule_once(lambda *_, ip=ip, n=n: (self.add_device({"ip": ip, "name": n, "src": "UPnP"}),
                                                                   self.fill_devices()), 0)
            macs = {}
            try:
                with open("/proc/net/arp") as fh:
                    for line in fh.read().splitlines()[1:]:
                        parts = line.split()
                        if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                            macs[parts[0]] = parts[3]
            except Exception:
                pass

            def done(*_):
                for ip, mac in macs.items():
                    if ip in self.devices:
                        self.devices[ip]["mac"] = mac
                self.dev_busy = False
                others = len(self.devices) - 1
                named = sum(1 for v in self.devices.values() if v.get("name") and v.get("tag") != "you")
                self.set_dev_text(f"Found {others} other device(s), {named} shared a name. Tap one to copy its IP." +
                                  ("" if macs else " Android hides MAC addresses from apps."))
                self.fill_devices()
            Clock.schedule_once(done, 0)
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- vpn
    def build_vpn(self):
        p = self.page()
        p.add_widget(self.header("VPN", scan=False))
        try:
            self.vpn_on = self.backend.vpn_active() if self.backend else False
        except Exception:
            pass
        on = self.vpn_on
        card = Card(bg="#10343A" if on else PANEL, orientation="vertical", padding=dp(14), spacing=dp(4), size_hint_y=None, height=dp(96))
        card.add_widget(TLabel(text=f"[b]VPN is {'ON' if on else 'OFF'}[/b]", markup=True, font_size=sp(20),
                               color=rgba(GOOD if on else WHITE)))
        sec = next((n["security"] for n in self.networks if n["connected"]), "")
        msg = ("Your traffic is going through a VPN." if on else
               "This Wi-Fi is not encrypted - turn on a VPN before using it." if sec in ("Open", "WEP")
               else "Turn on a VPN on public or shared Wi-Fi.")
        card.add_widget(TLabel(text=msg, font_size=sp(12), color=rgba(BAD if (sec in ("Open", "WEP") and not on) else MUTED)))
        p.add_widget(card)
        p.add_widget(self.btn("Open Android VPN settings", lambda *_: (self.backend and self.backend.open_vpn_settings(),
                                                                      Clock.schedule_once(lambda *_: self.go("vpn"), 1.2)),
                              bg=ACCENT, fg=BG, size_hint_y=None, height=dp(48)))
        p.add_widget(TLabel(text="[b]Your VPN apps[/b]", markup=True, font_size=sp(15), size_hint_y=None, height=dp(30)))
        sv = ScrollView(do_scroll_x=False)
        g = GridLayout(cols=1, size_hint_y=None, spacing=dp(6))
        g.bind(minimum_height=g.setter("height"))
        for name, pkg, custom in [(n, k, False) for n, k in VPN_PRESETS] + [(a[0], a[1], True) for a in self.cfg["vpn_apps"]]:
            r = Card(bg=PANEL, size_hint_y=None, height=dp(54), padding=dp(8), spacing=dp(6))
            col = BoxLayout(orientation="vertical")
            col.add_widget(TLabel(text=f"[b]{name}[/b]", markup=True, font_size=sp(14)))
            col.add_widget(TLabel(text=pkg, font_size=sp(9), color=rgba(MUTED)))
            r.add_widget(col)
            r.add_widget(self.btn("Open", lambda *_, pkg=pkg: self.open_vpn_app(pkg), bg=BLUE, fs=12, size_hint_x=None, width=dp(62)))
            if custom:
                r.add_widget(self.btn("Del", lambda *_, pkg=pkg: self.del_vpn_app(pkg), bg=BAD, fs=12, size_hint_x=None, width=dp(48)))
            g.add_widget(r)
        ti = dict(background_normal="", background_active="", background_color=rgba(PANEL2), foreground_color=rgba(WHITE),
                  hint_text_color=rgba(MUTED), cursor_color=rgba(ACCENT), padding=(dp(10), dp(10)), font_size=sp(13),
                  multiline=False, write_tab=False, size_hint_y=None, height=dp(44))
        nm, pk = TextInput(hint_text="App name", **ti), TextInput(hint_text="Package name, e.g. com.example.vpn", **ti)
        g.add_widget(nm)
        g.add_widget(pk)

        def add(*_):
            if nm.text.strip() and re.fullmatch(r"[A-Za-z0-9_.]+", pk.text.strip()):
                self.cfg["vpn_apps"].append([nm.text.strip(), pk.text.strip()])
                self.save_cfg()
                self.go("vpn")
            else:
                self.notice("Add VPN app", "Enter a name and a valid package name.")
        g.add_widget(self.btn("Add my VPN app", add, size_hint_y=None, height=dp(44)))
        g.add_widget(AutoLabel(text="This app does not create its own VPN tunnel (that needs native Android VpnService code). "
                                    "It shows your VPN status and opens Android's VPN settings or the VPN app you installed. "
                                    "If an app is not installed, its Play Store page opens.", font_size=sp(10), color=rgba(MUTED)))
        sv.add_widget(g)
        p.add_widget(sv)

    def open_vpn_app(self, pkg):
        try:
            res = self.backend.launch_app(pkg)
            if res == "store":
                self.notice("VPN app", "That app is not installed (or Android hides it from this app), so its Play Store page was opened.")
        except Exception as exc:
            self.notice("VPN app", "Could not open it: " + str(exc))

    def del_vpn_app(self, pkg):
        self.cfg["vpn_apps"] = [a for a in self.cfg["vpn_apps"] if a[1] != pkg]
        self.save_cfg()
        self.go("vpn")

    # ----------------------------------------------------------- settings
    def build_settings(self):
        p = self.page()
        p.add_widget(self.header("Settings", scan=False))
        sv = ScrollView(do_scroll_x=False)
        g = GridLayout(cols=1, size_hint_y=None, spacing=dp(6), padding=(0, 0, 0, dp(12)))
        g.bind(minimum_height=g.setter("height"))
        c = self.cfg

        def section(t):
            g.add_widget(TLabel(text=f"[b]{t}[/b]", markup=True, color=rgba(ACCENT), font_size=sp(14),
                                size_hint_y=None, height=dp(34)))

        def row(label, getter, tap):
            r = Card(bg=PANEL, size_hint_y=None, height=dp(50), padding=dp(8), spacing=dp(8))
            r.add_widget(TLabel(text=label, font_size=sp(13)))
            b = self.btn("", None, fs=12, size_hint_x=None, width=dp(90))

            def refresh():
                v = getter()
                b.lbl.text = ("ON" if v else "OFF") if isinstance(v, bool) else str(v)
                b.set_bg(GOOD if v is True else PANEL2)
                b.lbl.color = rgba(BG if v is True else WHITE)
            b.bind(on_release=lambda *_: (tap(), self.save_cfg(), refresh()))
            refresh()
            r.add_widget(b)
            g.add_widget(r)

        def toggle(label, key, after=None):
            def tap():
                c[key] = not c[key]
                if after:
                    after(c[key])
            row(label, lambda: c[key], tap)

        def cycle(label, key, options, fmt=lambda v: v):
            def tap():
                c[key] = options[(options.index(c[key]) + 1) % len(options)] if c[key] in options else options[0]
            row(label, lambda: fmt(c[key]), tap)

        section("Scanning")
        toggle("Auto scan", "auto_scan")
        cycle("Scan every (seconds)", "scan_interval", [5, 10, 20, 30, 60])
        g.add_widget(AutoLabel(text="Android limits how often apps may scan, so faster values may repeat older results.",
                               font_size=sp(11), color=rgba(MUTED)))
        toggle("Group same-name networks", "group_ssid")
        toggle("Hide hidden networks", "hide_hidden")
        toggle("Show 2.4 GHz", "show_24")
        toggle("Show 5 GHz", "show_5")
        toggle("Show 6 GHz", "show_6")
        cycle("Hide weaker than (dBm)", "min_signal", [-100, -90, -80, -70])
        cycle("Sort by", "sort_by", ["signal", "name", "security"])
        section("Display")
        cycle("Distance unit", "unit", ["m", "ft"])
        cycle("Signal unit", "signal_unit", ["dBm", "%"])
        section("Radar")
        cycle("Radar range (m)", "range", [20, 50, 100, 200])
        cycle("Sweep time (seconds)", "sweep_seconds", [2, 4, 6, 8])
        cycle("Radar orientation", "radar_mode", ["heading", "north"], lambda v: "Heading up" if v == "heading" else "North up")
        toggle("Show names on radar", "show_labels")
        cycle("Environment", "env", [2.0, 2.5, 3.0, 3.5, 4.0],
              lambda v: {2.0: "Open space", 2.5: "Light walls", 3.0: "Home", 3.5: "Thick walls", 4.0: "Concrete"}[v])
        cycle("Signal at 1 m (dBm)", "ref_1m", [-30, -35, -40, -45, -50])
        g.add_widget(AutoLabel(text="Environment and signal-at-1-m tune how distance is calculated from signal strength.",
                               font_size=sp(11), color=rgba(MUTED)))
        section("Devices & names")
        toggle("Ask other devices for their names", "ask_names")
        toggle("Share this phone's name on the network", "share_name",
               lambda on: self.backend and self.backend.share_name(on))
        g.add_widget(AutoLabel(text="Other devices only show a name if their own settings allow it. Sharing is off until you turn it on.",
                               font_size=sp(11), color=rgba(MUTED)))
        section("VPN")
        toggle("Warn me on unencrypted Wi-Fi", "vpn_warn")
        section("Other")
        toggle("Demo mode (sample data, restart app)", "demo")
        g.add_widget(self.btn("Open Android Wi-Fi settings", lambda *_: self.backend and self.backend.open_panel(),
                              size_hint_y=None, height=dp(44)))

        def reset(*_):
            self.cfg.clear()
            self.cfg.update(DEFAULTS)
            self.cfg["vpn_apps"] = []
            self.save_cfg()
            self.go("settings")
        g.add_widget(self.btn("Reset to defaults", reset, bg=BAD, size_hint_y=None, height=dp(44)))
        g.add_widget(AutoLabel(text=f"Wi-Fi Mini Scanner v{__version__}", font_size=sp(11), color=rgba(MUTED), halign="center"))
        sv.add_widget(g)
        p.add_widget(sv)


if __name__ == "__main__":
    WifiMiniApp().run()
