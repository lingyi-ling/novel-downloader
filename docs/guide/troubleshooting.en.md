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
python -c "import importlib.util as u; print('httpx', bool(u.find_spec('httpx'))); print('curl_cffi', bool(u.find_spec('curl_cffi')))"
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

4. Open `http://<computer IP>:8081/docs` in the phone's browser. If the Swagger page loads, the network path works; if not, continue with the firewall checks.

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

   * Apple Music / SoundCloud need `pywidevine`.
   * YouTube needs `ytmusicapi` and an executable Node (`node` or `nodejs`).
   * Some HLS/streaming formats need `ffmpeg` installed on the system.

   Check:

   ```bash
   python -c "import importlib.util as u, shutil; print('pywidevine', bool(u.find_spec('pywidevine'))); print('ytmusicapi', bool(u.find_spec('ytmusicapi'))); print('node', shutil.which('node') or shutil.which('nodejs')); print('ffmpeg', shutil.which('ffmpeg'))"
   ```

4. Platforms that need account credentials (such as Spotify, TIDAL, and Soda Music) also require cookies or tokens in the platform credentials; otherwise search may return nothing.

5. If one platform fails, try another to confirm that the basic feature works:

   ```bash
   novel-cli music search "keyword" --platform netease --limit 5
   ```

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

## 8. Still Stuck?

Include the following information when opening an issue:

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.platform()); print(platform.python_version()); print(novel_downloader.__version__)"
tail -n 200 logs/novel_downloader.log
```

Also describe: the target site, the exact command you ran, the relevant part of `settings.toml` (remove credentials, cookies, tokens, and other sensitive values first), and whether the problem is reproducible.