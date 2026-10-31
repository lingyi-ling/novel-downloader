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

### Release Builds

Release builds require signing material. Create a **git-ignored**
`keystore.properties` under `android/` (template in the comment at the top of
`app/build.gradle.kts`):

```properties
storeFile=release.jks
storePassword=...
keyAlias=...
keyPassword=...
```

```bash
./gradlew assembleRelease    # produces app/build/outputs/apk/release/app-release.apk
```

When the file is missing or incomplete, release builds fall back to the debug
signature (fine for local testing, not for distribution). **Keep
`keystore.properties` and the keystore file local; never commit them.**

## Run the Unit Tests

The app ships device-free JVM unit tests (API client, follow queueing, navigation, and more):

```bash
cd android
./gradlew testDebugUnitTest
```

## First-time Configuration

Open "Settings" in the top-right corner of the app:

| Scenario | API address |
| --- | --- |
| Emulator reaching the computer | `http://10.0.2.2:8081/` |
| Physical device reaching the computer | `http://<computer LAN IP>:8081/` |

If the API has a token configured, enter the same value in "API key".

## Features

Bottom tabs: **Search / Tasks / History / Music / Follow**.

- **Search**: keyword plus optional site filter (comma-separated; empty means all sites). Results can be added to the download queue with one tap, or followed via the "Follow" action.
- **Follow**: maintain the follow list (list / add / remove); "Check updates" searches each book for its latest chapter, and entries with new chapters can join the download queue with one tap.
- **Tasks**: running / queued / completed sections, progress bars and chapter counts, auto-refresh (every 2 seconds), cancellation, and one-tap retry for failed tasks.
- **Login credential requests**: when a site requires login and no usable credential is stored, the task parks and a "Login credential requests" card appears at the top of the Tasks page. Tap "Provide credentials" to fill in the fields (password/cookie inputs are masked, required fields validated, prefill values seeded); submitting resumes the task.
- **History**: exported files; tapping one hands it to the system DownloadManager, which saves it into the public Downloads directory.
- **Music**: choose a platform, search songs, select multiple results, create a download job, and view progress.

When a task finishes (completed or failed) the app posts a system notification. On Android 13+ the first launch asks for the "Notifications" permission (`POST_NOTIFICATIONS`); if it was denied, re-enable it from the system settings.

## Security Notes

The app uses HTTP to reach the local API (`usesCleartextTraffic=true`). Only use it on a trusted LAN. For access across networks, use a VPN (such as Tailscale) or put an HTTPS reverse proxy in front of the API, and always set a token.