# 交接手册 · 书音 BookTone

> 面向下一个接手本仓库的会话。只写**当前真实状态、坑、下一步**；项目规则以 `AGENTS.md` 为准。
> 本文件替换 2026-10-17 的旧交接材料（旧版描述的 NiceGUI `novel-web` 已经移除，别再去 `apps/web/` 找东西）。

---

## 0. 一句话现状

- **版本 v1.31.0**，分支 `main`；最新 release：<https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.31.0>
- 全量测试 **12096 passed / 42 skipped**；`ruff check`、`ruff format --check`（790 files）、`mypy src`（548 files）全绿；Android **425 unit tests**、`lintDebug` 0 error
- 产品名 **书音 BookTone**；主色 **藏青 `#1E3A5F`（结构）+ 暖橙 `#C2410C`（动作）**，桌面 / Android / 官网 / 图标 / OG 已统一
- 三端：CLI（`novel-cli`）、REST API（`novel-api`）、Windows 桌面（pywebview + 单文件 HTML）；另有 Android（Kotlin/Compose，可选 Chaquopy 独立引擎）

---

## 1. 五分钟上手

| 项 | 值 |
| --- | --- |
| 工作目录 | `c:\Users\lingy\Desktop\闲散项目\下载器` |
| 解释器 | `.venv\Scripts\python.exe`（Python 3.12） |
| 安装方式 | 源码可编辑安装（`novel_downloader` → `src/novel_downloader`） |
| 入口 | `novel-cli` / `novel-api` / `novel-desktop`（`pyproject.toml [project.scripts]`） |
| 许可证 | **MIT**（`LICENSE`，Copyright (c) 2025 Saudade Z） |
| git 身份 | 已配置：`lingyi-ling <316542251+lingyi-ling@users.noreply.github.com>` |

```bash
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m ruff format --check src tests scripts
.venv/Scripts/python.exe -m mypy src
```

### 启动

```bash
# 桌面端（源码）
.venv/Scripts/python.exe -m novel_downloader.apps.desktop
# 已打包（最近一次 release 构建的 onedir）
./dist/NovelDownloader/NovelDownloader.exe

# REST API
.venv/Scripts/novel-api.exe --port 8811 --rate-limit 0
```

### Android（本机无 emulator / adb）

SDK 位于 `android/local.properties` 指向的 `C:/Android/sdk`：

```bash
cd android
./gradlew.bat compileDebugKotlin --no-daemon --console=plain
./gradlew.bat testDebugUnitTest --no-daemon --console=plain
./gradlew.bat assembleDebug --no-daemon --console=plain
```

**只能编译 + 单测 + 构建 APK**，无法跑 UI。任何"手机上能不能用"的结论都必须在有设备时复核。

---

## 2. 收尾流水线（用户已授权全自动）

按 `AGENTS.md`：门禁 → 提交 → 推送；`website/` 变了 → `scripts/deploy_site.py` + curl 逐字节校验；版本号变了 → `scripts/release.py`。

### 发布网络坑（每次都会遇到）

- 国内环境经常 **`github.com:443` 不通，但 `api.github.com` 正常**。构建、上传、创建 release 都走 API，只有 `git push` 走 github.com。
- `release.py` 已有兜底：tag 推送失败时改用 `POST /git/refs` 创建引用（指向 **tag 自身提交**，不是 HEAD）。若 REST 报 `Object does not exist`，说明 tag 指向的提交还没推上远端——先把 main 推上去。
- **不要 `release.py | tail`**：管道退出码是 `tail` 的，会把失败吞成 0。用：
  ```bash
  .venv/Scripts/python.exe -u scripts/release.py > /tmp/rel.log 2>&1
  tail -20 /tmp/rel.log
  ```
- 大资产（APK 60–75 MB）上传可能 `HTTP 408 Upload body timed out`，构建成功后断点续跑：
  ```bash
  .venv/Scripts/python.exe -u scripts/release.py --skip-build > /tmp/rel.log 2>&1
  ```
  已上传且 digest 一致的资产会跳过。
- `git push` 失败通常重试几轮就好（曾连续失败 6 次后成功）；本机没有 `gh`，GitHub 相关 API 操作用 Python `urllib` + `git credential fill` 取令牌。

### 2026-11-08 起：github.com 被链路拦截

- `git push` 持续报 `Recv failure` / `Failed to connect to github.com:443`；`curl https://github.com/...` 直接返回 **504**（连接 0.1s 就通，说明是中间代理拦截）。`api.github.com` 与 `uploads.github.com` 正常。
- 应急做法：用 GitHub Git Data API 逐提交创建 blob/tree/commit，再 `PATCH /git/refs/heads/main`。API 创建的提交 sha 与本地不同、**tree 一致**；本地 `main` 会与远端分叉，后续提交继续走 API，别再普通 `git push`（会 non-fast-forward）。
- `release.py` 的 tag 推送回退会指向本地提交；本地提交不在远端时会 422。绕法：先用 API 把 `refs/tags/vX.Y.Z` 建在远端 head 上，再把 `origin` 临时指向一个含有同名 tag 的本地 bare 仓库（让 `remote_tag_exists` 为真、跳过 tag 推送），带 `--repo` 跑 release，结束后恢复 origin。
- 本机 `~/.ssh/id_ed25519` 未加入 GitHub：`ssh.github.com:443` 端口可达但 `Permission denied (publickey)`。

### 自动更新（桌面）

- 安装形态：`updater.detect_install_shape()` → `installer` / `portable` / `source`；资产选择据此优先 setup 或 zip
- 安装版：setup + `/SILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`，Inno 原地覆盖，用户数据在 `%LOCALAPPDATA%
ovel_downloader` 不受影响
- 便携版：zip 覆盖当前文件夹，由 `_apply_portable_update.ps1` 在主进程退出后解压并重启；需要 PowerShell（Win10/11 自带）
- 下载：`download_update_async` + `update_download_state` 提供字节进度；`download_asset(resume=True, attempts=3)` 支持 Range 续传
- 清理：`updates/` 里 `.part` 1 天、安装包/zip 7 天后自动删除
- 仍是未启用状态：清单签名（`TRUSTED_UPDATE_KEYS` 为空）。启用后必须每次都发布 `latest.json`，且私钥丢失会导致无法再签新清单

### 搜索可解释性与确定性（v1.30）

- 最终结果带只读 `rank_score`/`match_tier`（在 `plugins/search.py` 注解，不写回缓存；流式分片不含）
- 多词查询（2–4 token）在 `ranking.py`；缓存 key 仍用原始关键词
- `sort_results` 的预排序键是行内容全序（`_tie_break_key`），并行/乱序到达结果一致

### HTTP 冷却与重试预算（v1.30）

- `TokenBucketRateLimiter.penalize()`：429/503 + Retry-After 时同一 limiter 的并发 worker 一起暂停（≤60s、仅向上抖动、取最新截止点）
- `HttpStatusError.retryable=False`；任务级不会再重试 fetch 层已耗尽预算的错误

### 桌面设置页（v1.30）

- 第 6 个页面（Ctrl+6）：存储/网络/更新/外观/应用；`save_prefs` 白名单 + 原子写；输出目录展开与可写探测
- 结果列表分块渲染（首屏 60，`#results-more`）；歌单同名覆盖需确认

### 发布工具（v1.30）

- `scripts/push_via_api.py`：github.com 被墙时走 Git Data API（支持 `--dry-run`、非 force）
- 安装包裁剪：`packaging/_spec_common.py` 的 `UNCUSED_*`/`prune_bundle_toc`；`verify_frozen` 会检查被剪内容不回归

### 并行改进批次（2026-11-10）

- 一次 30 个独立槽位的并行加固；进度与各槽位结论见 `build/agent-loop/state.json`（gitignore）
- 重点：搜索去重/排序/缓存、站点 ID 规范化、规则引擎限制、CLI 路径穿越、音乐 API 数据泄漏与路径约束、队列恢复/章节存储事务、停止丢章、更新器大小/重定向/续传
- 若继续循环：保持每槽位目录隔离；每 ~30 个完成停补位，跑一次全量门禁再提交/发版

### 官网

- 目录 `website/`（纯静态），线上 <http://43.165.170.43/noveldl/>，服务器路径 `/var/www/noveldl`
- 部署：`.venv/Scripts/python.exe scripts/deploy_site.py`
- 校验：`curl -s http://43.165.170.43/noveldl/index.html -o /tmp/l && cmp /tmp/l website/index.html`

---

## 3. 代码地图

| 需求 | 位置 |
| --- | --- |
| 站点插件 | `src/novel_downloader/plugins/sites/<site>/`（fetcher / parser / client / searcher） |
| 下载生命周期 | `plugins/mixins/download.py` |
| 聚合搜索 | `plugins/search.py`（`_search_one` 每站点硬截止、`_abandon` 不等待取消） |
| 任务队列 + 持久化 | `apps/core/task_manager.py` |
| 桌面桥 + 前端 | `apps/desktop/webview_app.py`、`web_assets.py`（单文件 HTML/CSS/JS） |
| Android 独立引擎 | `apps/embedded/server.py`（stdlib HTTP，路由必须镜像 REST） |
| REST API | `apps/api/routes.py` + `schemas.py` |
| 音乐引擎 | `apps/music/`（`providers/musicdl.py` 包装 vendored 平台） |
| 音乐统一搜索 | `apps/music/aggregate.py`（并发 + 合并 + `search_all_isolated`） |
| 音乐曲目缓存 | `apps/music/trackcache.py`（`search_id` → 原始 Track，JSON 往返后仍能解析直链） |
| 混合平台下载 | `apps/music/downloader.py: download_mixed` |
| 官网 | `website/` |

### 必须成对修改

- **新增 REST 路由** → 同步 `apps/embedded/server.py` 的 `ROUTES`，否则 `tests/apps/test_contract_parity.py` 失败。
- **新增用户可见字符串** → `webview_app._i18n_catalog()` 加 `t("...")`；然后
  `update_pot.py` → `update_po.py`（手填 zh_CN、删 `#, fuzzy`）→ `compile_mo.py`。
  前端 `data-i18n` 的 key 由该目录下发，漏了 `test_i18n_catalog_covers_every_html_key` 会失败。
- **新增音乐可选依赖** → 三处同步：`pyproject.toml` 的 `music`/`music-desktop` extras、`android/app/build.gradle.kts` 的 Chaquopy `pip { install(...) }`、`packaging/_spec_common.py` 的 `EXTRA_HIDDEN_IMPORTS`。
- 文档自检命令：`docs/guide/troubleshooting*.md` 里有一行检查完整音乐依赖集合的 `python -c`，加依赖时顺手更新。

---

## 4. 最近几轮做了什么（避免重复劳动）

### 番茄完全匿名可用

- 设备自注册 `register.py`、`x-argus` `argus.py`、`x-ladon` `ladon.py` 全部本地生成，与抓包逐字节一致。
- App 搜索接口免登录、无浏览器；首次搜索自动注册并落盘 `<cache>/fanqienovel/device.json`。
- 分类浏览：榜单页 `rankCategoryTypeList` 给分类；书目走 `GET /api/library/book_list?app_id=2503&category_id=…&gender=…&offset=0&limit=20&word_count=0&sort=0`，**无需签名**，但 `word_count=0&sort=0` 缺一不可。
- ⚠️ **书名/作者是 PUA 字体混淆**，必须靠详情页回填（`_fill_book_metadata`）。不要因为"接口看起来是明文"把它删掉。
- 协议细节：`docs/notes/fanqie-client-protocol.md`。

### 搜索速度与质量

- `search(timeout=…)` 是**每站点墙钟预算**：超时站点丢弃，整体 ≈ 预算（5s → 约 5.2s，之前 116s）。
- Windows proactor 的取消可能延迟，所以超时后不 await 卡住的任务；取消后给 0.35s 有界宽限再退出 session，否则日志会刷 `Task was destroyed` / `Unclosed client session`。
- **quality 排序默认过滤相关性 < 0.45**（`min_relevance`，传 `None` 关闭）：无关结果不再混入，衍生作保留；永不返回空页。非 quality 排序不过滤。
- 过滤作用在**合并后的书目**上（`group=True`）：作者查询时，同书的镜像站可能没有作者字段、单行相关性为 0，但它仍是下载来源，不再被误删。
- 同站同 `book_id` 的重复行自动折叠；跨站重复由「合并同名书」处理，桌面端该开关**默认开启**。
- 站点失败日志统一走 `BaseSearcher._log_fetch_failure()`：超时/被墙记 WARNING，只有真实异常才是 ERROR。
- 桌面端用 `search_async` + `search_progress` 轮询，首屏约 0.2s。

### 底层加固

- `BaseFetcher.fetch` 重试传输错误、4xx 不重试、遵守 `Retry-After`；`get_chapter` 不再叠加请求层预算。
- 分页有 `MAX_PAGES` + 重复页检测；存储 worker 崩溃不卡生产者；坏插件只跳过自己；限流器锁内预留槽位。
- 封面/字体/raw 页面落盘、失败账本追加走 `asyncio.to_thread`；`curl_cffi` 有 `max_clients`。

### 队列持久化

- `<应用数据目录>/tasks.json`：原子写、1.5s 节流、`close()` 补写。
- `manager.start()` 在 API lifespan、桌面 `bridge.start_services()`、内嵌引擎 `start_server()` 调用；恢复任务一律回 `queued`，章节缓存让下载从断点继续。
- 默认关闭（`persist=True` / `state_path=…` 才开），测试与一次性 CLI 不受影响。

### Android 音乐

- 独立引擎已接真实引擎（platforms / search / health / export / download / jobs）。
- 音频写 `<output>/music/`，`/downloads/music/<name>` 暴露回环地址；结果带 `download_url`，Kotlin 交给 `DownloadManager` 落到公共下载目录。
- 依赖已从 5 个补到 17 个（`m3u8`、`emoji`、`bleach`、`filetype`、`puremagic`、`fake-useragent`、`json-repair`、`tinytag`、`tabulate`、`prettytable`、`wcwidth`、`prompt_toolkit`）。

### 音乐统一搜索

- 默认 `platform="all"`：`apps/music/aggregate.py` 并发搜全部平台，同名同歌手合并，`sources` 列出来源；`platforms` 返回每站 `ok/count/error`。
- **每个平台一个线程**（最多 32），不要再改回固定 worker 数：曾因固定 10 个 worker，前 10 个恰全是卡死平台，netease 在队列里第 11 位永远轮不到，表现为「搜完是空的」。
- **失败熔断 10 分钟**（`FAILURE_TTL`）：聚合模式跳过最近失败的平台；状态落盘 `<音乐数据目录>/search_stats.json`，**重启后仍然生效**（`tests/conftest.py` 把 `NOVEL_MUSIC_HOME` 指到临时目录，避免污染本机）
- **慢且空的平台短暂跳过**：连续两次 ≥2s 且 0 结果，跳过 5 分钟（`SLOW_EMPTY_*`）
- **首个可用批次后提前返回**：拿到非空结果后再等 `EARLY_EXIT_GRACE=2s` 就收工，慢尾不拖住搜索；被提前放弃的平台**不记为失败**（可能只是还在请求），下次照常参与
- **渐进式结果**：`search_all(on_update=...)` 每完成一个平台回调一次快照；桌面用 `music_search_async` + `music_search_progress` 轮询，首屏约 0.7–0.9s，最终合并约 2.5s。
- **下载必须回传 `search_id`**：musicdl 的 `extra["info"]`（`SongInfo`）无法 JSON 往返，服务端用 `apps/music/trackcache.py` 取回原始 Track。缓存默认 32 条 / 30 分钟。
- 混合平台下载用 `download_mixed`：按 `track.provider` 分组；主来源失败会回退到 `_alternatives`（合并时挂在原始 Track 上的进程内属性），失败回调只发一次。
- 平台可用性由依赖决定：`tidal` 需要 `aigpy`，缺包时**不注册**（`LIGHT_PLATFORMS`）。
- CLI 的一次性聚合搜索走 `search_all_isolated`（子进程 + `terminate`）；`music` 子命令结束时 `os._exit`（测试进程内会跳过），否则 vendor 的非 daemon 线程会把退出拖到分钟级。
- 健康检查并发、单站 6s；CLI 全部平台约 7s（以前逐站 15s 串行）。

### UI

- 单文件 `web_assets.py`：设计令牌、壳、控件、卡片、音乐、Toast、弹窗都在这里。
- 结果卡片有封面 + 首字回落；队列行有 2px 状态色条；支持 `prefers-reduced-motion`。
- **DOM id / `data-*` / 桥接方法名是测试契约**，重构不要改；结构断言在 `tests/apps/test_desktop_app.py`。
- 窗口几何恢复按当前显示器裁剪（`window_state.clamp_geometry(geometry, bounds)`）；多显示器负坐标保留。

---

## 5. 已知缺口（按优先级）

1. **Android 音乐未真机验证**。有设备后：开独立引擎 → 音乐页 → 选平台搜一首 → 任务完成 → 确认公共「下载」目录出现文件；再点「导出歌单」确认 `.m3u8` 落地。
2. **Android 搜索结果不持久化本地路径**：下载完成后才回填，App 重启后要重新下载才能导出歌单（可考虑服务端搜索返回 `path` 或在客户端缓存）。
3. **重型平台已打包，但仍受账号/DRM 限制**：`aigpy`（TIDAL）、`ytmusicapi`（YouTube Music）、`pywidevine`（Apple Music / SoundCloud）已进 extras、PyInstaller 与 Chaquopy，平台数 24 → 28
   - TIDAL 必须先在「平台凭据」配置账号 token，否则搜索会提示「需要先配置账号信息」
   - Apple Music / SoundCloud 的 DRM 解密需要用户自备 CDM（`.wvd`），SoundCloud HLS 还需要外部 `N_m3u8DL-RE`；**只打包依赖不等于能解密**
   - 歌词转写（`faster_whisper`）仍然不打包：模型 + ctranslate2 有数百 MB，且只影响「无歌词时自动转写」，不影响搜索/下载
4. **SQLite 每章提交仍在事件循环上**（`storage_batch_size=1`）。连接有线程亲和性，改 `to_thread` 要 `check_same_thread=False` + 锁，风险 > 收益，暂缓。
5. **作者查询会丢掉无作者字段的行**：相关性过滤对 `author=""` 且书名不含关键词的行打 0 分，实测会少 1-2 个来源胶囊；若要补偿，需产品决策。
6. **许可证文案不一致**：`LICENSE` 是 MIT，但官网页脚写 Apache-2.0。改之前先问用户。
7. **旧文档/截图**：`docs/assets/images/` 里部分截图来自已删除的 NiceGUI Web UI，可能与现状不符；不要拿它们当验收依据。
8. **8 个站点在本机被 DNS 污染**（mangg_net / dxmwx / esjzone / kadokado / syosetu / westnovel / westnovel_sub / yodu）：外部网络核实站点存活、解析正确，本机需换网络或代理才能搜到。

---

## 6. 视觉验收方法（不依赖真机）

Chromium 路径：

```
C:\Users\lingy\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe
```

步骤：

1. 把 `web_assets.APP_HTML` 写进临时目录的 `index.html`；
2. 在 `<body` 前注入假的 `window.pywebview.api`，给每个桥方法返回 canned 数据；
3. `python -m http.server` 起服务；
4. 截图：
   ```bash
   chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
     --window-size=1440,900 --virtual-time-budget=6000 \
     --screenshot=C:\abs\path\shot.png http://127.0.0.1:PORT/index.html
   ```

坑：

- `--screenshot` 必须 **Windows 绝对路径**，否则报"系统找不到指定的路径"；
- 截图前注入 `<style>*{animation:none!important;transition:none!important}</style>`，否则会拍到 `pageIn` 的半透明帧；
- 从 Git Bash 用 `&`/后台任务启动真实桌面端，窗口可能以最小化出现；用 `Start-Process` 或正常双击。最小化窗口的 `GetWindowRect` 返回 `-25600,-25600 159x27`，用 `ShowWindow(h, 9)` 还原。

---

## 7. 容易踩的测试坑

- `tests/apps/test_contract_parity.py`：REST 与内嵌路由一一对应；音乐端点必须真实存在（不是桩）。
- `tests/test_i18n_coverage.py`：`t()` 的每个 msgid 都要有非空、非 fuzzy 的 zh_CN 翻译。
- `tests/apps/test_desktop_app.py`：断言 HTML 结构、桥接方法名和若干 JS 片段（如 `if (st.error) throw new Error(st.error);`）。改模板先跑这套。
- 测试必须 hermetic：`NOVEL_HOME` 指到 tmp；不要依赖 cwd 里有某个文件（历史上踩过）。
- 全量约 3 分钟；prioritise 局部测试再跑全量。

---

## 8. 用户偏好与边界

- 要求**一口气做完、不要问**，能查证就自己查证；token 充足。
- 逆向那两个番茄客户端包是**作者授权**的（用户原话：都是 linuxdo 的佬友）。
- 不提交任何凭据 / token / keystore / 签名私钥；破坏性 git 操作（改写历史、强推、删 release）必须先征得同意。
- 发布/部署无需逐步确认，但**失败必须如实说**（上传 408、网络中断、未真机验证都要写清楚）。
- 中文回复；conventional commits。
- 用户会直接打开桌面端试用，遇到报错会发截图——先看 `%LOCALAPPDATA%\novel_downloader\logs\novel-desktop.log`。

---

## 9. 快速冒烟

```bash
# 番茄搜索（免登录、App 接口）
.venv/Scripts/novel-cli.exe search -s fanqienovel 斗破 --json --timeout 5 | head -20

# 分类浏览
.venv/Scripts/novel-cli.exe browse fanqienovel --categories

# API（另开进程）
.venv/Scripts/novel-api.exe --port 8811 --rate-limit 0
curl -s "http://127.0.0.1:8811/api/v1/browse/categories?site=fanqienovel" | head -c 200

# 音乐统一搜索（默认全部平台）
.venv/Scripts/novel-cli.exe music search 晴天 --limit 8 --json

# 音乐平台连通性（含 5sing / 咪咕 / 酷我 等）
.venv/Scripts/novel-cli.exe music health --json

# 官网线上校验
curl -s http://43.165.170.43/noveldl/index.html -o /tmp/live.html && cmp /tmp/live.html website/index.html && echo OK
```

---

## 10. 下一步建议

如果用户没有新指示，按 §5 的顺序推进：

1. 桌面音乐页 + Android 音乐页各加一个「导出歌单」，调用已有 `/api/v1/music/export`（桌面走桥接，Android 走 Retrofit）。
2. 用真机验证 Android 音乐下载到公共 Downloads。
3. 处理许可证文案不一致（先问用户是 MIT 还是 Apache-2.0）。
