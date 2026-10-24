# 架构说明

本文说明 NovelDownloader 的代码分层、任务生命周期、扩展点与数据目录布局。文中路径与类名均对应仓库中的真实实现（`src/novel_downloader/`）。

---

## 1. 设计目标

* **站点差异隔离**：每个站点的抓取、解析、搜索逻辑放在独立插件包中，核心流程不感知具体站点。
* **一套服务层、多个入口**：CLI、本地 REST API、桌面客户端共用 `plugins` + `apps/core`，Android 客户端只通过 REST API 接入。
* **规则驱动扩展**：无需写 Python 即可用 JSON 描述一个书源（`plugins/rule/`）。
* **可恢复**：元数据与章节分别落盘，下载中断后可基于已有数据续爬。
* **可替换基础设施**：HTTP 后端、存储、导出器、处理器均可在不改核心流程的情况下替换。

---

## 2. 分层结构

```
┌──────────────────────────── 入口层 ────────────────────────────┐
│  novel-cli            novel-api          novel-desktop          │
│  apps/cli             apps/api           apps/desktop           │
│                                                               │
│  Android App（android/，Kotlin + Compose，仅调用 REST API）      │
└──────────────────────────────┬────────────────────────────────┘
                               │ 调用
┌──────────────────────────────▼────────────────────────────────┐
│ 应用 / 编排层                                                   │
│  apps/core     TaskManager、DownloadTask、Status、UI 适配器、watchlist │
│  apps/common   历史导出文件枚举                                  │
│  apps/music    音乐平台注册表与下载器                            │
│  apps/login    交互式浏览器登录                                  │
└──────────────────────────────┬────────────────────────────────┘
                               │ 调用
┌──────────────────────────────▼────────────────────────────────┐
│ 领域 / 插件层                                                   │
│  plugins/registry.py      PluginRegistry / registrar           │
│  plugins/protocols/       Client / Fetcher / Parser / Processor│
│  plugins/base/            AbstractClient、BaseFetcher、...      │
│  plugins/mixins/          Download / Process / Export / Cleanup│
│  plugins/sites/<site>/    fetcher.py、parser.py、searcher.py    │
│  plugins/rule/            JSON 规则书源引擎                     │
│  plugins/processors/      cleaner、zh_convert、translator、...  │
│  plugins/search.py        聚合搜索、排序、缓存                   │
└──────────────────────────────┬────────────────────────────────┘
                               │ 依赖
┌──────────────────────────────▼────────────────────────────────┐
│ 基础设施 / 库层                                                 │
│  infra/    config、persistence、sessions、cookies、paths、i18n │
│  libs/     crypto、epub_builder、html_builder、filesystem、...  │
│  schemas/  BookConfig、ClientConfig、BookInfoDict、ChapterDict │
└───────────────────────────────────────────────────────────────┘
```

依赖方向是单向的：`apps/* → plugins/* → infra/* / libs/*`，`schemas/*` 作为共享数据模型可被任意层引用。`infra` 与 `libs` 不反向依赖 `plugins` 或 `apps`。

### 入口层

| 入口 | 代码位置 | 启动方式 | 说明 |
| --- | --- | --- | --- |
| CLI | `apps/cli/main.py` | `novel-cli` | `argparse` 子命令：`download` / `search` / `follow` / `export` / `login` / `config` / `clean` / `storage` / `doctor` / `sources` / `music` |
| REST API | `apps/api/main.py` | `novel-api` | FastAPI + uvicorn，默认 `127.0.0.1:8081`，路由前缀 `/api/v1` |
| 桌面客户端 | `apps/desktop/main.py` | `novel-desktop` | Tkinter UI；`apps/desktop/engine.py` 中的 `AsyncEngine` 持有后台 asyncio 事件循环 |
| Android | `android/app/src/main/java/com/lingyi/noveldl/` | Gradle 构建 | 通过 HTTP 调用本地 API，不直接依赖 Python 代码 |

命令入口在 `pyproject.toml` 的 `[project.scripts]` 中声明。

---

## 3. 关键模块

| 模块 | 真实路径 | 职责 | 关键符号 |
| --- | --- | --- | --- |
| 数据模型 | `schemas/` | 跨层数据类与类型定义 | `BookConfig`、`ClientConfig`、`FetcherConfig`、`ParserConfig`、`ExporterConfig`、`ProcessorConfig`、`BookInfoDict`、`ChapterDict` |
| 配置加载 | `infra/config/file_io.py` | 按优先级查找并解析 `settings.toml` / `settings.json` | `load_config()`、`copy_default_config()` |
| 配置适配 | `infra/config/adapter.py` | 合并全局与站点配置，产出各阶段配置对象 | `ConfigAdapter.get_client_config()`、`get_processor_configs()` |
| 章节存储 | `infra/persistence/chapter_storage.py` | SQLite 章节读写、续爬标记 | `ChapterStorage`，表 `chapters` |
| 用户状态 | `infra/persistence/state.py` | 语言等用户偏好 | `StateManager`、`state_mgr` |
| HTTP 会话 | `infra/sessions/` | 可插拔后端 | `BaseSession`、`create_session()`、`_aiohttp.py` / `_httpx.py` / `_curl_cffi.py` |
| 路径常量 | `infra/paths.py` | 应用主目录、内置资源路径 | `app_home()`、`STATE_PATH` |
| 插件注册 | `plugins/registry.py` | 按 `site_key` / 处理器名注册与查找实现 | `PluginRegistry`、`registrar` |
| 客户端基类 | `plugins/base/client.py` | 配置解析、书目录、阶段检测 | `AbstractClient`、`BaseClient` |
| 下载流程 | `plugins/mixins/download.py` | 元数据、章节计划、并发抓取、批量入库 | `DownloadMixin.download_book()` |
| 处理流程 | `plugins/mixins/process.py` | 逐阶段执行处理器并生成新 SQLite | `ProcessMixin.process_book()` |
| 导出流程 | `plugins/mixins/export_txt.py`、`export_epub.py`、`export_html.py` | TXT / EPUB / HTML 生成 | `ExportTxtMixin`、`ExportEpubMixin`、`ExportHtmlMixin` |
| 清理流程 | `plugins/mixins/cleanup.py` | 删除缓存、元数据、章节或整本书 | `CleanupMixin` |
| 规则书源 | `plugins/rule/` | JSON 规则解析与动态注册 | `RuleFetcher`、`RuleParser`、`RuleSearcher`、`register_all()` |
| 聚合搜索 | `plugins/search.py` | 多站点并发搜索与结果合并 | `search()`、`search_stream()` |
| 任务编排 | `apps/core/task_manager.py` | 队列、并发、重试、取消 | `TaskManager`、`manager` |
| 任务模型 | `apps/core/models.py` | 任务状态与进度 | `DownloadTask`、`Status` |
| 追更列表 | `apps/core/watchlist.py` | 记录关注的书籍与最新章节 | `WatchlistStore`、`WatchEntry` |
| REST 路由 | `apps/api/routes.py` | `/api/v1/*` 端点 | `router`、`create_app()` |
| 音乐 | `apps/music/providers/` | 平台注册表与惰性加载 | `PLATFORMS`、`create()` |

---

## 4. 任务生命周期

### 4.1 CLI 直连模式

CLI 的 `download` 命令不经过任务队列，而是直接构造客户端：

```
cli_main()
  → DownloadCmd.run()
  → ConfigAdapter(load_config())
  → registrar.get_client(site, adapter.get_client_config(site))
  → client.download_book(book)       # 抓取并写入 chapter.raw.sqlite
  → client.process_book(book, ...)   # 配置了 processors 时
  → client.export_book(book, ...)    # 生成 TXT / EPUB / HTML
```

`apps/cli/commands/download.py` 会在同一进程中依次完成下载、处理、导出；`--no-export` 可跳过导出，`--json` 输出机器可读摘要。

### 4.2 API / 桌面客户端模式

API 与桌面客户端共享 `apps/core/task_manager.py` 中的 `TaskManager`。状态定义在 `apps/core/models.py` 的 `Status`：

```
QUEUED → RUNNING → PROCESSING → EXPORTING → COMPLETED
   │         │           │           │
   └─────────┴───────────┴───────────┴──→ FAILED / CANCELLED
```

关键行为：

1. `TaskManager.add_task()` 将任务加入 `pending[site]`，并为该站点启动一个 worker。
2. **每个站点一个 worker，站点内严格 FIFO；不同站点并行**。
3. `RUNNING` 阶段：若 `login_required` 为真，先调用 `client.login()`；随后 `client.download_book()`。
4. 若站点配置了 processors，任务进入 `PROCESSING`，由单线程 worker 通过 `asyncio.to_thread(client.process_book, ...)` 执行（处理器是同步 API）。
5. 随后进入 `EXPORTING`，同样在线程中执行 `client.export_book()`。
6. 成功置为 `COMPLETED`；失败置为 `FAILED` 并记录 `error`；取消置为 `CANCELLED`。
7. **重试策略**：只有瞬时传输错误会重试（`is_retryable_error()`）；登录失败、权限、数据缺失、编程错误等永久错误不重试。额外重试次数由 `general.download_retry_times` 控制，默认 `DEFAULT_MAX_DOWNLOAD_RETRIES = 2`。
8. 已完成任务保留在 `completed` 双端队列中，上限 `MAX_COMPLETED_TASKS = 100`。

API 对应端点：

| 端点 | 行为 |
| --- | --- |
| `POST /api/v1/tasks` | 创建任务 |
| `GET /api/v1/tasks` | 查询 pending / running / completed |
| `GET /api/v1/tasks/{task_id}` | 查询单个任务 |
| `POST /api/v1/tasks/{task_id}/cancel` | 取消任务 |
| `GET /api/v1/tasks/stream` | SSE 快照流 |
| `GET /api/v1/history` | 已导出文件 |
| `POST /api/v1/search`、`GET /api/v1/search/stream` | 聚合搜索 |
| `GET /api/v1/music/platforms` 等 | 音乐搜索 / 下载 |

所有路由挂载在 `require_token` 依赖下：配置了 token 时，请求必须带 `Authorization: Bearer <token>` 或 `X-API-Key`。

### 4.3 下载阶段内部

`DownloadMixin.download_book()`（`plugins/mixins/download.py`）：

1. `get_book_info()` 获取元数据；命中缓存时读取 `book_info.raw.json`。
2. 建立/复用 `chapter.raw.sqlite`，补齐缺失的章节 ID。
3. 根据 `BookConfig.start_id` / `end_id` / `ignore_ids` 生成章节计划。
4. 以 `asyncio.Semaphore(self.workers)` 控制并发；每个章节请求后按 `request_interval` 抖动等待。
5. 章节写入通过有界队列批量提交，批量大小由 `storage_batch_size` 控制。
6. `cache_chapter = true` 时，已存在且 `need_refetch = 0` 的章节会跳过，实现断点续爬。

### 4.4 处理阶段内部

`ProcessMixin.process_book()` 按配置顺序执行处理器：

* 每个处理器读取上一阶段 SQLite，写出 `chapter.<stage>.sqlite` 与 `book_info.<stage>.json`。
* 阶段记录写入 `pipeline.json`（`pipeline` 与 `executed`）。
* 增量模式下会比较配置哈希与 `depends_on`，避免重复处理。

### 4.5 导出阶段内部

`export_book()` 根据 `ExporterConfig.split_mode`（默认 `book`）和格式名查找客户端方法，例如：

* `_export_book_txt` / `_export_chapter_txt`
* `_export_book_epub` / `_export_volume_epub`
* `_export_book_html` / `_export_chapter_html`

未指定 `stage` 时，`BaseClient._detect_latest_stage()` 会从 `pipeline.json` 反向查找最后一个同时存在 `chapter.<stage>.sqlite` 与 `book_info.<stage>.json` 的阶段，找不到则回退到 `raw`。

---

## 5. 扩展点

### 5.1 站点适配器（Python 插件）

新增站点时，在 `plugins/sites/<site_key>/` 下按需提供：

| 文件 | 必需 | 基类 | 注册装饰器 |
| --- | --- | --- | --- |
| `fetcher.py` | 生成 `_client` 时需要 | `plugins.base.fetcher.GenericFetcher` | `@registrar.register_fetcher()` |
| `parser.py` | 生成 `_client` 时需要 | `plugins.base.parser.BaseParser` | `@registrar.register_parser()` |
| `searcher.py` | 需要参与搜索时 | `plugins.base.searcher.BaseSearcher` | `@registrar.register_searcher()` |
| `client.py` | 可选 | `BaseClient` + mixins | `@registrar.register_client()` |

示例：

```python
from novel_downloader.plugins.base.fetcher import GenericFetcher
from novel_downloader.plugins.registry import registrar


@registrar.register_fetcher()
class ExampleFetcher(GenericFetcher):
    site_name = "example"
    DEFAULT_BASE_URL = "https://example.com"
```

约定与查找顺序：

* `site_key` 小写；以数字开头时自动补 `n` 前缀（如 `23qb` → `n23qb`）。
* 未显式指定 key 时，注册表从模块路径推导：`plugins.sites.<site>.fetcher`。
* `registrar.get_fetcher()` / `get_parser()` / `get_searcher_class()` 先查已注册项，再尝试导入内置模块，最后尝试规则书源。
* 没有站点专属 `client.py` 时，`registrar.get_client()` 回退到 `plugins/common/client.py` 的 `CommonClient`，它组合了下载、处理、三种导出与清理 mixin。
* 本地插件：在 `[plugins]` 中设置 `enable_local_plugins = true`，默认命名空间为 `novel_plugins`；`override_builtins = true` 时本地实现优先。

### 5.2 规则书源（无代码）

规则文件由 `plugins/rule/model.py` 解析，注册时通过 `registrar.register_site()` 动态生成 `RuleFetcher` / `RuleParser` / `RuleSearcher`。

* 加载位置（后者覆盖前者）：内置 `plugins/rule/builtin/*.json` → `./rules/*.json` → `$NOVEL_RULES_DIR/*.json` → 用户配置目录下的 `rules/*.json`。
* 以 `_` 开头的文件不加载，适合放模板。
* 支持搜索、详情、目录、正文、分页、URL 解析，以及 HTML 与 JSON 两种响应模式；不支持签名、加密等需要脚本的逻辑。
* 详细字段见 [规则书源](book-sources.md)。

### 5.3 处理器

处理器实现 `ProcessorProtocol`（`plugins/protocols/processor.py`）：`process_book_info()` 与 `process_chapter()`。

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

* 不传名字时，注册键取模块路径中 `.processors.` 之后的部分。
* 配置中通过 `[[general.processors]]` 或 `[[sites.<site>.processors]]` 选择处理器；站点级列表存在时只使用站点级列表。
* 内置实现位于 `plugins/processors/`：`cleaner`、`zh_convert`、`corrector`、`translator.google`、`translator.edge`、`translator.youdao`。

### 5.4 导出器

导出器不是运行时注册表，而是客户端上的约定方法：

* 全书导出：`_export_{split_mode}_{fmt}`，例如 `_export_book_txt`、`_export_volume_epub`。
* 单章导出：`_export_chapter_{fmt}`。

新增格式时，实现对应 mixin（参考 `plugins/mixins/export_txt.py`）并在客户端类中提供匹配的方法名；`CommonClient.export_book()` 会按 `formats` 列表动态查找方法。未实现的方法会在结果中返回空列表并触发 `ExportUI.on_unsupported()`。

---

## 6. 数据目录布局

### 6.1 应用主目录

`infra/paths.py` 的 `app_home()` 解析顺序：

1. `NOVEL_HOME` 环境变量
2. 平台数据目录，例如 Windows 的 `%LOCALAPPDATA%\novel_downloader`、Linux 的 `~/.local/share/novel_downloader`

桌面客户端启动时会 `chdir` 到该目录，并在此生成 `settings.toml`（`apps/desktop/main.py::_prepare_home()`）。`state.json` 也位于该目录，保存语言等偏好。

### 6.2 工作目录

CLI 与 API 以当前工作目录为基准，配置查找顺序为：

1. 命令行 `--config` 指定的文件
2. 当前目录的 `settings.toml` / `settings.json`
3. 应用主目录中的同名文件

默认相对目录如下：

| 路径 | 内容 |
| --- | --- |
| `./raw_data/<site>/<book_id>/book_info.<stage>.json` | 书籍元数据（`stage` 默认 `raw`） |
| `./raw_data/<site>/<book_id>/chapter.<stage>.sqlite` | 章节正文数据库 |
| `./raw_data/<site>/<book_id>/pipeline.json` | 处理流水线记录（`pipeline`、`executed`） |
| `./raw_data/<site>/<book_id>/media/` | 下载到本地的图片、字体等资源 |
| `./novel_cache/<site>/` | 站点级 Cookie 与缓存 |
| `./novel_cache/fixed_fonts/`、`./novel_cache/fixed_font_map/` | 混淆字体缓存 |
| `./downloads/` | TXT / EPUB / HTML 导出结果 |
| `./logs/` | 运行日志（`general.debug.log_dir`） |
| `./debug/<site>/html/` | `general.debug.save_html = true` 时保存的原始 HTML |

规则书源的额外位置：`./rules/`、`$NOVEL_RULES_DIR`、`<用户配置目录>/novel_downloader/rules/`。

### 6.3 Android 端

Android App 不保存书籍原始数据，只负责调用 REST API、展示任务与历史文件，并把导出文件交给系统下载器保存。