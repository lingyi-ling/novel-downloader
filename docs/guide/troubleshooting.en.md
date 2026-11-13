# Troubleshooting

This page covers common problems and their diagnosis steps. Commands are run from the project root or in an environment where `novel-cli` is installed; on Windows, Git Bash or PowerShell both work. Replace `<site>`, `<book_id>`, and `<IP>` in the examples with real values.

---

## 0. Collect Basic Information First

Start with the built-in diagnostic command. It checks the Python version, package installation, `settings.toml`, output directory, sites/searchers, music dependencies, and whether the API port is available:

```bash
novel-cli doctor

# Check a different port
novel-cli doctor --port 8080
```

Failing checks include suggestions; `✓` means pass and `✗` means failure (optional dependencies are labelled `optional`).

If you suspect local-state problems such as "missing directories / piling-up temporary files", preview the automatic repair first and then apply it:

```bash
novel-cli doctor --fix --dry-run   # report what would be created/removed, zero changes
novel-cli doctor --fix             # create missing output/cache/log dirs, clean stale .tmp/.part
```

`--fix` only acts inside the configured output, cache, and log directories, refuses `..` and symlinks, and never changes configuration, exports, or login state. By default it removes only temporary files older than 7 days (adjust with `--fix-age-days`). It does not repair configuration errors, missing dependencies, or a busy port; follow the suggestions on those check rows instead.

Then collect the version and logs:

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.python_version(), novel_downloader.__version__)"
tail -n 100 logs/novel_downloader.log
```

If the log directory is not the default `./logs`, check `general.debug.log_dir` in your configuration:

```bash
grep -n "log_dir\|log_level" settings.toml
```

For more detailed logs, set `general.debug.log_level` to `DEBUG` in `settings.toml` and retry.

---

## 1. Site Returns 403 / Cloudflare Verification

**Symptoms**: requests are rejected, return `403`, or show a Cloudflare verification page or TLS handshake failure.

**Verify**: first request the site directly with browser-like headers (replace `example.com` with the target site):

```bash
curl -I -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36" "https://example.com/"
```

If `curl` returns `403` or `5xx`, the site is sensitive to the current network fingerprint, usually TLS fingerprint or HTTP version.

Check whether the optional backends are installed:

```bash
python -c "import importlib.util as u; mods=["mutagen", "pathvalidate", "PIL", "qrcode", "websocket", "m3u8", "emoji", "bleach", "filetype", "puremagic", "fake_useragent", "json_repair", "tinytag", "tabulate", "prettytable", "wcwidth", "prompt_toolkit", "aigpy", "ytmusicapi", "pywidevine"]; print([m for m in mods if not u.find_spec(m)])"
```

**Fix**:

1. Install the backends:

   ```bash
   pip install "novel-downloader[all-backends]"
   ```

2. Switch the backend in `settings.toml`; try `curl_cffi` with browser impersonation first:

   ```toml
   [general]
   backend = "curl_cffi"
   impersonate = "chrome136"

   # Or try httpx + HTTP/2:
   # backend = "httpx"
   # http2 = true
   ```

3. Retry the target site:

   ```bash
   novel-cli download --site <site> <book_id>
   ```

4. If the site returns `503 Service Temporarily Unavailable`, lower the request rate:

   ```toml
   [general]
   max_rps = 1.0
   request_interval = 1.0
   ```

   Already-downloaded chapters are resumed from `chapter.raw.sqlite`, so nothing is fetched twice.

> Different sites can react in opposite ways: some work with HTTP/2, while others only work with HTTP/1.1. Adjust per site instead of forcing one global backend.

---

## 2. Login Expired / Invalid Cookies

**Symptoms**: the site says you are not logged in, only some chapters are visible, or a permission error is returned.

**Verify**:

```bash
# List sites that support interactive login
novel-cli login --list

# Check whether cached cookie files exist
ls -l novel_cache/<site>/*.cookies
```

**Fix**:

1. Log in interactively again (recommended):

   ```bash
   novel-cli login <site>
   ```

   After a successful login, cookies are written to `<cache dir>/<site>/aiohttp.cookies`.

2. Or clear that site's cache and retry:

   ```bash
   novel-cli clean cache --site <site> --yes
   ```

3. You can also provide cookies or credentials in the configuration:

   ```toml
   [sites.<site>]
   login_required = true
   cookies = "complete cookie string"
   # Or:
   # username = "your_username"
   # password = "your_password"
   ```

4. Make sure the site key in the configuration matches the `--site` value you use (for example `fanqienovel`, not the display name).

> Cookies supplied this way are not renewed automatically; copy fresh cookies or log in again after they expire.

---

### Fanqie: some chapters are very short or marked as failed

For anonymous visitors Fanqie locks chapters **one by one**: the table of
contents carries an `isChapterLock` flag per chapter, and a locked chapter's
reader page returns only a ~200-character preview.

What the downloader does:

- chapters flagged as locked are recorded as `accessible: false` and skipped
  by default, so it does not spend requests on them;
- if fetching is forced (`fetch_inaccessible = true`), the parser recognises
  the preview and reports the chapter as failed instead of writing 200
  characters to disk as if it were the chapter.

Those chapters need a signed-in session:

```bash
novel-cli login fanqienovel
```

or a Cookie copied from the browser (see the previous section). Anonymous
access covers the unlocked chapters of a book; that limit comes from the site,
not from this tool.

---

## 3. API Returns 401 / Port Already in Use

### 3.1 401 Unauthorized

**Cause**: `novel-api` was started with `--token` or the `NOVEL_API_TOKEN` environment variable, and the request did not carry a matching token.

**Verify**:

```bash
# Returns 401 without a token
curl -i http://127.0.0.1:8081/api/v1/health

# Returns 200 with the correct token
export NOVEL_API_TOKEN="your-token"
curl -i -H "Authorization: Bearer $NOVEL_API_TOKEN" http://127.0.0.1:8081/api/v1/health
```

In PowerShell, set the environment variable like this:

```powershell
$env:NOVEL_API_TOKEN = "your-token"
curl.exe -i -H "Authorization: Bearer $env:NOVEL_API_TOKEN" http://127.0.0.1:8081/api/v1/health
```

`X-API-Key` also works:

```bash
curl -i -H "X-API-Key: your-token" http://127.0.0.1:8081/api/v1/health
```

**Fix**:

* Make sure the client and the server use the same token.
* For local-only use, you can omit the token:

  ```bash
  novel-api
  ```

* On Android, enter the same value under "API key" in Settings.

### 3.2 Port Already in Use

**Symptoms**: startup fails with `[Errno 10048]` (Windows) or `Address already in use` (Linux/macOS).

**Verify**:

```bash
netstat -ano | grep ":8081"
```

PowerShell:

```powershell
Get-NetTCPConnection -LocalPort 8081 | Select-Object LocalAddress,LocalPort,OwningProcess
Get-Process -Id <PID>
```

**Fix**:

1. Stop the process holding the port, or
2. Start on another port:

   ```bash
   novel-api --port 8082
   ```

   Then change the Android API address to `http://<IP>:8082/`.

3. Make sure request paths include `/api/v1`, for example `http://127.0.0.1:8081/api/v1/sites`; the root path serves no API endpoints.

---

## 4. Paths Containing Chinese Characters or Spaces

**Symptoms**: export fails, resource files are missing, third-party tools report encoding errors, or Android download links break.

**Verify**: confirm that Python can read and write a Chinese path:

```bash
python -c "import sys; from pathlib import Path; p = Path.cwd(); print('filesystem encoding:', sys.getfilesystemencoding()); q = p / '中文测试目录'; q.mkdir(exist_ok=True); print('ok:', q.resolve()); q.rmdir()"
```

If the command succeeds, Python itself handles the path fine; the problem usually comes from the terminal code page, an old third-party tool, or how the path is written in the configuration.

**Fix**:

1. Point data directories at a pure-ASCII path (use forward slashes to avoid TOML escaping issues):

   ```toml
   [general]
   raw_data_dir = "C:/novel-downloader-data/raw_data"
   output_dir = "C:/novel-downloader-data/downloads"
   cache_dir = "C:/novel-downloader-data/cache"
   ```

2. Or move the application home to a pure-ASCII path with `NOVEL_HOME`:

   ```bash
   # Git Bash
   export NOVEL_HOME="C:/novel-downloader-home"
   ```

   ```powershell
   # PowerShell
   $env:NOVEL_HOME = "C:\novel-downloader-home"
   ```

3. If the Windows console shows garbled text, switch to the UTF-8 code page:

   ```powershell
   chcp 65001
   ```

4. If the path is inside a OneDrive-synced folder, pause syncing before exporting to avoid locked files.

---

## 5. Android Cannot Reach the Computer

**Symptoms**: search or tasks spin forever, or the app reports a connection failure or timeout.

**Verify and fix**:

1. Make sure the API binds to a LAN address rather than loopback only:

   ```bash
   novel-api --host 0.0.0.0 --port 8081 --token "your-token"
   ```

2. Find the computer's LAN IPv4 address:

   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '127.*'} | Select-Object IPAddress,InterfaceAlias
   ```

   In Git Bash you can also run `ipconfig` and look for the IPv4 address of the active Wi-Fi/Ethernet adapter.

3. Confirm that the phone and computer are on the same subnet:

   ```bash
   ping -n 4 <computer IP>
   ```

4. Open `http://<computer IP>:8081/api/v1/health` in the phone's browser. `/docs` is disabled automatically on a non-loopback bind, so use the health endpoint: a `401` (`Invalid or missing API token...`) means the network path works and authentication is enforced; a timeout or refused connection means you should continue with the firewall checks.

5. Allow the port through Windows Firewall (administrator PowerShell):

   ```powershell
   New-NetFirewallRule -DisplayName "NovelDownloader API 8081" -Direction Inbound -Protocol TCP -LocalPort 8081 -Action Allow
   ```

   Or from the command line:

   ```bash
   netsh advfirewall firewall add rule name="NovelDownloader API 8081" dir=in action=allow protocol=TCP localport=8081
   ```

6. The emulator uses `http://10.0.2.2:8081/`; a physical device uses `http://<computer IP>:8081/`. Keep the trailing `/`.

7. Disable the router's "AP isolation / client isolation" and make sure the phone is not on a guest network.

8. The Android app uses cleartext HTTP. For access across networks, use a VPN (such as Tailscale) or an HTTPS reverse proxy, and always set a token.

### 5.1 The Custom Download Directory (SAF) Is Unavailable

**Symptoms**: a download directory was selected in Settings, but files still land in the system Downloads folder; or Settings shows the red message "The custom download directory is unavailable — pick it again in Settings".

**Cause**: Android grants a directory through SAF (`ACTION_OPEN_DOCUMENT_TREE`) and the app stores a persisted URI permission. The grant stops working when the user revokes it in system settings, the provider app that owns the directory is uninstalled/changed, the directory is deleted or moved, or app data is restored to another device.

**Behaviour and fix**:

* Settings keeps the previously selected directory (so it can be replaced) and clearly marks it as unavailable. History downloads **fall back to the system Downloads directory** and say so ("The custom download directory is unavailable; saving to system Downloads instead"). Files are never written to the wrong place, and the download itself does not fail.
* Choose the directory again and confirm the system grant to restore it. The picker requests and persists read/write permission; if that fails, the app says "Could not keep write access to that directory — choose another one" and leaves the previous working directory untouched.
* "Reset" releases the persisted permission and returns to the system Downloads directory.
* Files are written into a `NovelDL/` subdirectory of the chosen tree. Name clashes become `name (1).ext`, `name (2).ext`, … (case-insensitive), so existing files are never silently overwritten.
* This setting only affects downloads of exported files from the History tab; music files keep using the music engine's own directories / system download flow.

---

## 6. Music Download Missing Dependencies

**Symptoms**: `novel-cli music ...` reports a missing module, the platform list is empty, or downloads fail.

**Verify**:

```bash
novel-cli music platforms
python -c "import importlib.util as u; print('mutagen', bool(u.find_spec('mutagen'))); print('pathvalidate', bool(u.find_spec('pathvalidate'))); print('PIL', bool(u.find_spec('PIL'))); print('qrcode', bool(u.find_spec('qrcode'))); print('websocket', bool(u.find_spec('websocket')))"
```

**Fix**:

1. Install the music extra:

   ```bash
   pip install "novel-downloader[music]"
   ```

2. List platforms again:

   ```bash
   novel-cli music platforms
   ```

3. Some platforms have extra requirements:

   * Apple Music / SoundCloud need `pywidevine`; DRM decryption also needs your own CDM (a `.wvd` device file), and SoundCloud's HLS downloads need an external `N_m3u8DL-RE`.
   * YouTube search needs `ytmusicapi` (not Node); downloads rely on a third-party parsing service and fail when it is unreachable.
   * Some HLS/streaming formats need `ffmpeg` installed on the system.

   Check:

   ```bash
   python -c "import importlib.util as u, shutil; print('pywidevine', bool(u.find_spec('pywidevine'))); print('ytmusicapi', bool(u.find_spec('ytmusicapi'))); print('ffmpeg', shutil.which('ffmpeg'))"
   ```

4. Platforms that need account credentials (such as Spotify, TIDAL, and Soda Music) also require cookies or tokens in the platform credentials; otherwise search may return nothing.

5. If one platform fails, try another to confirm that the basic feature works:

   ```bash
   novel-cli music search "keyword" --platform netease --limit 5
   ```

**No lyrics file (`.lrc` missing)**:

1. Confirm `save_lyric` is not disabled in the music settings (default `true`); it lives in `settings.json` under the music data directory.
2. The platform may simply have no lyrics, or may return a placeholder value; no file is created and the audio still succeeds.
3. Lyric files share the audio name: `<title> - <artist>.lrc`, with `.zh.lrc` when a translation exists (a translation alone is written to `.lrc`). If the audio uses the ` [<id>]` deduplication suffix, the lyrics carry it too — look next to the audio file with the same stem.
4. When the audio already exists and `skip_existing` skips it, lyrics are backfilled only if `.lrc` is missing; existing lyrics are neither overwritten nor re-fetched.
5. A lyric response larger than 1 MiB is treated as an error page and skipped (a WARNING is logged). Set the log level to `DEBUG` to see the exact reason.

---

## 7. Proxy / VPN Causes Timeouts or SSL Errors

**Symptoms**: every site times out, SSL certificate errors appear, or behavior changes when a VPN is enabled or disabled.

**Verify**:

```bash
# Inspect current environment variables
env | grep -i proxy

# Test whether the proxy works
curl -x http://127.0.0.1:7890 -I https://www.google.com
```

**Fix**:

1. Configure the proxy explicitly in `settings.toml`:

   ```toml
   [general]
   proxy = "http://127.0.0.1:7890"
   # If the proxy requires authentication:
   # proxy_user = "username"
   # proxy_pass = "password"
   trust_env = false
   ```

2. To reuse proxy settings from system environment variables, set:

   ```toml
   [general]
   trust_env = true
   ```

3. Avoid enabling both the system proxy and the `proxy` setting, which can create a double proxy chain.

4. SSL errors usually come from the proxy's man-in-the-middle certificate. You can temporarily disable verification to narrow down the cause (for diagnosis only, not recommended long-term):

   ```toml
   [general]
   verify_ssl = false
   ```

5. Switch backends to check for TLS compatibility issues:

   ```toml
   [general]
   backend = "httpx"
   http2 = true
   ```

6. Music platforms may read the system proxy themselves. If novels download but music times out, check the system proxy and platform reachability first.

---

## 8. Some Music Platforms Do Not Respond / Are Very Slow in Aggregate Search

**Symptoms**: searching "all platforms" returns only a few songs after the progress bar finishes; some platforms never return anything, or the whole search waits out the budget. Searching that platform explicitly may still work.

**How it works** (`apps/music/aggregate.py`):

* Each platform runs in its own thread, and the aggregate search has a wall-clock budget: 5 seconds by default (`DEFAULT_TIMEOUT`), with 6 seconds passed by the desktop progressive aggregate search (`SEARCH_SITE_TIMEOUT`).
* As soon as one platform returns results, the search waits 2 more seconds and stops (`EARLY_EXIT_GRACE`); platforms that have not answered are skipped for this search and are not counted as failures.
* A platform that raises, or that still has not answered when the budget runs out, is recorded as failed and short-circuited for **10 minutes** (`FAILURE_TTL`), during which aggregate searches skip it; platforms skipped by the 2-second early exit are not marked as failures.
* Two consecutive searches that take ≥2 seconds and return zero results mark the platform as "slow and empty", skipping it for **5 minutes** (`SLOW_EMPTY_STREAK` / `SLOW_EMPTY_TTL`).
* This state is written to `search_stats.json` in the music data directory and survives a restart. The default is `%LOCALAPPDATA%\novel_downloader\music\search_stats.json`; set `NOVEL_MUSIC_HOME` to replace the whole music data directory.

**Diagnose**:

1. Verify one platform first; an explicitly selected platform bypasses the breaker:

   ```bash
   novel-cli music search "keyword" --platform netease --limit 5
   ```

2. Wait 10 minutes (failure) or 5 minutes (slow-and-empty) for automatic recovery. You can also quit every client and delete `search_stats.json`; the next search recreates it.
3. To keep your settings but start from a clean state, point `NOVEL_MUSIC_HOME` at an empty directory for one run:

   ```bash
   # Git Bash
   export NOVEL_MUSIC_HOME="$TEMP/novel-music-clean"
   ```

   ```powershell
   # PowerShell
   $env:NOVEL_MUSIC_HOME = Join-Path $env:TEMP "novel-music-clean"
   ```

4. If every platform is in the breaker window, the code clears the cache once and retries automatically. If the result is still empty, check dependencies, credentials, and network as described in section 6.

---

## 9. Desktop Auto-update: Installer and Portable Copies Behave Differently

**Symptoms**: the app exits after an update but is not upgraded; two copies appear after upgrading; or the update reports that PowerShell was not found.

**How it works**:

* The client first detects the install shape: it prefers the uninstall registry's `InstallLocation` / `DisplayIcon` values (under the `{AppId}_is1` key), then an `unins000.exe` marker next to the executable, and only then the default `%LOCALAPPDATA%\Programs\NovelDownloader` directory. A copy installed into a custom directory is therefore still an **installer** copy; any other frozen folder is **portable**, and a source checkout is `source`.
* An **installer** copy downloads `NovelDownloader-setup-<version>-x64.exe` and launches Inno Setup silently after exit, overwriting the install directory in place. Setup removes the old `_internal` before writing the new build and uses the named mutex `NovelDownloader-UpdateMutex` to close a running instance; after the silent install it starts the new build itself, so nothing needs to be opened manually.
* A **portable** copy downloads a zip. After exit, the PowerShell helper `updates/_apply_portable_update.ps1` waits for the process to end, expands the zip to `%TEMP%`, deletes `_internal` in the current program folder, then copies the new files over and restarts the app. Plugins dropped from the product do not linger, and settings and downloads under `app_home` are untouched. It does not install a second copy into `%LOCALAPPDATA%`.
* Portable updates need `powershell` or `pwsh`. If neither is found, the update aborts and asks you to extract the archive manually.

**Diagnose**:

1. Update packages live in `%LOCALAPPDATA%\novel_downloader\updates\`. After a failure the zip/installer is usually still there; for a portable copy you can extract it over the folder containing `NovelDownloader.exe` and start the app manually.
2. Cleanup rules: unfinished `.part` files are kept for 1 day, complete installers/zips for 7 days, then removed automatically.
3. If PowerShell is missing: manually extract the zip from `updates\` and copy every file over the current program folder.
4. If the upgrade seems ineffective or two copies exist: the installer directory comes from the uninstall registry (`InstallLocation` / `DisplayIcon`) or `unins000.exe`, so it need not be the default `%LOCALAPPDATA%\Programs\NovelDownloader`; confirm which copy you launched. The `v...` label at the bottom of the sidebar is the running version.
5. Logs are in `%LOCALAPPDATA%\novel_downloader\logs\novel-desktop.log`; search for `Launched the update installer` or `Launched the portable update helper` to confirm that the updater actually started.

See [Installation](./installation.en.md) for how install, upgrade, and uninstall treat user data.

---

## 10. TIDAL / YouTube / Apple Music / SoundCloud: Searchable Does Not Mean Downloadable

**Symptoms**: these platforms appear in the list, but search is empty, only previews are returned, or download fails with an HLS-not-supported or no-downloadable-link error (the UI currently shows the Chinese reason strings `HLS 流暂不支持直链下载` / `该平台未返回可下载直链`).

**What to know**:

* Dependencies are bundled: the desktop installer and `pip install "novel-downloader[music]"` both include `aigpy` (TIDAL), `ytmusicapi` (YouTube), and `pywidevine` (DRM for Apple Music / SoundCloud). Node is not required.
* "Searchable" and "downloadable" are separate: these platforms try to resolve a downloadable direct link during search. When no link is available, the list may be empty or contain only previews.
* **TIDAL**: you must configure a token JSON in the platform credentials, otherwise search is empty. Credentials live under the `tidal` key in `<music data dir>/platform_creds.json`; the default is `%LOCALAPPDATA%\novel_downloader\music\platform_creds.json`. The legacy music desktop tool has a "Platform credentials" dialog that writes this file, or you can edit the JSON directly.
* **Apple Music**: an account is required for full tracks; without one you usually get preview clips only. Access to `music.apple.com` is also required.
* **SoundCloud**: search works; DRM/HLS tracks need your own CDM (`.wvd` device file) and an external `N_m3u8DL-RE`.
* For these four musicdl platforms, the built-in downloader accepts only non-HLS direct links: resolution rejects HLS (m3u8) immediately with the HLS-not-supported reason. Therefore, even with a CDM and `N_m3u8DL-RE`, DRM/HLS tracks from Apple Music / SoundCloud cannot be downloaded in-app; use an external tool that supports Widevine and `N_m3u8DL-RE`. TIDAL / YouTube work only when the platform returns an HTTP direct link.

**Verify**:

```bash
novel-cli music platforms
novel-cli music search "keyword" --platform tidal --limit 5
```

* The platform is missing from the list: a dependency or registration condition is unmet (for example, TIDAL is not registered without `aigpy`).
* It is listed but search is empty: check credentials and network first.
* Search works but download fails: read the error. The HLS-not-supported and no-downloadable-direct-link reasons are expected limits of this version, not network failures.

See section 6 for dependency installation and self-check commands.

---

## 11. Clicking Download in Search Results Says "Site and book ID are required"

**Symptoms**: clicking Download or Follow on a search result card shows "Site and book ID are required", even though the card displays a title and a site.

**Cause**: before v1.28.1 the desktop page put the site in `data-add` / `data-follow`, but the click handler read `dataset.site`, so it always got an empty string. v1.28.1 reads `dataset.add` / `dataset.follow` instead, restoring both download and follow.

**Fix**:

1. Upgrade to v1.28.1 or newer. The `v...` label at the bottom of the sidebar is the running version; if an installer copy and a portable copy coexist, confirm you launched the upgraded one.
2. If it still happens after upgrading, collect:
   * the version shown at the bottom of the sidebar;
   * the log at `%LOCALAPPDATA%\novel_downloader\logs\novel-desktop.log`;
   * the site key, the search keyword, and whether every result for that keyword fails.
3. If only one site's results fail while others work, that site's search results may not contain a book ID (the card's `data-bid` is empty). Empty arguments are rejected before the task queue, so `Failed to queue` will not appear in the log; include the site and keyword in the issue.

---

## 12. Music Playlist Export Returns 400 / Music Jobs Return 429

**Symptoms**: `POST /api/v1/music/export` returns `400`; or `POST /api/v1/music/jobs`
returns `429` even though the per-IP request rate limit was not hit.

**Read `detail` first** (both messages are fixed and never echo the submitted
path):

| `detail` | Meaning |
| --- | --- |
| `Music out_dir must be inside the music output directory` | Download directory escaped the allowed roots |
| `Playlist out_path must be a .m3u/.m3u8 file inside the music output directory` | Playlist path escaped the roots or had a bad suffix |
| `Too many music jobs are already running (limit 4); retry later` | Active music-job limit reached |

**Diagnosing the `out_dir` / `out_path` 400**:

1. Only two roots are writable: the music setting `out_dir` and the platform
   default music directory (usually `~/Music/novel_downloader`). On Windows the
   music settings file is
   `%LOCALAPPDATA%\novel_downloader\music\settings.json`; check its `out_dir`.
   Setting `NOVEL_MUSIC_HOME` replaces the whole music data directory.

   ```bash
   # Git Bash
   cat "$LOCALAPPDATA/novel_downloader/music/settings.json"
   ```

2. Use an absolute path inside one of those roots, or omit the field to use the
   default directory. Relative paths are resolved against the **default
   directory**, not the API process's current directory.
3. If `out_path` has a suffix, it must be `.m3u` / `.m3u8`; `.txt`, `.json`, and
   so on are rejected. A value without a suffix is treated as a directory. `..`,
   a symlink pointing outside the roots, and an absolute path outside the roots
   are all rejected even when the target exists.
4. A `500` instead of `400` means the path passed validation but the write
   failed (permissions, disk, locked file). The detail only contains the
   exception class name (`"Could not write playlist: <class>"`); the cause is in
   the server log.

**Diagnosing the `429`**:

1. Confirm the detail says `Too many music jobs...` rather than the rate-limit
   message. Both carry `Retry-After`: the job-cap `429` is fixed at `5` seconds
   (wait roughly one download slot), while the rate-limit `429` depends on when
   the window drains.
2. At most `4` music jobs may be `queued` / `running` at once (`done` / `failed`
   do not count). Poll the existing jobs until they reach a terminal state, then
   submit again:

   ```bash
   curl -s "$BASE/api/v1/music/jobs/<job_id>" -H "Authorization: Bearer $TOKEN"
   ```

3. For batch downloads you can also use the synchronous
   `POST /api/v1/music/download` endpoint (it blocks until every track
   finishes), or split the work into smaller batches.

---

## 13. Auto-update Refuses an http Asset / Download Was Cleaned Up

**Symptoms**: the desktop app reports
`Refusing to download an update from an insecure source`; `novel-cli update
--yes` exits with code `1`; `novel-cli update --json --yes` has `error` set to
`insecure-url`, `missing-digest`, or `no-cli-asset`; a `.part` file is left in
`updates\` after an interrupted transfer.

**How it works** (the desktop app and CLI share `apps/common/updater.py`):

* `novel-cli update` requests `shape="cli"` explicitly and accepts only
  `novel-cli-<version>-<platform>.zip`. Even if the manifest/API only returns a
  desktop package, that fallback is discarded and the command stops; it never
  downloads or runs `NovelDownloader-setup-*.exe`. Accordingly, the `--json`
  `asset_shape` is `cli` when a CLI asset was selected, and `null` when there is
  no CLI asset or the check failed.
* The asset URL in the release manifest must be `https://`. The CLI refuses a
  plaintext address before prompting or downloading; the desktop client does the
  same.
* The downloader itself accepts only `http://` / `https://`; if an HTTPS request
  is redirected to plain HTTP, it refuses the transfer (a digest proves
  integrity, not confidentiality).
* One asset is capped at `1 GiB` (`1024**3` bytes), so a tampered manifest or
  mirror cannot fill the disk.
* On failure: a non-resume transfer deletes `.part` before the **next retry**; a
  resumable transfer keeps the downloaded prefix for the next `Range` attempt; a
  SHA-256 mismatch always deletes `.part` (a stale prefix from an older release
  is useless). If every retry fails, the unfinished file may remain under
  `updates\`; the desktop app removes it after 1 day.

**Diagnose**:

1. `insecure-url`: use the official release page or an HTTPS mirror.
   `NOVEL_UPDATE_MIRROR` is a user-configured prefix, but a mirror that redirects
   HTTPS to HTTP is still refused during download.
2. `missing-digest`: that release provides no SHA-256 for your platform asset, so
   the client will not download an unverifiable file; download it manually from
   the release page and check the digest.
3. `no-cli-asset`: the release has no matching CLI asset (`novel-cli-*.zip`) for
   the current platform (for example, it was not uploaded or the architecture is
   unsupported). The CLI does not fall back to a desktop installer; use
   `pip install -U novel-downloader`, or download the matching CLI zip manually
   from the release page.
4. Repeated checksum failures: close the app, delete the corresponding `.part`
   under `%LOCALAPPDATA%\novel_downloader\updates\`, and retry. The desktop app
   also removes `.part` files older than 1 day automatically.
5. For a machine-readable reason, run `novel-cli update --json --yes`: it never
   prompts and puts the download failure in the `error` field; `asset_shape`
   reports the selected asset shape (for the CLI it is `cli` or `null`). `--json`
   alone behaves like `--check` and never attempts a download.

---

## 14. Confirm What a Clean or Restore Will Do First

`clean cache`, `clean book`, and `storage --clean` all support `--dry-run`: they
report the paths and sizes **without deleting anything**, and use the same path
validation as a real removal (escaping site keys and protected directories still
raise an error).

```bash
novel-cli clean cache --site qidian --dry-run
novel-cli clean book --site qidian 1010868264 --all --dry-run
novel-cli storage --clean --dry-run
```

For scripts, add `--json` (it requires `--dry-run`) to get a single-line
`{"dry_run", "command", "site", "book_id", "book_ids", "items", "paths", "files", "bytes"}`.

If a chapter database was deleted by mistake, restore it from a backup:

```bash
# Back up every chapter database first
novel-cli storage backup --out ~/novel-backups

# Read-only check of every live database under raw_data (modifies nothing)
novel-cli storage verify

# Restore from a backup; replacing an existing database requires -y
novel-cli storage restore --from ~/novel-backups/<site>/<book>/chapter.raw.<timestamp>.sqlite -y
```

`restore` validates the backup (integrity, schema version, required columns) before
atomically replacing the target; when an existing database is replaced, the old one
is preserved as `*.pre-restore-*.bak` in the same directory. The target must be
inside `raw_data_dir`.

The backup contains only the chapter databases under `raw_data`; it does not include
cookies/tokens, `settings.toml`, logs, or media files, so sign in again after a restore,
and treat the plaintext backup as sensitive data. See the
[full “back up → verify (SHA-256 / `PRAGMA integrity_check`) → restore” workflow](./cli-examples.en.md#9-storage-subcommand)
in the CLI usage examples.

For scheduled backups, `--keep` rotates old copies automatically. Preview first, then apply:

```bash
# Report which old backups would be deleted; the new backup is still written, zero deletion
novel-cli storage backup --out ~/novel-backups --keep 7 --dry-run

# Keep only the newest 7 backups per book and per stage
novel-cli storage backup --out ~/novel-backups --keep 7
```

`--keep` deletes only files under `--out` whose names strictly match `chapter.<stage>.<UTC timestamp>.sqlite`; directories, symlinks, hand-renamed files, and files left by older versions are never touched, and backups written by the current run are always protected. `--keep 0` disables pruning; a negative value or using `--dry-run` without `--keep` exits with a usage error (`2`). A failed delete exits `1`, but completed backups stay in place.

---

## 15. No Results After Search Filtering

**Symptoms**: after adding `--min-words` / `--max-words` / `--updated-after` (or the API's `min_words` / `max_words` / `updated_after`), results drop from many to zero even though the book clearly exists.

**Cause**: filtering keeps only rows where the relevant field can be parsed. With a word filter, rows that have no `word_count` or an unparseable value are dropped; with a date filter, rows without `update_date` are dropped. This is deliberate ("rather nothing than wrong rows"), not a failed request.

**Verify and fix**:

1. Remove the filters and confirm the book is searchable at all:

   ```bash
   novel-cli search "title" --site <site>
   ```

2. Add the filters one at a time to find the one that removed the result:

   ```bash
   # no filters first
   novel-cli search "title" --site <site>

   # date filter only
   novel-cli search "title" --site <site> --updated-after 2024-01-01

   # word filter only
   novel-cli search "title" --site <site> --min-words 300000
   ```

3. Check units and boundaries: the word count is a per-book number (`"30万字"` parses as 300000), and both bounds are inclusive; `--updated-after` is an **inclusive** lower bound and must use exactly `YYYY-MM-DD`, otherwise the command exits `2` instead of silently ignoring it.
4. `--group` merging and `--limit` run after filtering, so a smaller final row count is expected.

---

## 16. Still Stuck?

Include the following information when opening an issue:

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.platform()); print(platform.python_version()); print(novel_downloader.__version__)"
tail -n 200 logs/novel_downloader.log
```

Also describe: the target site, the exact command you ran, the relevant part of `settings.toml` (remove credentials, cookies, tokens, and other sensitive values first), and whether the problem is reproducible.