# Architecture

This document describes the code layers, task lifecycle, extension points, and data-directory layout of NovelDownloader. All paths and class names refer to the real implementation in the repository (`src/novel_downloader/`).

---

## 1. Design Goals

* **Isolate site differences**: fetching, parsing, and searching for each site live in a dedicated plugin package, so the core flow stays site-agnostic.
* **One service layer, multiple entry points**: the CLI, local REST API, and desktop client share `plugins` + `apps/core`; the Android client connects only through the REST API.
* **Rule-driven extension**: a book source can be described in JSON without writing Python (`plugins/rule/`).
* **Recoverable**: metadata and chapters are persisted separately, so an interrupted download can resume from existing data.
* **Replaceable infrastructure**: HTTP backends, storage, exporters, and processors can be replaced without changing the core flow.

---

## 2. Layered Structure

```
┌───────────────────────── Entry layer ─────────────────────────┐
│  novel-cli            novel-api          novel-desktop          │
│  apps/cli             apps/api           apps/desktop           │
│                                                               │
│  Android app (android/, Kotlin + Compose, REST API only)        │
└──────────────────────────────┬────────────────────────────────┘
                               │ calls
┌──────────────────────────────▼────────────────────────────────┐
│ Application / orchestration layer                              │
│  apps/core     TaskManager, DownloadTask, Status, UI adapters, watchlist │
│  apps/common   exported-file history enumeration               │
│  apps/music    music platform registry and downloader          │
│  apps/login    interactive browser login                       │
└──────────────────────────────┬────────────────────────────────┘
                               │ calls
┌──────────────────────────────▼────────────────────────────────┐
│ Domain / plugin layer                                          │
│  plugins/registry.py      PluginRegistry / registrar           │
│  plugins/protocols/       Client / Fetcher / Parser / Processor│
│  plugins/base/            AbstractClient, BaseFetcher, ...     │
│  plugins/mixins/          Download / Process / Export / Cleanup│
│  plugins/sites/<site>/    fetcher.py, parser.py, searcher.py   │
│  plugins/rule/            JSON rule-source engine              │
│  plugins/processors/      cleaner, zh_convert, translator, ... │
│  plugins/search.py        aggregate search, ranking, caching   │
└──────────────────────────────┬────────────────────────────────┘
                               │ depends on
┌──────────────────────────────▼────────────────────────────────┐
│ Infrastructure / library layer                                 │
│  infra/    config, persistence, sessions, cookies, paths, i18n│
│  libs/     crypto, epub_builder, html_builder, filesystem, ... │
│  schemas/  BookConfig, ClientConfig, BookInfoDict, ChapterDict│
└───────────────────────────────────────────────────────────────┘
```

Dependencies flow in one direction: `apps/* → plugins/* → infra/* / libs/*`. `schemas/*` is a shared data-model package that any layer may import. `infra` and `libs` never depend back on `plugins` or `apps`.

### Entry layer

| Entry point | Code location | Launch | Description |
| --- | --- | --- | --- |
| CLI | `apps/cli/main.py` | `novel-cli` | `argparse` subcommands: `download` / `search` / `follow` / `export` / `login` / `config` / `clean` / `storage` / `doctor` / `sources` / `music` |
| REST API | `apps/api/main.py` | `novel-api` | FastAPI + uvicorn, default `127.0.0.1:8081`, route prefix `/api/v1` |
| Desktop client | `apps/desktop/main.py` | `novel-desktop` | pywebview window hosting a single-file HTML front end (`web_assets.py`) driven by `webview_app.ApiBridge`; `AsyncEngine` in `apps/desktop/engine.py` owns a background asyncio event loop |
| Android | `android/app/src/main/java/com/lingyi/noveldl/` | Gradle build | Calls the local API over HTTP; no direct Python dependency |

The command entry points are declared under `[project.scripts]` in `pyproject.toml`.

---

## 3. Key Modules

| Module | Real path | Responsibility | Key symbols |
| --- | --- | --- | --- |
| Data models | `schemas/` | Cross-layer data classes and types | `BookConfig`, `ClientConfig`, `FetcherConfig`, `ParserConfig`, `ExporterConfig`, `ProcessorConfig`, `BookInfoDict`, `ChapterDict` |
| Config loading | `infra/config/file_io.py` | Locate and parse `settings.toml` / `settings.json` by priority | `load_config()`, `copy_default_config()` |
| Config adaptation | `infra/config/adapter.py` | Merge global and site settings into stage configs | `ConfigAdapter.get_client_config()`, `get_processor_configs()` |
| Chapter storage | `infra/persistence/chapter_storage.py` | SQLite chapter read/write and resume flags | `ChapterStorage`, table `chapters` |
| User state | `infra/persistence/state.py` | User preferences such as language | `StateManager`, `state_mgr` |
| HTTP sessions | `infra/sessions/` | Pluggable backends | `BaseSession`, `create_session()`, `_aiohttp.py` / `_httpx.py` / `_curl_cffi.py` |
| Path constants | `infra/paths.py` | Application home and bundled resource paths | `app_home()`, `STATE_PATH` |
| Plugin registry | `plugins/registry.py` | Register and look up implementations by `site_key` / processor name | `PluginRegistry`, `registrar` |
| Client base | `plugins/base/client.py` | Config parsing, book directory, stage detection | `AbstractClient`, `BaseClient` |
| Download flow | `plugins/mixins/download.py` | Metadata, chapter plan, concurrent fetching, batched storage | `DownloadMixin.download_book()` |
| Processing flow | `plugins/mixins/process.py` | Run processors stage by stage and produce new SQLite files | `ProcessMixin.process_book()` |
| Export flow | `plugins/mixins/export_txt.py`, `export_epub.py`, `export_html.py` | TXT / EPUB / HTML generation | `ExportTxtMixin`, `ExportEpubMixin`, `ExportHtmlMixin` |
| Cleanup flow | `plugins/mixins/cleanup.py` | Delete cache, metadata, chapters, or a whole book | `CleanupMixin` |
| Rule sources | `plugins/rule/` | Parse JSON rules and register them dynamically | `RuleFetcher`, `RuleParser`, `RuleSearcher`, `register_all()` |
| Aggregate search | `plugins/search.py` | Concurrent multi-site search and result merging | `search()`, `search_stream()` |
| Task orchestration | `apps/core/task_manager.py` | Queueing, concurrency, retries, cancellation | `TaskManager`, `manager` |
| Task model | `apps/core/models.py` | Task state and progress | `DownloadTask`, `Status` |
| Follow list | `apps/core/watchlist.py` | Tracked books and their latest chapters | `WatchlistStore`, `WatchEntry` |
| REST routes | `apps/api/routes.py` | `/api/v1/*` endpoints | `router`, `create_app()` |
| Music | `apps/music/providers/` | Platform registry and lazy loading | `PLATFORMS`, `create()` |

---

## 4. Task Lifecycle

### 4.1 Direct CLI mode

The CLI `download` command bypasses the task queue and builds a client directly:

```
cli_main()
  → DownloadCmd.run()
  → ConfigAdapter(load_config())
  → registrar.get_client(site, adapter.get_client_config(site))
  → client.download_book(book)       # fetch and write chapter.raw.sqlite
  → client.process_book(book, ...)   # when processors are configured
  → client.export_book(book, ...)    # produce TXT / EPUB / HTML
```

`apps/cli/commands/download.py` performs download, processing, and export in the same process. `--no-export` skips export, and `--json` prints a machine-readable summary.

### 4.2 API / desktop client mode

The API and desktop client share the `TaskManager` from `apps/core/task_manager.py`. States are defined by `Status` in `apps/core/models.py`:

```
QUEUED → RUNNING → PROCESSING → EXPORTING → COMPLETED
   │         │           │           │
   └─────────┴───────────┴───────────┴──→ FAILED / CANCELLED
```

Key behavior:

1. `TaskManager.add_task()` appends the task to `pending[site]` and starts a worker for that site.
2. **One worker per site, strict FIFO within a site; different sites run in parallel.**
3. `RUNNING`: when `login_required` is true, `client.login()` runs first; then `client.download_book()`.
4. If the site configures processors, the task enters `PROCESSING`; a single-threaded worker runs `client.process_book()` through `asyncio.to_thread()` (processors are synchronous).
5. The task then enters `EXPORTING`, also running `client.export_book()` in a thread.
6. Success sets `COMPLETED`; failure sets `FAILED` with `error`; cancellation sets `CANCELLED`.
7. **Retry policy**: only transient transport errors are retried (`is_retryable_error()`); login failures, permission errors, missing data, and programming errors are not. The extra retry budget comes from `general.download_retry_times`, defaulting to `DEFAULT_MAX_DOWNLOAD_RETRIES = 2`.
8. Finished tasks are kept in the `completed` deque, capped at `MAX_COMPLETED_TASKS = 100`.

Corresponding API endpoints:

| Endpoint | Behavior |
| --- | --- |
| `POST /api/v1/tasks` | Create a task |
| `GET /api/v1/tasks` | Query pending / running / completed |
| `GET /api/v1/tasks/{task_id}` | Query one task |
| `POST /api/v1/tasks/{task_id}/cancel` | Cancel a task |
| `GET /api/v1/tasks/stream` | SSE snapshot stream |
| `GET /api/v1/history` | Exported files |
| `POST /api/v1/search`, `GET /api/v1/search/stream` | Aggregate search |
| `GET /api/v1/music/platforms` and others | Music search / download |

Every route is mounted behind the `require_token` dependency: when a token is configured, requests must carry `Authorization: Bearer <token>` or `X-API-Key`.

### 4.3 Inside the download stage

`DownloadMixin.download_book()` (`plugins/mixins/download.py`):

1. `get_book_info()` fetches metadata; cached data is read from `book_info.raw.json` when available.
2. Opens or reuses `chapter.raw.sqlite` and fills in any missing chapter IDs.
3. Builds the chapter plan from `BookConfig.start_id` / `end_id` / `ignore_ids`.
4. Uses `asyncio.Semaphore(self.workers)` to bound concurrency; after each chapter request it waits for a jittered `request_interval`.
5. Chapters are written through a bounded queue in batches; batch size is controlled by `storage_batch_size`.
6. With `cache_chapter = true`, chapters that already exist with `need_refetch = 0` are skipped, enabling resumable downloads.

### 4.4 Inside the processing stage

`ProcessMixin.process_book()` runs processors in configuration order:

* Each processor reads the previous stage's SQLite file and writes `chapter.<stage>.sqlite` plus `book_info.<stage>.json`.
* Stage records are written to `pipeline.json` (`pipeline` and `executed`).
* In incremental mode, configuration hashes and `depends_on` are compared to avoid reprocessing.

### 4.5 Inside the export stage

`export_book()` looks up client methods based on `ExporterConfig.split_mode` (default `book`) and the format name, for example:

* `_export_book_txt` / `_export_chapter_txt`
* `_export_book_epub` / `_export_volume_epub`
* `_export_book_html` / `_export_chapter_html`

When `stage` is not specified, `BaseClient._detect_latest_stage()` walks `pipeline.json` backwards and picks the last stage for which both `chapter.<stage>.sqlite` and `book_info.<stage>.json` exist, falling back to `raw`.

---

## 5. Extension Points

### 5.1 Site adapter (Python plugin)

To add a site, create the following files under `plugins/sites/<site_key>/` as needed:

| File | Required | Base class | Registration decorator |
| --- | --- | --- | --- |
| `fetcher.py` | Required for a generated `_client` | `plugins.base.fetcher.GenericFetcher` | `@registrar.register_fetcher()` |
| `parser.py` | Required for a generated `_client` | `plugins.base.parser.BaseParser` | `@registrar.register_parser()` |
| `searcher.py` | Required to participate in search | `plugins.base.searcher.BaseSearcher` | `@registrar.register_searcher()` |
| `client.py` | Optional | `BaseClient` + mixins | `@registrar.register_client()` |

Example:

```python
from novel_downloader.plugins.base.fetcher import GenericFetcher
from novel_downloader.plugins.registry import registrar


@registrar.register_fetcher()
class ExampleFetcher(GenericFetcher):
    site_name = "example"
    DEFAULT_BASE_URL = "https://example.com"
```

Conventions and lookup order:

* `site_key` is lowercase; a leading digit is automatically prefixed with `n` (`23qb` → `n23qb`).
* Without an explicit key, the registry derives it from the module path: `plugins.sites.<site>.fetcher`.
* `registrar.get_fetcher()` / `get_parser()` / `get_searcher_class()` check registered entries first, then try importing the built-in module, and finally try rule sources.
* When a site has no dedicated `client.py`, `registrar.get_client()` falls back to `CommonClient` in `plugins/common/client.py`, which composes download, processing, all three exporters, and cleanup mixins.
* Local plugins: set `enable_local_plugins = true` under `[plugins]`; the default namespace is `novel_plugins`. With `override_builtins = true`, local implementations win.

### 5.2 Rule-driven book sources (no code)

Rule files are parsed by `plugins/rule/model.py`; registration uses `registrar.register_site()` to generate `RuleFetcher` / `RuleParser` / `RuleSearcher` dynamically.

* Load locations (later overrides earlier): built-in `plugins/rule/builtin/*.json` → `./rules/*.json` → `$NOVEL_RULES_DIR/*.json` → `rules/*.json` under the user config directory.
* Files starting with `_` are not loaded; use them for templates.
* Supports search, details, table of contents, body text, pagination, and URL resolution, with both HTML and JSON response modes. It does not support signatures, encryption, or other script-dependent logic.
* See [Rule-driven Book Sources](book-sources.md) for the full field reference.

### 5.3 Processors

A processor implements `ProcessorProtocol` (`plugins/protocols/processor.py`): `process_book_info()` and `process_chapter()`.

```python
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import BookInfoDict, ChapterDict


@registrar.register_processor("my_processor")
class MyProcessor:
    def __init__(self, config: dict) -> None:
        self.config = config

    def process_book_info(self, book_info: BookInfoDict) -> BookInfoDict:
        return book_info

    def process_chapter(self, chapter: ChapterDict) -> ChapterDict:
        return chapter
```

* When no name is passed, the registration key is the module path after `.processors.`.
* Select processors in configuration through `[[general.processors]]` or `[[sites.<site>.processors]]`; when a site-level list exists, only that list is used.
* Built-in implementations live in `plugins/processors/`: `cleaner`, `zh_convert`, `corrector`, `translator.google`, `translator.edge`, `translator.youdao`.

### 5.4 Exporters

Exporters are not a runtime registry; they are method-name conventions on the client:

* Whole-book export: `_export_{split_mode}_{fmt}`, such as `_export_book_txt` or `_export_volume_epub`.
* Single-chapter export: `_export_chapter_{fmt}`.

To add a format, implement a mixin (see `plugins/mixins/export_txt.py`) and provide matching method names on the client class; `CommonClient.export_book()` looks up methods dynamically from the `formats` list. Missing methods return an empty list and trigger `ExportUI.on_unsupported()`.

---

## 6. Data Directory Layout

### 6.1 Application home

`app_home()` in `infra/paths.py` resolves in this order:

1. The `NOVEL_HOME` environment variable
2. The platform data directory, e.g. `%LOCALAPPDATA%\novel_downloader` on Windows or `~/.local/share/novel_downloader` on Linux

On startup, the desktop client changes directory to this location and creates `settings.toml` there (`apps/desktop/main.py::_prepare_home()`). `state.json` also lives there and stores preferences such as language.

### 6.2 Working directory

The CLI and API use the current working directory as their base. Configuration lookup order is:

1. The file passed with `--config`
2. `settings.toml` / `settings.json` in the current directory
3. A file with the same name in the application home

Default relative directories:

| Path | Content |
| --- | --- |
| `./raw_data/<site>/<book_id>/book_info.<stage>.json` | Book metadata (`stage` defaults to `raw`) |
| `./raw_data/<site>/<book_id>/chapter.<stage>.sqlite` | Chapter body database |
| `./raw_data/<site>/<book_id>/pipeline.json` | Processing pipeline records (`pipeline`, `executed`) |
| `./raw_data/<site>/<book_id>/media/` | Images, fonts, and other locally cached resources |
| `./novel_cache/<site>/` | Per-site cookies and cache |
| `./novel_cache/fixed_fonts/`, `./novel_cache/fixed_font_map/` | Obfuscated-font cache |
| `./downloads/` | TXT / EPUB / HTML export results |
| `./logs/` | Runtime logs (`general.debug.log_dir`) |
| `./debug/<site>/html/` | Raw HTML saved when `general.debug.save_html = true` |

Additional rule-source locations: `./rules/`, `$NOVEL_RULES_DIR`, `<user config dir>/novel_downloader/rules/`.

### 6.3 Android

The Android app stores no raw book data. It calls the REST API, displays tasks and history files, and hands exported files to the system downloader.