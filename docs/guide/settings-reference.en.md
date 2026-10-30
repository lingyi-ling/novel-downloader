## `settings.toml` Configuration Reference

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
| `max_rps`            | `float`   | 1000.0             | Global RPS limit (requests per second) |
| `retry_times`        | `int`     | 3                  | Number of retries after a failed request |
| `backoff_factor`     | `float`   | 2.0                | Retry backoff factor (wait time grows by this multiple per retry, e.g. `2s`, `4s`, `8s`) |
| `timeout`            | `float`   | 10.0               | Per-request timeout (seconds) |
| `storage_batch_size` | `int`     | 1                  | Number of chapters committed per SQLite batch (improves write performance) |
| `cache_book_info`    | `bool`    | `true`             | Whether to enable the book_info cache |
| `cache_chapter`      | `bool`    | `true`             | Whether to enable the chapter cache |
| `fetch_inaccessible` | `bool`    | `false`            | Whether to try fetching chapters that are not subscribed |
| `backend`            | `str`     | `"aiohttp"`        | Global HTTP backend; one of `"aiohttp"`, `"httpx"`, `"curl_cffi"` |
| `http2`              | `bool`    | `true`             | Only effective for `httpx`; enables HTTP/2 |
| `impersonate`        | `str/None`| `None`             | Only effective for `curl_cffi`; enables browser fingerprint impersonation |
| `browser_assisted_sites` | `list[str]` | `["fanqienovel"]` | Site keys whose search needs a real browser; set to `[]` to disable |
| `download_retry_times` | `int`   | 2                  | Extra retries for download tasks after transient network errors |
| `proxy`              | `str/None`| `None`             | HTTP/HTTPS proxy URL, e.g. `"http://127.0.0.1:7890"` |
| `proxy_user`         | `str/None`| `None`             | Proxy username |
| `proxy_pass`         | `str/None`| `None`             | Proxy password |
| `trust_env`          | `bool`    | `false`            | Whether to read system proxy and other environment variables |
| `verify_ssl`         | `bool`    | `true`             | Whether to verify TLS certificates (disable only when diagnosing proxy issues) |

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
| `model_name`       | `str/None`     | None    | OCR model name; when `None`, `PP-OCRv5_server_rec` is used |
| `model_dir`        | `str/None`     | None    | OCR model storage path |
| `input_shape`      | `tuple/None`   | None    | OCR model input image size in (C, H, W) format |
| `device`           | `str/None`     | None    | Inference device, e.g. `"cpu"`, `"gpu"`, `"npu"`, `"gpu:0"`, `"gpu:0,1"` |
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
| `formats`                     | `list[str]` | `[]`                 | Output formats |
| `render_missing_chapter`      | `bool`      | `true`               | Whether to insert placeholder content for missing chapters during export |
| `append_timestamp`            | `bool`      | `true`               | Whether to append a timestamp to output filenames |
| `filename_template`           | `str`       | `"{title}_{author}"` | Filename template |
| `include_picture`             | `bool`      | `true`               | Whether to embed images from chapters (may increase file size) |

#### Debug subsection

| Parameter             | Type   | Default   | Description |
| --------------------- | ------ | --------- | ----------- |
| `debug.save_html`     | `bool` | `false`   | Whether to save fetched raw HTML to disk |
| `debug.log_level`     | `str`  | `"INFO"`  | Log level: DEBUG, INFO, WARNING, ERROR |
| `debug.log_dir`       | `str`  | `"./logs"`| Directory for runtime logs |

#### Example configuration

```toml
[general]
retry_times = 3
backoff_factor = 2.0
timeout = 10.0
max_connections = 10
max_rps = 1.0
request_interval = 0.5
raw_data_dir = "./raw_data"
output_dir = "./downloads"
# cache_dir defaults to cache/ inside the application data directory;
# to customize it (relative paths resolve against the working directory):
# cache_dir = "./novel_cache"
workers = 4
cache_book_info = true
cache_chapter = true
storage_batch_size = 4

[general.output]
formats = [
    "txt",
    "epub",
    "html",
]
include_picture = true

[general.parser]
enable_ocr = false
batch_size = 32
remove_watermark = true

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