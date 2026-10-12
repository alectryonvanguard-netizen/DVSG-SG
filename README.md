# Wi-Fi Mini Scanner (Android APK)

This is a small Kivy Android app that:
- Requests Android Wi-Fi/location-related permissions.
- Uses Android's `WifiManager` to request a scan and display visible SSIDs, signal strength (dBm), and frequency.
- Opens Android's own Wi-Fi panel/settings so the user can select a network and enter its password.

## Important Android limitation
Android controls Wi-Fi connection approval. This app does **not** silently connect to a network or bypass its password. Tapping a result opens the system Wi-Fi panel/settings; complete the connection there. Android may throttle scans or return cached results.

## Build from GitHub on your phone
1. Create a new GitHub repository.
2. Upload `main.py`, `buildozer.spec`, and `.github/workflows/build.yml` keeping the same folder structure.
3. Commit to `main` (or `master`).
4. Open the repository's **Actions** tab.
5. Open **Build Wi-Fi Mini Scanner APK** and wait for the run to finish.
6. Open the completed workflow run and download the artifact named `wifi-mini-scanner-apk`.
7. Extract the ZIP artifact and install the APK on your Android phone. Android may ask you to allow installation from your browser/files app.

## First launch
- Turn on Wi-Fi.
- Allow the requested permissions.
- On Android versions that require it, enable Location services for Wi-Fi scan results.
- Tap **Scan networks**.
- Tap **Select** beside a network to open Android's Wi-Fi panel/settings and finish connecting there.

## Notes
- This is a starter project. A successful GitHub build is not the same as a physical-device test; test on the phone and check Android permission prompts.
- If the build fails, open the workflow run and share the first actual error lines.
