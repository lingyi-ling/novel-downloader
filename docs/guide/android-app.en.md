# Android Client

A native Android app (Kotlin + Jetpack Compose) that communicates with the computer through the **local REST API**:
search novels, manage download tasks, browse exported files, and download music from multiple platforms.

## Prerequisites

1. Start the API service on the computer:

   ```bash
   # Local use only (the emulator can reach 10.0.2.2)
   novel-api --port 8081

   # Physical device: bind to the LAN and set a token
   novel-api --host 0.0.0.0 --port 8081 --token <your-token>
   ```

2. The phone and the computer must be on the same LAN; allow TCP port 8081 through the Windows firewall.

## Install the App

The source code lives in the repository's `android/` directory:

```bash
cd android
./gradlew assembleDebug      # produces app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug       # or install directly to a connected device
```

You can also open `android/` in Android Studio, sync, and run it.

## First-time Configuration

Open "Settings" in the top-right corner of the app:

| Scenario | API address |
| --- | --- |
| Emulator reaching the computer | `http://10.0.2.2:8081/` |
| Physical device reaching the computer | `http://<computer LAN IP>:8081/` |

If the API has a token configured, enter the same value in "API key".

## Features

- **Search**: keyword plus optional site filter (comma-separated; empty means all sites). Results can be added to the download queue with one tap.
- **Tasks**: running / queued / completed sections, progress bars and chapter counts, auto-refresh (every 2 seconds), and cancellation.
- **History**: exported files; tap one to hand it to the system browser/downloader for saving.
- **Music**: choose a platform, search songs, select multiple results, create a download job, and view progress.

## Security Notes

The app uses HTTP to reach the local API (`usesCleartextTraffic=true`). Only use it on a trusted LAN. For access across networks, use a VPN (such as Tailscale) or put an HTTPS reverse proxy in front of the API, and always set a token.