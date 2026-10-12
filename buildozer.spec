[app]

title = Wi-Fi Mini Scanner
package.name = wifiminiscanner
package.domain = org.samng
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 1.0.0

requirements = python3,kivy,pyjnius
orientation = portrait
fullscreen = 0

# Wi-Fi scan results require location permission on many Android versions.
# Modern Android also uses NEARBY_WIFI_DEVICES.
android.permissions = INTERNET,ACCESS_WIFI_STATE,CHANGE_WIFI_STATE,ACCESS_NETWORK_STATE,ACCESS_FINE_LOCATION,NEARBY_WIFI_DEVICES
android.api = 33
android.minapi = 21
android.ndk_api = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.debug_artifact = apk

[buildozer]
log_level = 2
warn_on_root = 1
