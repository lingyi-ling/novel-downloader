# 交接手册 · 书音 BookTone

> 面向下一个接手本仓库的会话。只写**当前真实状态、坑、下一步**；项目规则以 `AGENTS.md` 为准。
> 本文件替换 2026-10-17 的旧交接材料（旧版描述的 NiceGUI `novel-web` 已经移除，别再去 `apps/web/` 找东西）。

---

## 0. 一句话现状

- **版本 v1.22.1**，分支 `main`，工作区干净；最新 release：<https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.22.1>（8 个资产）
- 全量测试 **8959 passed / 32 skipped**；`ruff check`、`ruff format --check`、`mypy src` 全绿
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

### 搜索速度

- `search(timeout=…)` 是**每站点墙钟预算**：超时站点丢弃，整体 ≈ 预算（5s → 约 5.2s，之前 116s）。
- Windows proactor 的取消可能延迟，所以超时后不 await 卡住的任务，只发取消并回收异常。
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

### UI

- 单文件 `web_assets.py`：设计令牌、壳、控件、卡片、音乐、Toast、弹窗都在这里。
- 结果卡片有封面 + 首字回落；队列行有 2px 状态色条；支持 `prefers-reduced-motion`。
- **DOM id / `data-*` / 桥接方法名是测试契约**，重构不要改；结构断言在 `tests/apps/test_desktop_app.py`。
- 窗口几何恢复按当前显示器裁剪（`window_state.clamp_geometry(geometry, bounds)`）；多显示器负坐标保留。

---

## 5. 已知缺口（按优先级）

1. **歌单 / m3u8 导出没有 UI 入口**。引擎、CLI（`novel-cli music export-m3u8`）、REST `POST /api/v1/music/export` 都支持，但桌面网页和 Android Compose 都没有按钮。这是用户最近一次问到的"缺 m3u8"的真正含义。
2. **Android 音乐未真机验证**。有设备后：开独立引擎 → 音乐页 → 选平台搜一首 → 任务完成 → 确认公共「下载」目录出现文件。
3. **重依赖平台不可用**：TIDAL（`aigpy`/`pywidevine`/`av`）、YouTube Music（`ytmusicapi`）、SoundCloud HLS（需外部 `N_m3u8DL-RE`）、歌词转写（`faster_whisper`）未打包；这些平台可能搜得到、下不了。
4. **SQLite 每章提交仍在事件循环上**（`storage_batch_size=1`）。连接有线程亲和性，改 `to_thread` 要 `check_same_thread=False` + 锁，风险 > 收益，暂缓。
5. **桌面 `#detail` 在 <1180px 窗口隐藏**（媒体查询），窄窗口看不到详情面板，可改折叠抽屉。
6. **许可证文案不一致**：`LICENSE` 是 MIT，但官网页脚写 Apache-2.0。改之前先问用户。
7. **旧文档/截图**：`docs/assets/images/` 里部分截图来自已删除的 NiceGUI Web UI，可能与现状不符；不要拿它们当验收依据。

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
