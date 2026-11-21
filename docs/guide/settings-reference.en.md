## `settings.toml` Configuration Reference

The desktop Settings tab edits the common options below (output folder, proxy, automatic updates, theme and language) and saves them to `settings.toml`:

![Desktop Settings tab](../assets/images/desktop_settings.png)

### Desktop Settings: Search cache

The **Search cache** card on the desktop Settings page shows the search cache of the **current desktop process**. Search results are cached briefly per query, so repeating the same search is served from the cache instead of contacting every site again. The card reads a read-only snapshot: refreshing it never purges expired entries, opens disk payloads, or changes any cache behaviour. It is read once when the Settings page opens and again after a successful clear.

| Card row | Meaning |
| -------- | ------- |
| Memory entries | Number of entries held in memory by this process; includes entries whose TTL has elapsed but that no later write/read has purged yet |
| Disk entries | Number of payload files of this cache format in the cache directory, whether or not they have expired; always `0` when the disk layer is disabled |
| Cache hits | Hits since the process started; both memory hits and entries restored from disk into memory count |
| Expired entries | The subset of memory entries older than the TTL (300 seconds by default); counting them does not purge them, and expired disk files are not included |

A missing value (the cache has not been used yet) or a failed read renders as `0`, with the error shown below the rows; neither affects searches that are already running. The counters are cumulative for the desktop process; clearing the cache does not reset them. Misses and LRU evictions (caused by the memory entry cap, 64 by default) are counted too but not shown separately on this card; use the "Search-cache diagnostics" section of [`novel-cli doctor`](./cli-examples.en.md#doctor-subcommand) for the full field set. The disk layer is off by default and is enabled only when `NOVELDL_SEARCH_CACHE_DIR` is set before the search module is first imported; once enabled, each write leaves at most 256 payload files in the directory, and a snapshot larger than 2 MiB stays in memory only.

**Clearing the search cache**

"Clear search cache" requires two clicks: the first click only changes the label to "Click again to clear" and keeps the button armed for four seconds; a second click within four seconds performs the clear, while a timeout restores the normal label so the button never stays armed. The button is disabled while the clear runs. A successful clear:

* drops every in-memory entry;
* when the disk layer is enabled, deletes this cache format's payload files (`v1-*.json`) from the cache directory (best effort: a file that cannot be deleted is ignored without an error); if `NOVELDL_SEARCH_CACHE_DIR` points at a directory shared with other processes, the disk cache they wrote is removed too, so their later identical searches can only use their own in-memory entries and otherwise re-hit every site;
* leaves other files in the directory untouched, along with anything not of this cache format;
* keeps the cumulative hit/miss/eviction counters.

On success a "Search cache cleared" toast appears and the card is refreshed; on failure the toast shows the error text, the card keeps its previous values, and the action can be retried immediately.

### Desktop Settings: Updates {#desktop-settings-updates}

The **Settings → Updates** card has three switches and a check-now button:

| Control | Preference key | Behaviour |
| --- | --- | --- |
| Check for updates automatically | `auto_check_updates` | Checks after start-up, at most once every 24 hours; when off, no automatic check runs, but **Check now** still works |
| Download updates in the background | `auto_download_updates` | Downloads a new release in the background once it is found |
| Install the downloaded update when quitting | `auto_install_on_exit` | Launches the verified installer when the app exits (Windows only) |
| Check now | — | Forces a check, ignoring the 24-hour throttle and the automatic-check switch |

For "Later" versus "Skip this version", the pre-request `last_update_check` write, and where `skipped_update_version` is stored and how to clear it, see [The desktop update notice](updates.en.md#desktop-update-notice). These runtime values are outside the settings export (see the table below).

### Exporting and importing desktop settings

The **Settings → Backup** card exports the **desktop preferences** as JSON and imports them on another machine. It exports `desktop_prefs.json` (by default under the application data directory's `music/` folder), not `settings.toml`; the file contains only preferences that are safe to move between machines. An import **merges** the file into the current preferences; it is not a factory reset.

**Export file contract**

```json
{
  "app": "novel-downloader",
  "kind": "desktop-preferences",
  "format_version": 1,
  "app_version": "1.37.0",
  "exported_at": "2026-11-15T00:00:00Z",
  "settings": {
    "theme": "dark",
    "auto_check_updates": true,
    "auto_download_updates": true,
    "auto_install_on_exit": true,
    "search_filters": {
      "min_words": 10,
      "max_words": 500,
      "updated_after": "2024-01-02"
    },
    "search_sort": {"sort": "quality", "order": "desc"}
  }
}
```

| Field | Description |
| --- | --- |
| `app` | Fixed to `novel-downloader`; an import is rejected when it does not match |
| `kind` | Fixed to `desktop-preferences`, so an ordinary JSON file cannot be imported by mistake |
| `format_version` | Currently `1`. Only the integer `1` is accepted; `true`, `2`, `"1"` and the like are rejected. There is no backward compatibility |
| `app_version` | Application version at export time; informational only, and an import does not compare it with the running version |
| `exported_at` | UTC timestamp (second precision, `Z` suffix); informational only |
| `settings` | Preference payload; keys are listed in the whitelist below |

**Whitelist (these keys are the only ones exported)**

| `settings` key | Type | Meaning |
| --- | --- | --- |
| `theme` | `"light"` / `"dark"` | Appearance theme |
| `auto_check_updates` | bool | Check for updates automatically |
| `auto_download_updates` | bool | Download updates in the background |
| `auto_install_on_exit` | bool | Install a downloaded update when quitting |
| `search_filters` | object | Search filters; may contain `min_words` / `max_words` / `updated_after` (`YYYY-MM-DD`), and is `{}` when none are set |
| `search_sort` | object | Search sort, always with `sort` (`quality` / `priority` / `words` / `update`) and `order` (`asc` / `desc`) |

**Deliberately not exported**:

| Not exported | Reason |
| --- | --- |
| Proxy address, proxy username / password | Credentials must not travel in a file, and an address may be tied to the local network |
| Music account tokens (TIDAL / Apple Music / SoundCloud, etc.) | Account credentials; device-private data |
| Output folder, interface language | Stored in `settings.toml` / a separate state file and chosen per device and person; not part of the desktop preferences file |
| Throttle state such as `last_update_check`, and `skipped_update_version` | Local runtime state; migrating it is meaningless, and a skipped version would only give the new machine an equally meaningless "skipped" marker |
| Music download options such as `embed_lyric` | Shared with the CLI through the music settings file; exporting them would create a second source of truth |

The exported JSON is therefore safe to put in a sync folder or a chat message; the trade-off is that the proxy, accounts, output folder and language must be set again on the new machine (see "Limits of moving to another machine").

**Export behaviour**

* A blank path writes next to the desktop preferences file with a timestamped name such as `desktop-settings-20261115-143000.json`; a path without an extension gets `.json` appended.
* The write uses a temporary file plus an atomic replace, so a half-written file is never left behind.
* Exporting onto an existing directory is refused; other write failures are reported only in the card's status line and the toast.
* When the preferences file is missing or unreadable, the export contains the defaults (`theme=light`, all three update switches on, empty filters) instead of failing.

**Import validation**

An import validates everything **before writing anything**. Any failure leaves the existing preferences untouched and creates no `.bak`:

1. The path must not be blank; the file must exist and be a regular file (a directory is rejected).
2. The file must not exceed 1 MiB (`1,048,576` bytes); anything larger is rejected, guarding against picking the wrong document or exhausting memory.
3. It is read as UTF-8 with an optional BOM (`utf-8-sig`); any other encoding or invalid byte sequence is reported as "not valid JSON".
4. The top level must be a JSON object; `app` and `kind` must match exactly; `format_version` must be exactly the integer `1` (the boolean `true` is rejected too).
5. Only the six contract fields are allowed at the top level; any unknown field is rejected. `app_version` and `exported_at` may be absent, and their contents are not validated.
6. `settings` must contain all six whitelisted keys; a missing key is rejected. A key outside the whitelist is rejected as well, so an import cannot smuggle a field such as `proxy_pass` into the preferences file.
7. Types are strict: `theme` is only `light` / `dark`; the three update switches must be real booleans (the string `"false"` does not count); `min_words` / `max_words` accept integers, integral floats or digit-only strings in the range `0`–`1,000,000,000`, and the minimum must not exceed the maximum; `updated_after` must be `YYYY-MM-DD`; unknown keys inside `search_filters` and `search_sort` are rejected too, `search_sort` must contain both `sort` and `order`, and both values must be allowed.
8. The error is shown in the card's status line and the toast, and the current settings stay as they were.

**An import merges; it does not reset**

Other keys in `desktop_prefs.json` (including historical keys outside the whitelist and the proxy password) are not deleted; an import only overwrites the six preference keys present in `settings`. Importing a "default values" file therefore does not wipe the proxy password or account information.

Before writing, if the preferences file exists, it is copied to `<name>.bak` in the same directory (for example `desktop_prefs.json.bak`); that copy is atomic too. The preferences file is then replaced through a temporary file plus an atomic replace. If the final write fails, the old file stays in place and the `.bak` remains, so it can be used to roll back.

**Limits of moving to another machine**

* Only the six desktop preferences above are migrated. The output folder, proxy and accounts, music tokens and interface language are not in the file and must be set manually on the new machine.
* The import does not check `app_version`, but it rejects a file whose `format_version` is not `1`. If the format is upgraded in a future release, an old export must be re-exported with the new version rather than having its version number edited by hand.
* There is only one `.bak`: every import that reaches the write stage overwrites it. It is a rollback for the most recent import, not a history archive.
* After a successful import the UI immediately refreshes the theme and the Settings cards and reloads the task / follow lists, but the process is **not restarted**; the interface language was never part of the import.

### Keyboard and accessibility (desktop) {#desktop-keyboard-accessibility}

This section describes what `novel-desktop` actually implements today. Every shortcut is **window-scoped**, not a system-wide hotkey: it responds only while the desktop window's web content has focus and does nothing while the window is in the background. While focus is in an input, select, textarea or editable region, the custom combinations are left to text entry and the IME, and `Ctrl+D` does not fire either.

#### Cross-page shortcuts

The sidebar is a vertical `tablist` with six tabs: Search, Download, History, Music, Follow and Settings.

| Shortcut | Action | Applies when |
| --- | --- | --- |
| `Ctrl+1` … `Ctrl+6` | Switch to the corresponding tab (in the order above) | Focus is not in an editable control; switching to Search or Music also moves focus into that page's keyword field |
| `Ctrl+F` | Switch to Search and put focus in the keyword field; this is an in-app jump, not the browser's find bar | Same as above |
| `Ctrl+D` | Download the selected tracks on the Music tab | Only on the Music tab, with at least one track selected, the download button enabled, and focus outside editable controls |

The navigation has no additional letter shortcuts. Once focus is on a nav item, `↑`/`↓`/`←`/`→` switch to the adjacent tab and activate it in step, `Home`/`End` jump to the first/last item, and `Tab` moves through the individual tabs (each is an ordinary button); `Enter`/`Space` activates the current one.

#### Search page

| Action | Keyboard behaviour |
| --- | --- |
| Run a search | Press `Enter` in the keyword field; `Enter` in the Min words, Max words or Updated after fields of the Filters panel also runs the search. You can also `Tab` to the Search button and press `Enter`/`Space` |
| Open a result | The result title itself is a button: `Tab` to it and press `Enter`/`Space` to open the details. The row's Download control is a separate button and needs another `Tab`; pressing `Enter` on blank card space does not open the details |
| Sorting | Sort by and Sort direction are native select dropdowns, so the usual dropdown keyboard behaviour applies after focus (`↑`/`↓` changes the option). There is no custom sort chip group and no arrow-key movement between chips |
| Filters panel | The Filters button exposes `aria-expanded` and `aria-controls`; `Esc` only closes the panel and returns focus to the Filters button, leaving the keyword and results untouched |
| Categories panel | After it opens and finishes loading, focus moves to the first category chip; `Esc` closes the panel and returns focus to the Categories button. Category chips and the Male/Female switches are ordinary buttons: move with `Tab` and activate with `Enter`/`Space`; arrow-key movement between chips is not implemented |
| Search field `Esc` | Two-step behaviour: cancel the running search first if one is in progress; otherwise clear the input. It neither cancels the displayed results nor blurs the field; use `Tab`/`Shift+Tab` to leave it |
| Details (narrow window) | At a width of 1180 px or less the details are a bottom modal drawer: opening it moves focus to the Close button, `Tab`/`Shift+Tab` stay inside the drawer, and `Esc` closes it and returns focus to the result title that opened it (or the search field when that target is gone) |
| Details (wide window) | Above 1180 px the details are a permanent third column, not a dialog; selecting a result does not move focus and `Esc` does not close the column |
| Add-book dialog | `Tab` loops inside the dialog rather than reaching the page behind it; `Esc` closes it and returns focus to the button that opened it. `Enter` in a dialog input does not submit (the dialog has no form-submit semantics); `Tab` to OK and press `Enter`/`Space` |
| Show more | Show more is an ordinary button; once the final batch is loaded the button disappears. Keyboard focus does not automatically land on a new result title, so `Tab` back into the list |

#### Settings page

| Action | Keyboard behaviour |
| --- | --- |
| Moving through the form | Inputs, selects, switches and buttons in the Settings cards follow DOM order in the `Tab` sequence; inputs get their accessible names from visually hidden `<label>` elements, and the switches are native checkboxes |
| Output folder / proxy / backup paths | Type the path directly. `Enter` neither saves the settings nor triggers export or import; `Tab` to the corresponding button and press `Enter`/`Space` |
| Export settings | With focus on Export settings, `Enter`/`Space` runs it. A blank path writes to the default location; on success the resolved path is filled back into the input |
| Import settings | Type the file path, then `Tab` to Import settings and press `Enter`/`Space`. With a blank path the status line and toast report "Enter a file path" and focus returns to the path input |
| Clear search cache (two-step confirmation) | The first `Enter`/`Space` only changes the button label to "Click again to clear" and keeps it armed for four seconds; a second activation within four seconds performs the clear, and a timeout restores the normal label. The status line is an `aria-live` region and receives the armed state when it has no other message; the button is disabled while the clear runs and the stats are refreshed on success |
| Clear saved filters | A single activation clears them; there is no second confirmation |
| Save settings | Submit with the Save changes button; `Enter` inside a field does not submit. Save failures appear as a summary in the `role="alert"` region at the top of the form |

#### Screen-reader semantics

* The first focusable element is a "Skip to content" link targeting the main content region (`tabindex="-1"`); focusable controls use a `:focus-visible` outline, and `prefers-reduced-motion: reduce` collapses animations and transitions to near zero.
* Navigation uses a `tablist` / `tab` / `tabpanel` structure; `aria-selected` follows the active tab, and each panel is associated with its tab through `aria-labelledby`.
* Primary inputs and selects have accessible names: visual labels are hidden with `.sr-only` but stay in the accessibility tree; at startup an input or select without a name mirrors its placeholder into `aria-label`. Icon-only buttons get a name through `aria-label` or `aria-labelledby`.
* Dynamic messages use `role="status"` (which implies `aria-live="polite"`): search progress, the applied-filter strip, category hints, Settings status lines, search-cache status, import/export status, follow status, the update notice and the toast container are announced without stealing focus. Settings save failures and filter validation errors use `role="alert"`.
* The result container is marked `aria-busy="true"` while a search runs and cleared afterwards; the music result list uses the same busy state.
* The selected result card carries `aria-current="true"`; the title button's accessible name contains the title and, when match telemetry exists, the match tier and score explanation.
* The narrow-window details drawer carries `role="dialog"` and `aria-modal="true"` and is named by the details title; the same node has no dialog role in the wide layout. The add-book dialog also uses `dialog` / `aria-modal` and focuses its first input when opened.
* The toast close button is removed from the `Tab` sequence (`tabindex="-1"`). An action toast (for example "Open Settings") is itself a `role="button"`, does not auto-dismiss while focused, and runs its action on `Enter`/`Space`.

#### Known limitations

* There are no system-wide global hotkeys and no window-menu accelerators; shortcuts do nothing while the window is unfocused.
* Cross-page combinations (`Ctrl+F`, `Ctrl+1`–`Ctrl+6`, `Ctrl+D`) are ignored while focus is in an input, select, textarea or editable region; move focus to a non-editable control first, for example `Tab` to a button or nav item. The field- and panel-specific `Esc` behaviour is described in the preceding section.
* `Ctrl+D` only serves "download selected" on the Music tab; it does nothing on the other tabs. The sidebar text "Ctrl+D download selected" should be read with that restriction.
* Sorting uses native `<select>` controls, not a custom sort chip group; category chips likewise have no arrow-key roving tabindex and are reached one by one with `Tab`.
* `Esc` in the search field does not leave the field; it cancels a running search first and clears the text second. Use `Tab` to move out.
* The wide-layout details column is not modal and `Esc` does not collapse it; only the narrow-window (≤ 1180 px) bottom drawer supports `Esc` closing and focus return.
* The path inputs on the Settings page do not trigger export, import or save on `Enter`; focus must be moved to the corresponding button.
* When Show more is activated from the keyboard and loads the final batch, the button is removed and focus may fall back to the start of the document instead of a new result title; `Tab` back into the list.
* Toasts dismiss themselves after about four seconds by default (error messages last longer), and the close button on an ordinary toast is not in the `Tab` sequence; to re-read a message, check the corresponding status line or trigger the action again.
* This section describes the keyboard and ARIA semantics in the code; it is not a claim that the app has passed a formal accessibility audit across every screen reader, webview engine, IME and high-contrast theme combination.

### general Settings

Global runtime settings for downloads / concurrency / rate limits / directories and storage.

#### Main options

| Parameter            | Type      | Default            | Description |
| -------------------- | --------- | ------------------ | ----------- |
| `raw_data_dir`       | `str`     | `"./raw_data"`     | Directory for book data |
| `output_dir`         | `str`     | `"./downloads"`    | Directory for final exported files |
| `cache_dir`          | `str`     | `<user data dir>/cache` | Local cache directory (fonts / images, etc.); relative paths resolve against the working directory |
| `request_interval`   | `float`   | 0.5                | Interval (seconds) between chapter requests for **the same book** |
| `workers`            | `int`     | 4                  | Number of download task coroutines |
| `max_connections`    | `int`     | 10                 | Maximum number of concurrent connections |
| `max_rps`            | `float`   | 1000.0             | Global RPS limit shared by page and media (image/font) requests |
| `retry_times`        | `int`     | 3                  | Request retries (timeouts/connection errors and 408/425/429/5xx only; deterministic 4xx are not retried, and `Retry-After` is honoured) |
| `backoff_factor`     | `float`   | 2.0                | Retry backoff factor (wait time grows by this multiple per retry, e.g. `2s`, `4s`, `8s`) |
| `timeout`            | `float`   | 10.0               | Per-request timeout (seconds) |
| `storage_batch_size` | `int`     | 1                  | Number of chapters committed per SQLite batch (improves write performance) |
| `cache_book_info`    | `bool`    | `true`             | Whether to enable the book_info cache |
| `cache_chapter`      | `bool`    | `true`             | Whether to enable the chapter cache |
| `fetch_inaccessible` | `bool`    | `false`            | Whether to try fetching chapters that are not subscribed |
| `login_required`     | `bool`    | `false`            | Global default for requiring a login; a site-level `login_required` overrides it |
| `strict_config`      | `bool`    | `false`            | When `true`, unknown processor options raise an error instead of only warning |
| `backend`            | `str`     | `"aiohttp"`        | Global HTTP backend; one of `"aiohttp"`, `"httpx"`, `"curl_cffi"` |
| `http2`              | `bool`    | `true`             | Only effective for `httpx`; enables HTTP/2 |
| `impersonate`        | `str/None`| `None`             | Only effective for `curl_cffi`; enables browser fingerprint impersonation |
| `browser_assisted_sites` | `list[str]` | `["fanqienovel"]` | Site keys whose search needs a real browser; set to `[]` to disable |
| `download_retry_times` | `int`   | 2                  | Extra retries for the whole download task (retryable errors only); request-layer retries are handled by `retry_times` |
| `proxy`              | `str/None`| `None`             | HTTP/HTTPS proxy URL, e.g. `"http://127.0.0.1:7890"` |
| `proxy_user`         | `str/None`| `None`             | Proxy username |
| `proxy_pass`         | `str/None`| `None`             | Proxy password |
| `trust_env`          | `bool`    | `false`            | Whether to read system proxy and other environment variables |
| `verify_ssl`         | `bool`    | `true`             | Whether to verify TLS certificates (disable only when diagnosing proxy issues) |
| `locale_style`       | `str`     | `"simplified"`     | Site simplified/traditional mirror selection: `"simplified"` / `"traditional"` (only applies to sites offering both mirrors) |
| `update_check`       | `str`     | `"auto"`           | Update-check policy: `"auto"` (silent check after a command, at most once a day) / `"manual"` (only when running `novel-cli update`) / `"off"` (never) |
| `user_agent`         | `str/None` | `None`            | Custom request User-Agent (overrides the default) |
| `headers`            | `dict/None` | `None`           | Extra custom request headers (key-value pairs) |

**Cache directory**

When `cache_dir` is not set, caches (including site cookies) live in the `cache/` subdirectory of the application data directory: `%LOCALAPPDATA%\novel_downloader\cache` on Windows and `~/.local/share/novel_downloader/cache` on Linux. The application data directory can be overridden with the `NOVEL_HOME` environment variable. Relative paths set explicitly still resolve against the current working directory; set `cache_dir = "./novel_cache"` to restore the legacy behavior.

**Site pressure and 503**

Some sites are sensitive to high request rates (for example, >= 5 RPS) and may return `503 Service Temporarily Unavailable`.

Consider **lowering** `max_rps` or **increasing** `request_interval`. The tool supports resumable crawling, so already-downloaded data is not fetched again.

**HTTP backends**

The program supports pluggable HTTP backends, switchable with the `backend` parameter under `[general]`:

| Backend name | Description | Installation |
| ------------ | ----------- | ------------ |
| `aiohttp`    | Default backend, an asynchronous HTTP client based on `aiohttp` | - |
| `httpx`      | Modern asynchronous HTTP client supporting HTTP/1.1 and HTTP/2 | `pip install httpx[http2]` |
| `curl_cffi`  | `libcurl`-based implementation supporting browser impersonation (`impersonate`) | `pip install curl_cffi` |

> **Extra parameters**
>
> * `http2`: only effective for `httpx`; enables HTTP/2 (default `true`)
> * `impersonate`: only effective for `curl_cffi`; enables browser fingerprint impersonation, e.g. `"chrome136"`, `"chrome"`
>
> See the respective project documentation for more information:
>
> * [`httpx`](https://github.com/encode/httpx)
> * [`curl_cffi`](https://github.com/lexiforest/curl_cffi)

**Browser-assisted search**

A few sites sign their search requests inside their own JavaScript, so plain HTTP requests return nothing. List those sites in `browser_assisted_sites`; an explicit search for such a site then reuses the login session to open a hidden browser, lets the site's own script issue the request, and captures the result. Aggregate searches continue to skip them.

```toml
[general]
# The default is ["fanqienovel"]
browser_assisted_sites = ["fanqienovel"]
```

* A comma-separated string is also accepted: `browser_assisted_sites = "fanqienovel,example"`.
* When the key is omitted, the default `["fanqienovel"]` applies.
* Setting it explicitly to `[]` disables browser-assisted search entirely; those sites fall back to plain HTTP search (usually with no results).
* Run `novel-cli login <site>` first to establish the login session, and install the optional browser component.

**Compatibility and Cloudflare behavior**

Because HTTP clients differ in protocol implementation and TLS fingerprints, some sites may trigger **Cloudflare anti-bot or access verification** when using the default `aiohttp` backend, causing connections to be blocked or `403`/`5xx` responses.

If this happens, try switching to one of these backends:

* **`httpx` (with `http2 = true`)**: HTTP/2 provides network characteristics closer to modern browsers and improves compatibility with some Cloudflare-protected sites.
* **`curl_cffi` (with `impersonate`)**: Emulating a browser's TLS fingerprint and request headers can significantly improve access success on sites blocked by Cloudflare or other WAFs.

Note that backend behavior can differ by site. For example:

* Some sites only work with `aiohttp` or `httpx` in HTTP/1.1 mode, while enabling `http2 = true` triggers Cloudflare or causes connection failures.
* Other sites are the opposite: they only connect successfully with HTTP/2 or `curl_cffi` browser impersonation.

Therefore, when encountering access problems (403, TLS handshake failures, Cloudflare verification loops, etc.), adjust the backend and related parameters according to the specific site.

#### parser subsection

These settings handle **obfuscated font decoding** and **image-chapter OCR**, and control **watermark removal** for image chapters.

| Parameter          | Type           | Default | Description |
| ------------------ | -------------- | ------- | ----------- |
| `enable_ocr`       | `bool`         | false   | Whether to enable local OCR for obfuscated fonts or image-chapter text |
| `batch_size`       | `int`          | 32      | Batch size for OCR model inference |
| `remove_watermark` | `bool`         | false   | Whether to attempt watermark removal on image chapters (supported by some sites) |
| `model_name`       | `str/None`     | None    | OCR model name; when `None`, PaddleOCR selects `PP-OCRv5_server_rec`. The bundled `settings.toml` sets `PP-OCRv5_mobile_rec` explicitly (much faster on CPU) |
| `model_dir`        | `str/None`     | None    | OCR model storage path |
| `input_shape`      | `tuple/None`   | None    | OCR model input image size in (C, H, W) format |
| `device`           | `str/None`     | None    | Inference device, e.g. `"cpu"`, `"gpu"`, `"npu"`, `"gpu:0"`, `"gpu:0,1"` |
| `precision`         | `str`          | `"fp32"` | Inference precision |
| `cpu_threads`      | `int`          | 10      | Number of threads used for CPU inference |
| `enable_hpi`       | `bool`         | false   | Whether to enable high-performance inference |
| `use_truncation`   | `bool`         | true    | Whether to trim duplicated/truncated chapter content (e.g. qidian repeated paragraphs) |
| `cut_mode`         | `str`          | `"none"`| Chapter content cutting mode |

Behavior notes:

**Obfuscated-font chapters**

* If parsing is disabled or fails, the program automatically embeds the corresponding font file when exporting EPUB/HTML so that text displays correctly.

Dependencies:

If `enable_ocr` is enabled, install the extra image-processing dependencies:

```bash
pip install novel-downloader[image-utils]
```

OCR depends on `PaddleOCR` and its models. See the installation guide:

* [Installation](./installation.md)

For `PaddleOCR` configuration, refer to the official documentation:

* [PaddleOCR documentation](https://www.paddleocr.ai/main/version3.x/module_usage/text_recognition.html#_4)

#### output subsection

Controls export formats, file naming, and EPUB details.

| Parameter                     | Type        | Default              | Description |
| ----------------------------- | ----------- | -------------------- | ----------- |
| `formats`                     | `list[str]` | `[]`                 | Output formats; when unset the CLI exports `epub` |
| `render_missing_chapter`      | `bool`      | `true`               | Whether to insert placeholder content for missing chapters during export |
| `append_timestamp`            | `bool`      | `true`               | Whether to append a timestamp to output filenames |
| `filename_template`           | `str`       | `"{title}_{author}"` | Filename template |
| `include_picture`             | `bool`      | `true`               | Whether to embed images from chapters (may increase file size) |
| `split_mode`                  | `str`       | `"book"`             | Split mode for exports: `book` (one file per book) / `volume` (per volume) / `chapter` (per chapter) |

#### Debug subsection

| Parameter             | Type   | Default   | Description |
| --------------------- | ------ | --------- | ----------- |
| `debug.save_html`     | `bool` | `false`   | Whether to save fetched raw HTML to disk |
| `debug.log_level`     | `str`  | `"INFO"`  | Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `debug.log_dir`       | `str`  | `"./logs"`| Directory for runtime logs |

#### Example configuration

The values below match the defaults generated by `novel-cli config init`; adjust them as needed:

```toml
[general]
retry_times = 3
backoff_factor = 2.0
timeout = 10.0
max_connections = 10
max_rps = 1000.0
request_interval = 0.5
raw_data_dir = "./raw_data"
output_dir = "./downloads"
# cache_dir defaults to cache/ inside the application data directory;
# to customize it (relative paths resolve against the working directory):
# cache_dir = "./novel_cache"
workers = 4
cache_book_info = true
cache_chapter = true
storage_batch_size = 1

[general.output]
formats = [
    "txt",
    "epub",
]
include_picture = true

[general.parser]
enable_ocr = false
batch_size = 32
remove_watermark = false

model_name = "PP-OCRv5_mobile_rec"
```

---

### sites Settings

Site-level settings (such as `qidian`, `b520`, ...). **Site-level settings override global behavior**; each site is configured under `[sites.<site>]`.

#### Common keys

| Parameter         | Type                       | Default | Description |
| ----------------- | -------------------------- | ------- | ----------- |
| `book_ids`        | `list<str>` / `list<dict>` | -       | List of novel IDs |
| `login_required`  | `bool`                     | false   | Whether a login is required for access |

#### `book_ids` field description

`book_ids` supports the following two formats:

1) Simple list

```toml
[sites.<site>]
book_ids = [
  "1010868264",
  "1020304050"
]
```

2) Structured (supports ranges and ignore lists)

```toml
[sites.<site>]
login_required = true

[[sites.<site>.book_ids]]
book_id = "1030412702"
start_id = "833888839"
end_id = "921312343"
ignore_ids = ["1234563", "43212314"]

[[sites.<site>.book_ids]]
book_id = "1111111111"   # other fields may be omitted
```

**Structured field description**

| Field        | Type        | Required | Description |
| ------------ | ----------- | -------- | ----------- |
| `book_id`    | `str`       | Yes      | Unique identifier of the novel |
| `start_id`   | `str`       | No       | First chapter ID (inclusive) |
| `end_id`     | `str`       | No       | Last chapter ID (inclusive) |
| `ignore_ids` | `list[str]` | No       | Chapter IDs to skip |

#### Example configuration

**Simple format**

```toml
[sites.qidian]
book_ids = [
  "1010868264",
  "1012584111"
]
login_required = true

# Site-level parser settings override the same keys in [general.parser]
[sites.qidian.parser]
use_truncation = true
```

> `use_truncation` is a parser option and **must** be placed under `[sites.<site>.parser]` (or `[general.parser]`).
> A top-level key in `[sites.<site>]` is not read; validation reports a warning and suggests moving it to `sites.<site>.parser.use_truncation`.
> Other parser options (`enable_ocr`, `remove_watermark`, `cut_mode`, ...) can also be overridden in the site-level `parser` table.

**Structured format**

```toml
[sites.qidian]
login_required = true

[[sites.qidian.book_ids]]
book_id = "1010868264"
start_id = "434742822"
end_id = "528536599"
ignore_ids = ["507161874", "516065132"]

[[sites.qidian.book_ids]]
book_id = "1012584111"
```

**Site settings override global behavior**

Site configuration can override global configuration.

In the example below, the global `request_interval = 0.5` is overridden by `request_interval = 2.0` in `sites.qidian`:

```toml
[general]
timeout = 10.0
request_interval = 0.5

[sites.qidian]
login_required = true
request_interval = 2.0  # overrides the global 0.5
```

---

### plugins Settings

Plugin-system settings used to enable or override built-in implementations.

#### Main options

| Parameter              | Type   | Default            | Description |
| ---------------------- | ------ | ------------------ | ----------- |
| `enable_local_plugins` | `bool` | `false`            | Whether to enable the local plugin directory (scan and load local plugins) |
| `override_builtins`    | `bool` | `false`            | Whether local plugins may **override** built-in implementations with the same name (same site/processor/exporter) |
| `local_plugins_path`   | `str`  | `"./novel_plugins"`| Local plugin directory path; only effective when `enable_local_plugins = true` |

> For more about writing plugins (custom sites, processors, exporters), see: [Plugin System Documentation](../reference/plugins.md)

#### Example configuration

```toml
[plugins]
# Whether to enable the local plugin directory
enable_local_plugins = false
# Whether local plugins may override built-in implementations
override_builtins = false
# Local plugin path (optional)
local_plugins_path = "./novel_plugins"
```

---

### Config validation and migration

The `novel_downloader.infra.config` package exports two helper functions for checking or migrating a loaded `settings.toml`:

| Function         | Signature                            | Description |
| ---------------- | ------------------------------------ | ----------- |
| `validate_config` | `(path_or_dict) -> list[Problem]`    | Validates a configuration mapping or a `.toml`/`.json` config file path and returns a list of problems (an empty list means the configuration is valid); parse errors and missing files are returned as `error` problems instead of raising, and no network access is performed |
| `migrate_config` | `(data) -> tuple[dict, list[str]]`   | Migrates old keys to the current layout and returns `(migrated config, list of change notes)` |

Each `Problem` object has `level` (`error` / `warning` / `info`), `path` (dotted location inside the configuration), `message`, and `fix_hint` fields.

`migrate_config` currently performs these migrations:

* `sites.<site>.cookie` -> `sites.<site>.cookies`
* `sites.<site>.use_truncation` -> `sites.<site>.parser.use_truncation`
* Fills `download_retry_times` with the default `2` when a `[general]` table exists but the key is unset

The function is idempotent: calling it twice on the same configuration yields the same data and an empty change list on the second call; the input mapping is not mutated.

```python
from novel_downloader.infra.config import migrate_config, validate_config

problems = validate_config("settings.toml")
for p in problems:
    print(p)  # [level] path: message Hint: fix_hint

cfg, notes = migrate_config(cfg)
for note in notes:
    print(note)
```