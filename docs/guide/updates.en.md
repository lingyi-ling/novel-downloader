# Updates and auto-update

The desktop app, the CLI and the Android client all check for new versions. They share one **update manifest** and the same verification rules; only the installation step differs.

## How often the check runs

| Client | When | Limit |
| --- | --- | --- |
| Desktop | after start-up, triggered by the front end | once per 24 hours |
| CLI | silently after any command (TTY only, never with `--json`) | once per 24 hours |
| Android | at start-up | once per 24 hours |

The result is cached, so restarting within that window does not hit the network again.

## Turning it off

```toml
[general]
update_check = "auto"    # default: silent check after a command
# update_check = "manual"  # only when running novel-cli update
# update_check = "off"     # never
```

The desktop app has an "automatically check for updates" switch, and the Android settings screen has the same toggle.

## Checking manually

```bash
novel-cli update --check   # report only
novel-cli update           # check, then ask before downloading
novel-cli update --yes     # download and verify without asking
novel-cli update --json    # machine-readable output
```

`--yes` downloads into the app data directory (`<app_home>/updates/`) and verifies the file. It **never** runs or installs anything. `novel-cli update` requests `shape="cli"` explicitly and only downloads `novel-cli-<version>-<platform>.zip`; it never downloads or runs a desktop installer, and when the release has no matching CLI asset it stops (`error` is `no-cli-asset` with `--json --yes`) instead of falling back to `NovelDownloader-setup-*.exe`.

To install the CLI: close the running CLI, extract that zip, and replace the installed CLI directory with its contents (on Windows standalone installs this is usually `%LOCALAPPDATA%\Programs\novel-downloader\novel-cli\`). The desktop installer/portable packages are handled by the auto-update flow below.

## Desktop auto-update

The desktop app checks once every 24 hours by default; a newer version is downloaded in the background and the sidebar shows byte-level progress. Transfers use an HTTP `Range` request to reuse the existing `.part` file, so an interrupted download resumes instead of starting over, with up to 3 retries.

- **Installer build**: on quit, the verified Inno Setup installer starts silently and replaces the installation directory in place; once the silent install finishes, Setup starts the new build itself, so nothing needs to be opened manually.
- **Portable build**: the downloaded asset is a zip; after the app exits, a PowerShell helper waits for the process to end, expands the zip to `%TEMP%`, deletes `_internal` in the current folder, then copies the new files over it and restarts the app. That removes plugins dropped from the product, while settings and downloads under `app_home` stay untouched. It does not install a second copy into `%LOCALAPPDATA%`.
- Leftover `.part` files older than a day and finished packages older than a week are pruned from the update folder.

## What gets verified

1. The download URL in the manifest must be **HTTPS**;
2. a **SHA-256** must be present — without one the client refuses to download rather than install something it cannot verify;
3. the digest is checked after the download **and again right before the installer starts**, so a file swapped in between is caught;
4. a file that fails verification is deleted, never left half-written.

On Windows the Inno Setup installer only replaces files after the app has exited. On Android the system installer performs the installation; the app cannot install silently.

## The update manifest

Every release ships `latest.json`:

```
https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json
```

It lists the exact file name, size, SHA-256 and download URL for each platform, and is generated from the assets that were **actually uploaded**. Clients prefer it and fall back to the GitHub API only when it is unavailable — the unauthenticated API allows 60 requests per hour, the manifest has no such limit.

> Once a trusted signing key is configured the client **stops falling back** to the API: a missing or badly signed manifest is an error. That is deliberate — allowing the fallback would let an attacker delete the manifest to bypass signature checking. With signing enabled, every release must ship `latest.json`.

Entries are per **package shape**:

| Key | Artifact |
| --- | --- |
| `windows-x64-installer` | `NovelDownloader-setup-<ver>-x64.exe` |
| `windows-x64-portable` | `NovelDownloader-<ver>-windows-x64.zip` |
| `windows-x64-cli` | `novel-cli-<ver>-windows-x64.zip` |
| `windows-x64-api` | `novel-api-<ver>-windows-x64.zip` |
| `android` | `novel-downloader-<ver>-android.apk` |

`novel-cli update` always selects the `cli` entry; when there is no `*-cli` entry
for the current platform it does not fall back to installer / portable / api. The
install shape is detected from the uninstall registry (`InstallLocation` /
`DisplayIcon`, falling back to `unins000.exe`), so an installer copy in a custom
directory still updates to the installer. A copy installed from the installer
only ever updates to the installer, and a portable copy only to the portable
archive.

## Known limitations

- **Android cannot install silently**: that needs a system signature permission ordinary apps do not have. The flow is download → verify → hand off to the system installer → user confirms.
- **Only Windows and Android artifacts are published**: macOS/Linux need their own runners; PyInstaller cannot cross-compile.
- **The manifest is unsigned by default**: the trust anchor is HTTPS plus the per-asset SHA-256. Ed25519 signing is built in and can be enabled (see `AGENTS.md`); once a key is trusted the client rejects unsigned or badly signed manifests.
