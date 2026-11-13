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
| Standalone mode ("Standalone engine" on) | No configuration needed — uses `http://127.0.0.1:8081/` automatically |

If the API has a token configured, enter the same value in "API key".

## Features

Bottom tabs: **Search / Tasks / History / Music / Follow**.

- **Search**: keyword search plus multi-select site selection (leave every site unchecked to search all enabled sites), and an "Advanced filters" panel for word-count range and update date. Results can be added to the download queue with one tap, or followed via the "Follow" action.
- **Follow**: maintain the follow list (list / add / remove); "Check updates" searches each book for its latest chapter, and entries with new chapters can join the download queue with one tap.
- **Tasks**: running / queued / completed sections, progress bars and chapter counts, auto-refresh (every 2 seconds), cancellation, and one-tap retry for failed, cancelled, and interrupted tasks.
- **Login credential requests**: when a site requires login and no usable credential is stored, the task parks and a "Login credential requests" card appears at the top of the Tasks page. Tap "Provide credentials" to fill in the fields (password/cookie inputs are masked, required fields validated, prefill values seeded); submitting resumes the task.
- **History**: exported files; tapping one saves it to the public Downloads directory, or to the custom directory chosen in Settings (see below).
- **Music**: choose a platform, search songs, select multiple results, create a download job, and view progress.

## Search and Filters

### Site selection

The "Search sites" field opens a multi-select dialog rather than a text box:

- Select one or more sites to search only those sites.
- **Selecting nothing means every enabled site**; the field then reads "All sites".
- The selection is stored on the device and reused the next time the app starts.

### Advanced filters

The "Advanced filters" row below the search box expands optional conditions:

| Condition | Rule |
| --- | --- |
| Min words / Max words | Either field may be left blank; a value must be a whole number from `0` to `1000000000` |
| Updated after | Pick a `YYYY-MM-DD` date with the date picker; the selected day is **included**; use the clear icon to remove it |

- **Blank means no filtering**: with all three fields empty, the request is exactly the one sent before these filters existed.
- Invalid input (not a whole number, negative, above 1,000,000,000, or min words greater than max words) stops the search from being sent and opens the filter panel with the matching error.
- Filters run in the engine after ranking (the computer API or the standalone engine) and only narrow the result set; ordering is unchanged. With a word-count filter active, rows whose site did not report a word count are dropped; with a date filter active, rows without a parseable update date are dropped. That is not a request failure — "fewer results" usually means exactly this.
- "Active filters: N" counts the non-blank conditions and offers "Clear filters". The values survive tab switches and a system-initiated process death when the app is restored from Recents.

## Download Directory and File Saving

The History tab lists exported files; tapping one starts saving it. By default the file goes through the system DownloadManager into the public Downloads directory, with progress shown in the notification shade.

### Custom download directory (SAF)

In Settings → Download, tap "Choose directory" and grant a directory through the system picker (SAF, `ACTION_OPEN_DOCUMENT_TREE`):

- Files are written into a `NovelDL/` subfolder of the chosen directory, created when missing.
- Existing files are never overwritten: clashes become `name (1).ext`, `name (2).ext`, … (case-insensitive).
- The read/write grant is persisted with `takePersistableUriPermission`, so it survives a reboot.
- Settings shows the current target: "Download directory: <folder> (NovelDL subfolder)" or "Download directory: system Downloads".

When the grant stops working (revoked in system settings, the provider app was uninstalled or replaced, the directory was deleted or moved, or app data was restored to another device), Settings shows "The custom download directory is unavailable — pick it again in Settings" but keeps the stored choice so it can be replaced. Downloads from History then **fall back to the system Downloads directory** with the notice "The custom download directory is unavailable; saving to system Downloads instead". The grant is checked both before and during the write, so a revoked tree is never written to the wrong place.

"Use system Downloads" releases the persisted grant and returns to the default. If the directory is reachable but a file cannot be created (for example, the provider errors), the save fails with a message instead of silently writing somewhere else.

!!! note "Scope"
    The custom directory only affects downloads from the History tab. Downloading an exported file from a task's detail panel still goes through the system DownloadManager into system Downloads; music files follow the next section. See [Troubleshooting § custom download directory](./troubleshooting.en.md#51-the-custom-download-directory-saf-is-unavailable).

## Tasks, Notifications, and Recovery

The Tasks tab refreshes every 2 seconds and also supports pull-to-refresh; cards show progress and chapter counts, and expanding one reveals its exported files for download.

### Task snapshot and interrupted tasks

The computer-side API (or the on-device engine in standalone mode) keeps tasks in process memory, so they disappear when that process ends. The app keeps its own task snapshot on the device:

- After the app is killed or restarted, the Tasks tab still lists the previous queue, progress, and finished records instead of starting blank.
- Tasks that were queued or running at restart cannot still be running, so they are marked "Interrupted" and show "The app restarted and interrupted this task. Tap retry to continue."
- An in-flight task that the engine answers without listing is also marked "Interrupted" rather than looking active forever.
- Failed, cancelled, and interrupted tasks offer "Retry": the task is re-queued with its original site, book id, and title, and the app reports "Re-added to the download queue: <title>". Records without a site or book id cannot be retried. Within one session a task is re-queued at most once, so repeated taps cannot enqueue duplicates.

### Notifications and tap-to-locate

Finishing or failing a task posts a system notification; re-posts for the same task update in place instead of stacking.

- On Android 13+ the first launch asks for the "Notifications" permission (`POST_NOTIFICATIONS`). If it was denied, Settings → Notifications shows the state and "Open notification settings" leads to the system toggle; while notifications are off, the Tasks tab explains once why a finished task was silent.
- **Tapping a notification opens the app on the Tasks tab, expands the matching card, briefly highlights it, and scrolls it into view.** If the local snapshot already contains the task it is located immediately; otherwise the app waits for the first refresh before deciding.
- If the task is no longer listed after a refresh (for example, it was cleaned up), the app stays on the Tasks tab and reports "That task is no longer in the list". A notification for a malformed record without a usable task id only opens the Tasks tab and locates nothing.

## Music and Lyrics

On the Music tab, choose a platform, search songs, select several results, and create a download job; the page shows progress and the succeeded / skipped / failed counts.

- **Standalone mode**: when a track finishes, the app hands the audio to the system DownloadManager, which copies it into the public Downloads directory.
- **Remote mode**: files stay on the computer; the app only shows the server-side path and does not pull the audio onto the phone.
- **Lyrics**: the music engine saves lyrics by default (`save_lyric`) as `<stem>.lrc` next to the audio, plus `<stem>.zh.lrc` when a translation exists. The Android client currently **transfers only the audio file and does not copy the `.lrc` files into system Downloads**: in standalone mode they remain in the app-private engine directory, and in remote mode they remain on the computer. Use the desktop app or `novel-cli music download` when you need the lyric files.

## Built-in Update Check

The app checks for updates at launch, **at most once every 24 hours** (the timestamp of the previous check is cached on the device; a clock moved backwards triggers a new check as well). It reads the update manifest `latest.json` shipped with each release first — a stable URL that is not subject to GitHub API rate limits — and falls back to the GitHub Releases API only when the manifest is unavailable or has no Android entry.

Constraints on the update flow:

1. Only **HTTPS** download URLs are accepted.
2. The asset must carry a **SHA-256**; without one the download is refused — better to update by hand than to install something that cannot be verified.
3. The digest is recomputed after the download and **checked again right before the system installer is launched**, catching a file swapped between the two checks.
4. A verified APK is handed to the **system installer** for the user to confirm; the app does not and cannot install silently.

The release side has a gate as well: before publishing, `scripts/release.py` runs `scripts/check_android_alignment.py` to verify zip-entry and `PT_LOAD` segment alignment of the release APK. A package that is not **16 KB page aligned** is never published — that alignment is what lets Android 15+ devices with 16 KB pages load its native libraries.

## Standalone Engine (No PC Needed)

The app supports two operating modes, switched at any time via the "Standalone engine (no PC needed)" toggle on the Settings page:

- **Remote mode (default, toggle off)**: the phone remote-controls novel-api on the computer, where searching, downloading, and everything else run; the computer must run the API service on the same LAN as the phone — the original mode described above.
- **Standalone mode (toggle on)**: no computer needed — the phone runs the full download engine itself. The APK embeds a Python 3.12 runtime via Chaquopy (both arm64-v8a and x86_64 ABIs) and bundles the entire novel_downloader service layer; the engine serves the same REST contract as novel-api on the loopback address `http://127.0.0.1:8081/`. Search, tasks, history, follow, and all other features run on the phone, and data, tasks, and history stay on the device.

When the toggle is on, the app starts the local engine at launch before initializing the API client (the API address is fixed to the loopback address automatically, so no configuration is needed — see "First-time Configuration"); turning the toggle off switches back to remote mode and reconnects to novel-api on the computer.

Notes:

- **Music works in standalone mode**: the engine bundles requests / mutagen / Pillow / qrcode / websocket-client / m3u8 / emoji and friends. Tracks are written to the engine directory first and then handed to the system DownloadManager, so they land in the public Downloads folder.
- **Larger APK**: bundling the Python runtime and its dependencies (aiohttp, lxml, cssselect, pycryptodome, rich, platformdirs, and more) grows the APK from about 12 MB to roughly 68 MB (the music extras add several MB).
- The embedded engine binds to the loopback address `127.0.0.1` only, so it is reachable by the app itself and not by other devices on the LAN.

## Security Notes

The app uses HTTP to reach the local API (`usesCleartextTraffic=true`). Only use it on a trusted LAN. For access across networks, use a VPN (such as Tailscale) or put an HTTPS reverse proxy in front of the API, and always set a token.