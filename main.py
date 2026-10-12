__version__ = "1.0.0"

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.utils import platform
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout

ANDROID = platform == "android"


class WifiMiniApp(App):
    def build(self):
        self.title = "Wi-Fi Mini Scanner"
        self.selected_ssid = None
        self.wifi_manager = None

        root = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
        root.canvas.before.clear()

        title = Label(
            text="[b]WI-FI MINI SCANNER[/b]",
            markup=True, font_size="22sp", size_hint_y=None, height=dp(48)
        )
        root.add_widget(title)

        self.status = Label(
            text="Tap Scan to look for nearby Wi-Fi networks.",
            font_size="14sp", size_hint_y=None, height=dp(54),
            halign="left", valign="middle"
        )
        self.status.bind(size=lambda *_: setattr(self.status, "text_size", self.status.size))
        root.add_widget(self.status)

        buttons = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        scan_btn = Button(text="Scan networks")
        scan_btn.bind(on_release=self.scan_networks)
        buttons.add_widget(scan_btn)

        settings_btn = Button(text="Wi-Fi settings")
        settings_btn.bind(on_release=lambda *_: self.open_wifi_settings())
        buttons.add_widget(settings_btn)
        root.add_widget(buttons)

        self.results = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        self.results.bind(minimum_height=self.results.setter("height"))
        scroll = ScrollView()
        scroll.add_widget(self.results)
        root.add_widget(scroll)

        note = Label(
            text="Android controls the final connection. Tap a network, then use Wi-Fi settings to select it and enter its password.",
            font_size="12sp", size_hint_y=None, height=dp(60),
            halign="left", valign="middle"
        )
        note.bind(size=lambda *_: setattr(note, "text_size", note.size))
        root.add_widget(note)

        if ANDROID:
            Clock.schedule_once(lambda *_: self.prepare_android(), 0.5)
        else:
            self.status.text = "This scanner needs to run as an Android APK to access the phone's Wi-Fi API."
        return root

    def prepare_android(self):
        try:
            from jnius import autoclass
            self.PythonActivity = autoclass("org.kivy.android.PythonActivity")
            self.Context = autoclass("android.content.Context")
            self.Intent = autoclass("android.content.Intent")
            self.wifi_manager = self.PythonActivity.mActivity.getApplicationContext().getSystemService(
                self.Context.WIFI_SERVICE
            )
            self.request_wifi_permissions()
        except Exception as exc:
            self.status.text = "Android Wi-Fi setup failed: " + str(exc)

    def request_wifi_permissions(self):
        try:
            from android.permissions import request_permissions
            # FINE_LOCATION is needed by Wi-Fi scan-result APIs on many Android versions.
            # NEARBY_WIFI_DEVICES is also declared/requested for modern Android.
            request_permissions(
                ["android.permission.ACCESS_FINE_LOCATION",
                 "android.permission.NEARBY_WIFI_DEVICES"],
                self.permission_result
            )
        except Exception as exc:
            self.status.text = "Could not request Wi-Fi permissions: " + str(exc)

    def permission_result(self, permissions, grants):
        if grants and any(grants):
            self.status.text = "Permission granted. Tap Scan networks."
        else:
            self.status.text = (
                "Wi-Fi permission was not granted. Allow Nearby devices / Location "
                "permission in Android Settings, then reopen the app."
            )

    def scan_networks(self, *_):
        if not ANDROID:
            self.status.text = "Run this as the built APK on Android."
            return
        if self.wifi_manager is None:
            self.prepare_android()
            self.status.text = "Preparing Android Wi-Fi access. Tap Scan again in a moment."
            return
        try:
            if not self.wifi_manager.isWifiEnabled():
                self.status.text = "Turn Wi-Fi on first, then tap Scan networks."
                self.open_wifi_settings()
                return

            self.status.text = "Scanning… Android may limit how often apps can scan."
            try:
                self.wifi_manager.startScan()
            except Exception:
                pass
            Clock.schedule_once(self.read_scan_results, 2.0)
        except Exception as exc:
            self.status.text = "Scan failed: " + str(exc)

    def read_scan_results(self, *_):
        try:
            scan_results = self.wifi_manager.getScanResults()
            self.results.clear_widgets()
            seen = set()
            count = 0

            for item in scan_results:
                ssid = str(item.SSID or "").strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                count += 1
                level = int(item.level)
                frequency = int(item.frequency)
                security = "Secured" if str(item.capabilities) else "Unknown security"
                if "WEP" in str(item.capabilities) or "PSK" in str(item.capabilities) or "EAP" in str(item.capabilities) or "SAE" in str(item.capabilities):
                    security = "Secured"
                elif "ESS" in str(item.capabilities):
                    security = "Open / check network"

                row = BoxLayout(size_hint_y=None, height=dp(72), spacing=dp(8), padding=dp(4))
                info = Label(
                    text=f"[b]{ssid}[/b]\nSignal: {level} dBm  •  {frequency} MHz  •  {security}",
                    markup=True, halign="left", valign="middle", font_size="13sp"
                )
                info.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
                row.add_widget(info)

                choose = Button(text="Select", size_hint_x=None, width=dp(88))
                choose.bind(on_release=lambda _btn, network=ssid: self.select_network(network))
                row.add_widget(choose)
                self.results.add_widget(row)

            if count:
                self.status.text = f"Found {count} visible network(s). Select one, then open Wi-Fi settings to connect."
            else:
                self.status.text = (
                    "No scan results returned. Turn on Location services if your Android version requires it, "
                    "grant permissions, wait a few seconds, and scan again."
                )
        except Exception as exc:
            self.status.text = "Could not read scan results: " + str(exc)

    def select_network(self, ssid):
        self.selected_ssid = ssid
        self.status.text = (
            f"Selected: {ssid}\nAndroid must complete the connection in its Wi-Fi screen."
        )
        self.open_wifi_settings()

    def open_wifi_settings(self):
        if not ANDROID:
            self.status.text = "Wi-Fi settings are available only in the Android APK."
            return
        try:
            from jnius import autoclass
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            Intent = autoclass("android.content.Intent")
            try:
                intent = Intent("android.settings.panel.action.WIFI")
                activity.startActivity(intent)
            except Exception:
                intent = Intent("android.settings.WIFI_SETTINGS")
                activity.startActivity(intent)
        except Exception as exc:
            self.status.text = "Could not open Wi-Fi settings: " + str(exc)


if __name__ == "__main__":
    WifiMiniApp().run()
