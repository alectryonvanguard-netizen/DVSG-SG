__version__ = "2.0.0"
"""
Wi-Fi Mini Scanner v2 (Kivy / Android APK)
- Home: live list of nearby Wi-Fi + current connection, no need to open Android settings
- Radar: full circular radar, networks plotted by estimated distance with their names
- Devices: find devices on the Wi-Fi you are connected to
- Settings: scan, filter, radar and unit options
- Connect: in-app connect (Android 10+ uses a network suggestion that Android must approve)

Buildozer: requirements = python3,kivy,pyjnius,android
permissions = ACCESS_FINE_LOCATION,NEARBY_WIFI_DEVICES,ACCESS_WIFI_STATE,CHANGE_WIFI_STATE,
              ACCESS_NETWORK_STATE,CHANGE_NETWORK_STATE,INTERNET
Without Android Wi-Fi access (for example in Pydroid), the app runs in DEMO mode with sample data.
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


def estimate_distance(level, freq, env=3.0, tx=20.0):
    """Rough distance in metres from signal strength (very approximate, walls change it a lot)."""
    exp = (tx - level - 20 * math.log10(max(freq, 1)) + 27.55) / (10.0 * env)
    return max(0.5, min(500.0, 10 ** exp))


def fmt_dist(m, unit):
    if unit == "ft":
        return f"{m * 3.281:.0f} ft"
    return f"{m:.0f} m" if m >= 10 else f"{m:.1f} m"


def int_to_ip(n):
    n &= 0xFFFFFFFF
    return ".".join(str((n >> (8 * i)) & 0xFF) for i in range(4))


def subnet_hosts(ip, mask):
    """Host addresses of the local network (capped to one /24 around the phone)."""
    def to_int(s):
        a = [int(x) for x in s.split(".")]
        return (a[0] << 24) | (a[1] << 16) | (a[2] << 8) | a[3]
    i, m = to_int(ip), to_int(mask)
    if bin(m).count("1") < 24:
        m = 0xFFFFFF00
    net = i & m
    size = (~m) & 0xFFFFFFFF
    hosts = []
    for h in range(1, size):
        v = net + h
        hosts.append(".".join(str((v >> s) & 255) for s in (24, 16, 8, 0)))
    return hosts
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

BG, PANEL, PANEL2 = "#071018", "#0F1E2E", "#172B40"
ACCENT, WHITE, MUTED = "#25E8C8", "#EAF4FF", "#8FA7BF"
GOOD, MID, BAD, BLUE = "#22E06B", "#F2D024", "#FF5A5A", "#2D8CFF"
ANDROID = platform == "android"

DEFAULTS = {
    "auto_scan": True, "scan_interval": 10, "group_ssid": True, "hide_hidden": True,
    "show_24": True, "show_5": True, "show_6": True, "min_signal": -100, "sort_by": "signal",
    "unit": "m", "signal_unit": "dBm", "range": 50, "env": 3.0, "tx_power": 20.0,
    "sweep_seconds": 4, "show_labels": True, "demo": False,
}


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
    """Full circular radar. Distance from the centre = estimated distance. Direction is not
    available from Wi-Fi scans, so networks are spread evenly around the circle."""
    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self.items, self.labels = [], []
        self.sweep = 0.0
        self.bind(pos=self.relayout, size=self.relayout, parent=self._parent)
        self._ev = Clock.schedule_interval(self.tick, 1 / 30)

    def _parent(self, _i, p):
        if p is None and self._ev:
            self._ev.cancel()
            self._ev = None

    def geom(self):
        cx, cy = self.center
        return cx, cy, max(dp(40), min(self.width, self.height) / 2 - dp(8))

    def frac(self, d):
        rng = max(5, self.app.cfg["range"])
        return min(.96, math.log1p(d) / math.log1p(rng))

    def set_items(self, items):
        items = sorted(items, key=lambda x: x["bssid"])
        n = max(1, len(items))
        for i, it in enumerate(items):
            it["angle"] = 90 + 360.0 * i / n
        self.items = items
        self.build_labels()
        self.relayout()

    def build_labels(self):
        for lb in self.labels:
            self.remove_widget(lb)
        self.labels = []
        rng, unit = self.app.cfg["range"], self.app.cfg["unit"]
        for f in (.25, .5, 1):
            lb = Label(text=fmt_dist(rng * f if f < 1 else rng, unit), font_size=sp(9), color=rgba(MUTED),
                       size_hint=(None, None))
            lb.kind, lb.ring = "ring", f
            self.labels.append(lb)
        if self.app.cfg["show_labels"]:
            for it in self.items:
                lb = Label(text=f"{it['ssid'][:16]}\n{self.app.sig_text(it['level'])}", font_size=sp(10),
                           color=rgba(WHITE), halign="center", size_hint=(None, None))
                lb.kind, lb.item = "net", it
                self.labels.append(lb)
        for lb in self.labels:
            lb.texture_update()
            lb.size = lb.texture_size
            self.add_widget(lb)

    def blip_pos(self, it):
        cx, cy, R = self.geom()
        r = R * self.frac(it["dist"])
        a = math.radians(it["angle"])
        return cx + r * math.cos(a), cy + r * math.sin(a)

    def relayout(self, *_):
        cx, cy, R = self.geom()
        for lb in self.labels:
            if lb.kind == "ring":
                r = R * self.frac(self.app.cfg["range"] * lb.ring)
                lb.center = (cx + r * .72 + dp(10), cy + r * .72)
            else:
                x, y = self.blip_pos(lb.item)
                lb.center = (min(max(x, self.x + lb.width / 2), self.right - lb.width / 2),
                             min(max(y + dp(20), self.y + lb.height / 2), self.top - lb.height / 2))
        self.draw()

    def tick(self, dt):
        self.sweep = (self.sweep + dt * 360.0 / max(1, self.app.cfg["sweep_seconds"])) % 360
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
            Color(*rgba(ACCENT), .35)
            Line(points=[cx - R, cy, cx + R, cy], width=dp(1))
            Line(points=[cx, cy - R, cx, cy + R], width=dp(1))
            for k in range(0, 360, 30):
                a = math.radians(k)
                Line(points=[cx + R * .95 * math.cos(a), cy + R * .95 * math.sin(a),
                             cx + R * math.cos(a), cy + R * math.sin(a)], width=dp(1.2))
            for k in range(0, 36, 2):  # fading sweep trail
                a = math.radians(self.sweep + k * 1.6)
                Color(*rgba(ACCENT), max(0.0, .38 - k * .011))
                Line(points=[cx, cy, cx + R * math.cos(a), cy + R * math.sin(a)], width=dp(3))
            a = math.radians(self.sweep)
            Color(*rgba(ACCENT), .95)
            Line(points=[cx, cy, cx + R * math.cos(a), cy + R * math.sin(a)], width=dp(2))
            for it in self.items:
                x, y = self.blip_pos(it)
                diff = (it["angle"] - self.sweep) % 360
                glow = max(0.0, 1 - diff / 140.0)
                size = dp(9) + bars_of(it["level"]) * dp(1.6)
                col = rgba(level_color(it["level"]))
                Color(*col[:3], .18 + .3 * glow)
                Ellipse(pos=(x - size, y - size), size=(2 * size, 2 * size))
                Color(*col[:3], .55 + .45 * glow)
                Ellipse(pos=(x - size / 2, y - size / 2), size=(size, size))
                if it.get("connected"):
                    Color(*rgba(WHITE))
                    Line(circle=(x, y, size * .9), width=dp(1.5))
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


# ---------------------------------------------------------------- backends
class AndroidWifi:
    def __init__(self):
        from jnius import autoclass
        self.ac = autoclass
        self.act = autoclass("org.kivy.android.PythonActivity").mActivity
        ctx = autoclass("android.content.Context")
        self.wm = self.act.getApplicationContext().getSystemService(ctx.WIFI_SERVICE)
        self.sdk = autoclass("android.os.Build$VERSION").SDK_INT
        self.demo = False

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
                        "freq": int(it.frequency), "caps": str(it.capabilities or "")})
        return out

    def current(self):
        try:
            info = self.wm.getConnectionInfo()
            ssid = str(info.getSSID() or "").strip('"')
            rssi = int(info.getRssi())
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

    def open_panel(self):
        Intent = self.ac("android.content.Intent")
        try:
            self.act.startActivity(Intent("android.settings.panel.action.WIFI"))
        except Exception:
            self.act.startActivity(Intent("android.settings.WIFI_SETTINGS"))

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
    NETS = [("Home-WiFi", "WPA2", 2437, -48), ("Home-WiFi-5G", "WPA2/WPA3", 5180, -55), ("Neighbour_2G", "WPA2", 2412, -72),
            ("CoffeeShop Free", "Open", 2462, -66), ("Office-Net", "Enterprise", 5745, -80), ("TP-Link_A1B2", "WPA2", 2447, -84),
            ("Phone Hotspot", "WPA3", 5220, -61), ("Guest", "WPA2", 2437, -90)]

    def __init__(self):
        self.t = 0

    def enabled(self):
        return True

    def start_scan(self):
        pass

    def results(self):
        out = []
        for i, (s, sec, f, lv) in enumerate(self.NETS):
            caps = {"Open": "[ESS]", "Enterprise": "[WPA2-EAP-CCMP][ESS]", "WPA3": "[RSN-SAE-CCMP][ESS]",
                    "WPA2/WPA3": "[RSN-PSK+SAE-CCMP][ESS]"}.get(sec, "[WPA2-PSK-CCMP][ESS]")
            out.append({"ssid": s, "bssid": "aa:bb:cc:00:00:%02x" % i, "level": lv + random.randint(-3, 3), "freq": f, "caps": caps})
        return out

    def current(self):
        return {"ssid": "Home-WiFi", "bssid": "aa:bb:cc:00:00:00", "level": -48 + random.randint(-2, 2), "speed": 433,
                "freq": 2437, "ip": "192.168.1.23"}

    def dhcp(self):
        return {"ip": "192.168.1.23", "gateway": "192.168.1.1", "mask": "255.255.255.0"}

    def open_panel(self):
        pass

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
        self.backend = None
        self.networks, self.current = [], None
        self.devices, self.dev_busy, self.dev_text = [], False, "Tap Scan to find devices on this Wi-Fi."
        self.scanning, self.last_scan, self.clock_s = False, -999, 0
        self.status_text = "Starting..."
        self.status_label = None
        self.screen = "home"
        self.radar = None
        self.track = None
        self.net_box = self.cur_box = self.net_sv = None
        root = BoxLayout(orientation="vertical")
        self.body = BoxLayout()
        root.add_widget(self.body)
        nav = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(3), padding=(dp(4), dp(3)))
        self.nav_tiles = {}
        for key, label in (("home", "Home"), ("radar", "Radar"), ("devices", "Devices"), ("settings", "Settings")):
            t = Tile(bg="#0A1626", radius=10)
            t.lbl = TLabel(text=label, halign="center", font_size=sp(12), color=rgba(MUTED))
            t.add_widget(t.lbl)
            t.bind(on_release=lambda _i, k=key: self.go(k))
            nav.add_widget(t)
            self.nav_tiles[key] = t
        root.add_widget(nav)
        self.go("home")
        Clock.schedule_once(lambda *_: self.start_backend(), .4)
        Clock.schedule_interval(self.tick, 1)
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
        pop = Popup(title=title, content=box, size_hint=(.88, .45), background_color=rgba(PANEL))
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
        self.body.clear_widgets()
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
        if self.cfg["demo"]:
            self.backend = DemoWifi()
            self.set_status("Demo mode - showing sample networks.")
            self.scan(True)
            return
        try:
            if not ANDROID:
                raise RuntimeError("not android")
            self.backend = AndroidWifi()
        except Exception:
            self.backend = DemoWifi()
            self.set_status("Android Wi-Fi access not available here - DEMO mode with sample data.")
            self.scan(True)
            return
        try:
            from android.permissions import request_permissions
            request_permissions(["android.permission.ACCESS_FINE_LOCATION", "android.permission.NEARBY_WIFI_DEVICES"],
                                lambda p, g: Clock.schedule_once(lambda *_: self.on_perms(g), 0))
        except Exception:
            self.scan(True)

    def on_perms(self, grants):
        if grants and any(grants):
            self.scan(True)
        else:
            self.set_status("Permission not granted. Allow Location / Nearby devices for this app in Android Settings.")

    def tick(self, _dt):
        self.clock_s += 1
        if self.track:
            if self.clock_s - self.last_scan >= 4:
                self.scan(False)
        elif self.cfg["auto_scan"] and self.screen in ("home", "radar") and \
                self.clock_s - self.last_scan >= self.cfg["scan_interval"]:
            self.scan(False)

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
        self.set_status("Scanning...")
        self.backend.start_scan()
        Clock.schedule_once(self.read_scan, .4 if self.backend.demo else 1.5)

    def read_scan(self, *_):
        self.scanning = False
        try:
            raw = self.backend.results()
            self.current = self.backend.current()
        except Exception as exc:
            self.set_status("Could not read scan results: " + str(exc))
            return
        self.networks = self.process(raw)
        if self.networks:
            self.set_status(f"{len(self.networks)} network(s) - updated {time.strftime('%H:%M:%S')}")
        else:
            self.set_status("No networks found. Check permissions and that Location services are on, then scan again.")
        self.refresh_screen()
        self.update_track()

    def process(self, raw):
        c = self.cfg
        cur = self.current or {}
        items = []
        for r in raw:
            ssid = r["ssid"].strip()
            if not ssid and c["hide_hidden"]:
                continue
            band = band_of(r["freq"])
            if not c["show_" + {"2.4": "24", "5": "5", "6": "6"}[band]] or r["level"] < c["min_signal"]:
                continue
            items.append({"ssid": ssid or "(hidden network)", "bssid": r["bssid"] or ssid, "level": r["level"],
                          "freq": r["freq"], "band": band, "channel": channel_of(r["freq"]),
                          "security": parse_security(r["caps"]),
                          "dist": estimate_distance(r["level"], r["freq"], c["env"], c["tx_power"]),
                          "connected": bool(cur) and ssid == cur.get("ssid") and
                                       (not cur.get("bssid") or r["bssid"] == cur.get("bssid") or c["group_ssid"])})
        if c["group_ssid"]:
            best = {}
            for it in items:
                k = it["ssid"] if not it["ssid"].startswith("(hidden") else it["bssid"]
                if k not in best or it["level"] > best[k]["level"]:
                    best[k] = it
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
        elif self.screen == "devices":
            pass

    # --------------------------------------------------------------- home
    def build_home(self):
        p = self.page()
        p.add_widget(self.header("Wi-Fi Networks"))
        self.status_label = TLabel(text=self.status_text, font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(24))
        p.add_widget(self.status_label)
        self.cur_box = BoxLayout(size_hint_y=None, height=dp(96))
        p.add_widget(self.cur_box)
        self.net_sv = ScrollView(do_scroll_x=False, bar_width=dp(3))
        self.net_box = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        self.net_box.bind(minimum_height=self.net_box.setter("height"))
        self.net_sv.add_widget(self.net_box)
        p.add_widget(self.net_sv)

    def fill_home(self):
        self.cur_box.clear_widgets()
        cur = self.current
        card = Card(bg="#10343A" if cur else PANEL, padding=dp(12), spacing=dp(10))
        if cur:
            card.add_widget(SignalBars(cur["level"], level_color(cur["level"])))
            col = BoxLayout(orientation="vertical")
            col.add_widget(TLabel(text=f"[b]Connected: {cur['ssid']}[/b]", markup=True, font_size=sp(16)))
            col.add_widget(TLabel(text=f"{self.sig_text(cur['level'])}  |  {cur['speed']} Mbps  |  {band_of(cur['freq'])} GHz  |  IP {cur['ip']}",
                                  font_size=sp(11), color=rgba(MUTED)))
            card.add_widget(col)
        else:
            col = BoxLayout(orientation="vertical")
            col.add_widget(TLabel(text="[b]Not connected[/b]", markup=True, font_size=sp(16)))
            col.add_widget(TLabel(text="Tap a network below to connect.", font_size=sp(11), color=rgba(MUTED)))
            card.add_widget(col)
        self.cur_box.add_widget(card)
        y = self.net_sv.scroll_y
        self.net_box.clear_widgets()
        if not self.networks:
            self.net_box.add_widget(AutoLabel(text="No networks to show yet.", color=rgba(MUTED)))
        for it in self.networks:
            row = Tile(bg="#10343A" if it["connected"] else PANEL, size_hint_y=None, height=dp(70), padding=dp(10), spacing=dp(10))
            row.add_widget(SignalBars(it["level"], level_color(it["level"])))
            col = BoxLayout(orientation="vertical")
            name = it["ssid"] + ("  (connected)" if it["connected"] else "")
            col.add_widget(TLabel(text=f"[b]{name}[/b]", markup=True, font_size=sp(15)))
            col.add_widget(TLabel(text=f"{it['security']}  |  {it['band']} GHz ch {it['channel']}  |  {self.sig_text(it['level'])}  |  ~{fmt_dist(it['dist'], self.cfg['unit'])}",
                                  font_size=sp(11), color=rgba(MUTED)))
            row.add_widget(col)
            row.add_widget(TLabel(text="" if it["connected"] else ("Open" if it["security"] == "Open" else "Lock"),
                                  font_size=sp(11), color=rgba(MUTED), size_hint_x=None, width=dp(36), halign="center"))
            row.bind(on_release=lambda _i, it=it: self.show_network(it))
            self.net_box.add_widget(row)
        Clock.schedule_once(lambda *_: setattr(self.net_sv, "scroll_y", y), 0)

    # ---------------------------------------------------- network details
    def show_network(self, it):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(6))
        pop = Popup(title=it["ssid"], content=box, size_hint=(.92, .72), background_color=rgba(PANEL))
        for k, v in (("Signal", f"{self.sig_text(it['level'])}  ({bars_of(it['level'])}/4 bars)"),
                     ("Estimated distance", "~" + fmt_dist(it["dist"], self.cfg["unit"]) + " (approximate)"),
                     ("Security", it["security"]), ("Band / channel", f"{it['band']} GHz / {it['channel']}"),
                     ("Frequency", f"{it['freq']} MHz"), ("Address (BSSID)", it["bssid"])):
            r = BoxLayout(size_hint_y=None, height=dp(30))
            r.add_widget(TLabel(text=k, color=rgba(MUTED), font_size=sp(12)))
            r.add_widget(TLabel(text=str(v), halign="right", font_size=sp(13)))
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
        pw = TextInput(hint_text="Wi-Fi password", password=True, multiline=False, size_hint_y=None, height=dp(48),
                       background_normal="", background_active="", background_color=rgba(PANEL2),
                       foreground_color=rgba(WHITE), hint_text_color=rgba(MUTED), cursor_color=rgba(ACCENT),
                       padding=(dp(12), dp(12)), write_tab=False)
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        show = self.btn("Show", None, fs=12)
        show.bind(on_release=lambda *_: (setattr(pw, "password", not pw.password),
                                         setattr(show.lbl, "text", "Hide" if not pw.password else "Show")))
        row.add_widget(pw)
        row.add_widget(show)
        pw.size_hint_y = 1
        box.add_widget(row)
        box.add_widget(TLabel(text=f"Security: {sec}. The password is sent only to Android and is not saved by this app.",
                              font_size=sp(11), color=rgba(MUTED), size_hint_y=None, height=dp(40)))

        def go(*_):
            if len(pw.text) < 8 and sec != "Open":
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
        barbg.add_widget(Widget())
        for w in (big, dist, barbg, trend):
            box.add_widget(w)
        box.add_widget(Widget())
        box.add_widget(self.btn("Close", pop.dismiss, size_hint_y=None, height=dp(44)))
        self.track = {"key": it["bssid"], "ssid": it["ssid"], "big": big, "dist": dist, "trend": trend, "bg": barbg,
                      "fill": fill, "prev": it["level"]}
        barbg.add_widget(fill)
        pop.open()
        self.update_track(it)
        self.scan(True)

    def update_track(self, base=None):
        t = self.track
        if not t:
            return
        it = base or next((n for n in self.networks if n["bssid"] == t["key"] or n["ssid"] == t["ssid"]), None)
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
        p.add_widget(self.header("Wi-Fi Radar"))
        self.status_label = TLabel(text=self.status_text, font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(24))
        p.add_widget(self.status_label)
        self.radar = RadarView(self)
        p.add_widget(self.radar)
        p.add_widget(TLabel(text="Distance from the centre is estimated from signal strength. Wi-Fi gives no direction, so "
                                 "networks are spread around the circle. Tap a dot for details or live finding.",
                            font_size=sp(11), color=rgba(MUTED), size_hint_y=None, height=dp(52)))

    # ------------------------------------------------------------ devices
    def build_devices(self):
        p = self.page()
        p.add_widget(self.header("Devices on this Wi-Fi", scan=False))
        d = self.backend.dhcp() if self.backend else None
        info = f"This phone: {d['ip']}   Router: {d['gateway']}" if d else "Connect to a Wi-Fi network first."
        p.add_widget(TLabel(text=info, font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(26)))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        row.add_widget(self.btn("Find devices", lambda *_: self.scan_devices(), bg=ACCENT, fg=BG))
        p.add_widget(row)
        self.dev_label = TLabel(text=self.dev_text, font_size=sp(12), color=rgba(MUTED), size_hint_y=None, height=dp(26))
        p.add_widget(self.dev_label)
        sv = ScrollView(do_scroll_x=False)
        self.dev_box = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        self.dev_box.bind(minimum_height=self.dev_box.setter("height"))
        sv.add_widget(self.dev_box)
        p.add_widget(sv)
        self.fill_devices()

    def fill_devices(self):
        if self.screen != "devices":
            return
        self.dev_label.text = self.dev_text
        self.dev_box.clear_widgets()
        for dv in sorted(self.devices, key=lambda x: tuple(int(v) for v in x["ip"].split("."))):
            r = Tile(bg=PANEL, size_hint_y=None, height=dp(72), padding=dp(10), spacing=dp(8))
            col = BoxLayout(orientation="vertical")
            title = dv["name"] or "Unknown device"
            if dv.get("tag"):
                title += f"  ({dv['tag']})"
            col.add_widget(TLabel(text=f"[b]{title}[/b]", markup=True, font_size=sp(15)))
            extra = [dv["ip"]]
            if dv.get("mac"):
                extra.append(dv["mac"])
            if dv.get("rtt"):
                extra.append(f"{dv['rtt']} ms")
            if dv.get("ports"):
                extra.append("ports " + ",".join(str(x) for x in dv["ports"]))
            col.add_widget(TLabel(text="  |  ".join(extra), font_size=sp(11), color=rgba(MUTED)))
            r.add_widget(col)
            r.bind(on_release=lambda _i, ip=dv["ip"]: (Clipboard.copy(ip), self.set_dev_text(f"Copied {ip}")))
            self.dev_box.add_widget(r)

    def set_dev_text(self, t):
        self.dev_text = t
        if self.screen == "devices":
            self.dev_label.text = t

    def scan_devices(self):
        if self.dev_busy:
            return
        d = self.backend.dhcp() if self.backend else None
        if not d:
            self.notice("Devices", "You need to be connected to a Wi-Fi network to find its devices.")
            return
        self.dev_busy = True
        self.devices = []
        hosts = [h for h in subnet_hosts(d["ip"], d["mask"]) if h != d["ip"]]
        self.set_dev_text(f"Scanning {len(hosts)} addresses...")
        demo = self.backend.demo
        me, gw = d["ip"], d["gateway"]
        self.devices.append({"ip": me, "name": "This phone", "tag": "you"})
        self.fill_devices()

        def probe(ip):
            if demo:
                time.sleep(random.random() * .5)
                if random.random() < .25:
                    return {"ip": ip, "name": random.choice(["Smart-TV", "Laptop", "iPhone", "Printer", "Camera"]),
                            "rtt": random.randint(2, 40), "ports": random.choice([[], [80], [443], [22]])}
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
            name = ""
            try:
                name = socket.gethostbyaddr(ip)[0]
            except Exception:
                pass
            return {"ip": ip, "name": name, "rtt": rtt, "ports": ports}

        def work():
            found = 0
            with ThreadPoolExecutor(max_workers=48) as ex:
                futs = [ex.submit(probe, h) for h in hosts]
                for f in as_completed(futs):
                    try:
                        r = f.result()
                    except Exception:
                        r = None
                    if r:
                        found += 1
                        if r["ip"] == gw:
                            r["tag"] = "router"
                            r["name"] = r["name"] or "Router"
                        Clock.schedule_once(lambda *_, r=r: (self.devices.append(r), self.fill_devices()), 0)
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
                for dv in self.devices:
                    if dv["ip"] in macs:
                        dv["mac"] = macs[dv["ip"]]
                self.dev_busy = False
                self.set_dev_text(f"Found {found} other device(s). Tap one to copy its IP." +
                                  ("" if macs else " (Android hides MAC addresses from apps.)"))
                self.fill_devices()
            Clock.schedule_once(done, 0)
        threading.Thread(target=work, daemon=True).start()

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
            b = self.btn("", None, fs=12, size_hint_x=None, width=dp(86))

            def refresh():
                v = getter()
                b.lbl.text = ("ON" if v else "OFF") if isinstance(v, bool) else str(v)
                b.set_bg(GOOD if v is True else PANEL2)
                b.lbl.color = rgba(BG if v is True else WHITE)
            b.bind(on_release=lambda *_: (tap(), self.save_cfg(), refresh()))
            refresh()
            r.add_widget(b)
            g.add_widget(r)

        def toggle(label, key):
            row(label, lambda: c[key], lambda: c.__setitem__(key, not c[key]))

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
        toggle("Show names on radar", "show_labels")
        cycle("Environment", "env", [2.0, 2.5, 3.0, 3.5, 4.0],
              lambda v: {2.0: "Open space", 2.5: "Light walls", 3.0: "Home", 3.5: "Thick walls", 4.0: "Concrete"}[v])
        g.add_widget(AutoLabel(text="Environment changes how distance is estimated from signal strength.",
                               font_size=sp(11), color=rgba(MUTED)))
        section("Other")
        toggle("Demo mode (sample data)", "demo")
        g.add_widget(self.btn("Open Android Wi-Fi settings", lambda *_: self.backend and self.backend.open_panel(),
                              size_hint_y=None, height=dp(44)))

        def reset(*_):
            self.cfg.clear()
            self.cfg.update(DEFAULTS)
            self.save_cfg()
            self.go("settings")
        g.add_widget(self.btn("Reset to defaults", reset, bg=BAD, size_hint_y=None, height=dp(44)))
        g.add_widget(AutoLabel(text=f"Wi-Fi Mini Scanner v{__version__}. Demo mode applies after restarting the app.",
                               font_size=sp(11), color=rgba(MUTED), halign="center"))
        sv.add_widget(g)
        p.add_widget(sv)


if __name__ == "__main__":
    WifiMiniApp().run()
