## v1.19.0 (2026-11-07)

### Refactor

- **desktop UI**: 换成藏青（navy）极简设计体系，整份样式表重写
  - 令牌：`#1E3A5F` 主色 + `#2563EB` 交互色 + `#059669` 成功色，浅色底 `#F8FAFC`；暗色底 `#0B1220`、卡片 `#111C2E`，两套都按对比度重新取值
  - 去掉渐变、阴影装饰与橙色品牌，改为平面白/深色底 + 1px 发丝边；卡片、按钮、输入框圆角统一到 6/8/12px
  - 侧栏改为纯色底 + 细边，导航激活态用淡蓝底 + 蓝字，移除左侧悬浮竖条；版本号不再大写化
  - 搜索结果去掉「Author:」前缀，作者/字数/日期直接并列；选中态改为内描边，不再有外发光环
  - 队列行状态条、状态胶囊、进度条、音乐设置开关、Toast、弹窗全部按新令牌重绘
  - 亮/暗两套在 1024 / 1280 / 1440 三种宽度下逐页截图核对

### Test

- 桌面端 194 例、全量 `pytest` 8936 passed；`ruff` / `format` / `mypy` 全过

## v1.18.0 (2026-11-07)

### Refactor

- **desktop UI**: 桌面端界面重构，所有 DOM id、data-* 契约与桥接方法保持不变
  - **统一内容列**：所有页面共用 1120px 内容列，标题与卡片使用同一组左右边距；搜索框不再与结果卡片错位
  - **搜索结果加封面**：46×62 缩略图，无封面或图片加载失败时回落到首字渐变块（图片本身是装饰元素，标题就在旁边）
  - **任务行加状态色条**：运行/排队/完成/失败/取消各一条 3px 色条，不再靠额外徽章区分状态
  - **导航**：快捷键角标改为悬停显示，侧栏底部加分隔线，下载数量徽章独占右侧
  - **详情面板**：封面占位、元信息行内排布、浏览器入口独占一行，窄栏不再挤压换行
  - **可访问性**：结果卡片可 Tab 聚焦、Enter/空格选中；追更删除按钮补 `title`/`aria-label`；暗色模式次级文字提亮到 4.5:1
  - **动效**：新增 `prefers-reduced-motion` 支持，页面切换/进度条动画在系统要求减少动效时关闭

### Test

- 桌面端 194 例通过，全量 `pytest` 8937 passed
- 用无头 Chromium 在 1280/1440 两种宽度、亮/暗两套主题下逐页截图核对（搜索、下载、历史、音乐、追更）

## v1.17.0 (2026-11-07)

### Fix

- **fetcher**: `fetch()` 现在重试**传输层错误**（超时/连接重置/DNS），之前只重试 HTTP 状态码，一次网络抖动就能让书籍信息抓取直接失败。反过来，404/403 这类确定性状态不再重试（原来会白白重试 4 次）。429/503 带 `Retry-After` 时按服务端要求等待（上限 30s）
- **download**: 请求层的重试预算不再被章节层的循环重复消耗。失败章节的请求数从 `(retry_times+1)²` 降为 `retry_times+1`；错误原因也变回可读的 `http_404` / `network_error`，而不是笼统的 `retry_exhausted`
- **fetcher**: 分页加上 `MAX_PAGES=500` 上限与重复页检测。一个登录墙或永远回同一页的站点，之前会无限循环并把页面堆进内存
- **download**: 存储 worker 崩溃不再把整本书卡死。之前它会带着异常退出，而生产者在 `maxsize=10` 的队列上永久阻塞；现在 worker 持续排空队列、把失败记进账本，停机路径用 `put_nowait` + 超时，取消时先停生产者再 flush
- **registry**: 单个坏插件（缺依赖、语法错误）不再让整站搜索失败，只跳过它并记一条 warning。之前 `novel_plugins/` 里一个笔误就能让「全部站点」搜索直接抛异常
- **rate_limiter**: 并发下真正遵守 `max_rps`。之前在锁外算等待时间、醒来无条件扣 token，`workers > 1` 时所有请求同时发出；现在在锁内**预留**槽位，按槽位依次唤醒
- **cookies**: 半写坏的 cookie 文件不再继续沿用旧凭据，也不会每次访问都重读一遍坏文件
- **httpx**: 每个请求加总超时。httpx 的 `timeout` 是逐操作超时，服务端慢速滴流字节可以让请求永远不结束；aiohttp / curl_cffi 本来就是总超时
- **search**: 聚合搜索的连接池加每主机上限（8）与 DNS 缓存，59 站点并发不会再对同一主机开出上百个连接

### Test

- 新增/改写 20 例：传输重试、`Retry-After`（秒与 HTTP 日期两种形式、上限）、4xx 不重试、分页上限与重复页、章节层终止已重试过的错误、存储 worker 崩溃不阻塞、坏插件隔离、限流器槽位间距、坏 cookie 文件、httpx 总超时、搜索连接池上限
- 全量 `pytest` 8937 passed

## v1.16.0 (2026-11-07)

### Fix

- **search**: 聚合搜索的 `timeout` 现在是**每个站点的墙钟预算**，超时站点被丢弃而不是拖住全局。之前只把 `aiohttp` 的单请求超时设成 `5s`，一个站点连发十几个请求就能把整次搜索拖到 **116 秒**；现在同样的查询 **5.19 秒**返回（1s 预算 1.10s，2s 预算 2.01s），结果数量随预算单调增加（31 / 56 / 69）。`search_stream` 同样按预算收口，不再等最后一个卡住的站点
- **desktop**: 搜索改成边搜边显示——`search_async` + `search_progress` 轮询，第一屏结果约 **0.17s** 出现，站点陆续返回时列表实时增长，最后一次轮询换成按质量排好序（以及合并）的最终列表

### Docs

- `timeout` 的语义在 API/CLI 文档里统一改成「每站点墙钟预算」，并说明超时站点会被丢弃

### Test

- 新增 2 例：站点超出预算时聚合搜索仍按时返回且保留其它站点结果（`search` 与 `search_stream` 各一）
- 新增 3 例桌面桥接：渐进结果的中间态与最终排序、后端异常回传、空关键字短路

## v1.15.0 (2026-11-07)

### Feature

- **api**: 新增 `GET /api/v1/browse/categories`（分类树，按频道分组）与 `POST /api/v1/browse`（省略 `category_id` 看榜单，给了就取该分类书目）；站点没有浏览能力时返回 `400` 并说明原因
- **android**: 搜索页加「排行榜」「分类」入口，男生/女生切换 + 分类标签横向滑动，点一下直接把书目填进结果列表，可直接加入下载队列或追更

### Fix

- **fanqienovel**: `/api/library/book_list` 不认 `limit`（固定回 10 条），现在在客户端按调用方预算截断

### Test

- 新增 `tests/apps/test_api_browse.py`（5 例）：分类树、榜单默认、频道码映射、无浏览能力站点、非法 limit
- 分类用例补一条「endpoint 忽略 limit 时客户端仍按预算截断」
- Android `testDebugUnitTest` 与 `compileDebugKotlin` 通过

## v1.14.0 (2026-11-07)

### Feature

- **cli**: 新增 `browse` 子命令——不靠关键字直接浏览站点发布的内容：`--categories` 列出分类（按频道分组）、`--category ID|NAME` 取某个分类的书（支持中文名，同名分类按 `--gender` 优先）、缺省看总榜；`--json` 给脚本用，交互选择后可直接进入下载流程
- **docs**: 命令总览、示例、官网「15 个子命令」同步；顺带更新番茄搜索说明——现在是「App 接口直连、免登录、不弹窗」，浏览器辅助搜索只剩兜底

### Test

- 新增 `tests/apps/test_cli_browse.py`（8 例）：分类 JSON、分类名按频道解析、同名分类择频道、总榜默认、无浏览能力站点报错、互斥参数、未知分类名
- CLI e2e 的命令清单与 `--help` 关键字加入 `browse`

## v1.13.0 (2026-11-07)

### Feature

- **fanqienovel**: 分类浏览接通。榜单页里的 `rankCategoryTypeList` 给出男女频共 37 个分类，书目取 `GET /api/library/book_list`（`app_id=2503&category_id&gender&offset&limit&word_count=0&sort=0`，无需签名），书名/作者/封面/更新日期都是明文，不需要再回填详情页
- **desktop**: 搜索页加「分类」入口，男女频切换 + 分类标签，点一下直接列出该分类的书目并进入下载流程；「合并同一本书」标签也接入了 i18n

### Test

- 新增 4 例分类用例：解析 `rankCategoryTypeList`、坏页面返回空、书目映射与必填参数（含 `word_count=0&sort=0`）、非 JSON 容错
- 原先锁定「合并同一本书未接入 i18n」的测试改为正向断言

## v1.12.0 (2026-11-07)

### Feature

- **fanqienovel**: `x-ladon` 改为纯算法生成，不再依赖抓包种子。调度表存的是 `rol3(k) ^ new` 而不是 `new`，改对之后与客户端抓包逐字节一致（1 条已知时间戳 + 3 条同批次，共 4 条全部命中）。注册、搜索、批量取章三处都换成现算

### Docs

- 协议笔记把 ladon 从「结构已还原、调度未对齐」改写为完整算法；同时纠正上一版「流式异或」的错误判断（那是对比方法的伪影）

### Test

- 新增 `tests/plugins/fanqienovel/test_ladon.py`（5 例）：抓包向量逐字节复现、盐/时间戳敏感性、36 字节帧
- `test_register.py` 改为断言动态生成的 ladon，并覆盖环境变量覆盖

## v1.11.0 (2026-11-07)

### Feature

- **desktop**: 搜索页新增「排行榜」入口，一键拉取番茄榜单（服务端渲染页），标题/作者用书籍详情页回填成明文，可直接下载

### Test

- `test_fanqienovel.py` 增加 2 例：榜单页字段映射、坏页面返回空

## v1.10.1 (2026-11-07)

### Feature

- **desktop**: 书搜不到的时候，空状态里多一个「在音乐中搜索」——带着同一个关键词跳到音乐页并自动搜，不用再手输一遍

### Test

- 新增 msgid 已补 `zh_CN` 翻译并清除 fuzzy 标记

## v1.10.0 (2026-11-07)

### Feature

- **desktop**: 搜索页重构为「结果列表 + 详情面板」双栏。点一条结果，右侧显示封面、作者、最新章节与站点，下载/关注/在浏览器打开都收进面板；列表只保留最常用的「下载」，一屏可扫更多结果。窄窗口自动收起面板
- **brand**: 产品显示名改为 **书音 BookTone**，覆盖小说与音乐两条产品线。包名、可执行文件名与发布资产名保持不变（更新器按文件名匹配，改名会断掉自动更新）
- **docs/site**: 文案与标题同步新名字，首屏改为「整站小说与歌单」

### Test

- `test_desktop_app.py` 的快捷方式名断言随品牌更新

## v1.9.0 (2026-11-07)

### Feature

- **desktop**: 更新流程压到零点击。检出更新后后台自动下载并校验 SHA-256；退出应用时自动运行安装器，下次启动即是新版本。更新条仍在，只是「点一下」从必需变成可选
- **android**: 检出更新后自动下载并校验，对话框只剩系统安装器那一次确认（系统不允许静默安装）
- **desktop**: 新增两个偏好，默认开启即可零点击：`auto_download_updates`（后台下载）、`auto_install_on_exit`（退出时装）

### Test

- `test_desktop_update.py` 增加 5 例：检出后自动下载、关闭偏好时不下载、退出时安装、偏好关闭时不装、校验失败拒绝执行并删文件

## v1.8.2 (2026-11-07)

### Feature

- **fanqienovel**: 搜索在首次使用时也会自动注册设备身份（与下载共用同一份缓存），因此不先下载也能直接搜索。此前必须先下载一本书才会有身份，否则回退到需要登录的浏览器路径

## v1.8.1 (2026-11-07)

### Feature

- **fanqienovel**: 搜索不再需要登录或浏览器。显式指定番茄时优先调用官方 App 搜索接口（`novel.snssdk.com`，带自签名），直接返回明文书名/作者/封面；不可用时才回退浏览器辅助搜索。聚合搜索仍跳过番茄，保持快且不弹窗

### Docs

- 站点详情页的搜索说明按新行为改写

### Test

- `test_fanqienovel.py` 增加 1 例：显式搜索优先走 App 接口且不触发浏览器；并为该文件的用例加了默认禁用设备发现的夹具，避免真实网络

## v1.8.0 (2026-11-07)

### Feature

- **fanqienovel**: 正文改为批量获取。目录页取出章节 ID 后，按 20 章一组调用官方批量接口，单本书从「一章一个请求」降到「一本 1+14 个请求」（280 章实测），显著降低风控与耗时。批量失败时自动退回逐章

### Test

- `test_fetcher_appapi.py` 增加 3 例：目录 ID 提取、相邻章节共享一次批量请求、批量请求带 argus/ladon

## v1.7.2 (2026-11-07)

### Fix

- **fanqienovel**: 服务端拒绝当前设备密钥时（密钥轮换或被吊销），自动丢弃旧身份、注册一台新设备并重试本章一次。此前这种情况会直接把章节判为缺失；现在只有重注册也失败才回退

### Test

- `test_fetcher_appapi.py` 增加 2 例：密钥被拒后重注册并成功、重注册失败则只尝试一次不循环

## v1.7.1 (2026-11-07)

### Fix

- **fanqienovel**: 注册时逐个尝试 `x-ladon` 种子而不是随机挑一个。此前若某一枚失效，约一半的自动注册会失败；现在只有全部种子都失效才会放弃

### Test

- `test_register.py` 增加 2 例：首枚种子被拒时自动换下一枚、环境变量覆盖排在最前

## v1.7.0 (2026-11-07)

### Feature

- **fanqienovel**: 锁定章节不再需要任何外部条件。首次使用会自动注册一台虚拟设备并换取内容密钥，之后直接读取全文——不装官方 App、不登录、不手动放文件。实测干净环境首次运行即拿到第 280 章 2220 字正文
- **fanqienovel**: 实现长签 `X-Argus`（SM3 + Simon-128/256 + AES-128-CBC + protobuf bean），与原客户端发出的签名**逐字节一致**，并钉成固定向量测试
- **fanqienovel**: `X-Ladon` 尚未还原算法，改为内置两枚可用种子（`NOVELDL_FANQIE_LADON` 可覆盖）；实测服务端不校验它与时间戳/设备的绑定
- **fanqienovel**: `NOVELDL_FANQIE_AUTO_REGISTER=0` 可关闭自动注册；注册失败静默回退网页模式，不影响免费章节

### Docs

- 协议笔记改写为「已打通」：完整的两步请求、argus 的 bean 字段与算法、ladon 的现状与替换方式
- 站点详情页改为「开箱即用」描述，补高级选项与常见日志

### Test

- 新增 `tests/plugins/fanqienovel/test_argus.py`（4 例）：SM3 标准向量、抓包签名逐字节复现、随机数与时间戳的影响
- 新增 `tests/plugins/fanqienovel/test_register.py`（6 例）：注册与换钥、请求签名形状、拒绝与非 JSON 回复、环境变量覆盖
- `test_fetcher_appapi.py` 增加 2 例：`init` 自动注册、开关关闭时不注册

## v1.6.0 (2026-11-06)

### Feature

- **fanqienovel**: 锁定章节可下载。实现了官方 App 内容接口的请求签名（`X-Gorgon` 8404 变体）、设备身份加载与 AES-128-CBC 解密：配置了可用设备身份后，原先只有 200 字预览的章节可以拿到完整正文（实测第 280 章 3359 字 → 2220 字纯文本）。请求失败、密钥不匹配或章节号不是纯数字时自动回退网页阅读页；没有设备身份时行为与以前完全一致
- **fanqienovel**: 设备身份支持三种来源（`NOVELDL_FANQIE_DEVICE` 环境变量、缓存目录 `fanqienovel/device.json`、本机同类客户端已注册的身份文件），文件只在本机读取，不随请求上传
- **fanqienovel**: App 接口返回的正文标记为 `app-api` 来源，解析器据此跳过网页字体混淆检查与字形替换，不再每章误报缺少 webfont
- **client**: 新增 `handles_locked_chapters` 能力位——抓取器声明能读到锁定章节时，编排阶段不再把它们当作不可读而丢弃。番茄配置设备身份后，实测同一本书的计划章节数从 10 章变成 280 章

### Docs

- 站点详情页新增「锁定章节与设备身份」：文件格式、来源优先级、回退行为、常见日志含义
- `docs/notes/fanqie-client-protocol.md` 改写为实测结论：可用的完整请求形状、8404 签名结构、`version_code` 为何必须压低，以及内容密钥签发仍未打通的完整证据表（含「已知可用设备同样被拒」这个决定性对照）

### Test

- 新增 `tests/plugins/fanqienovel/test_appapi.py`（18 例）：签名固定向量逐字节锁定、解密往返与错误密钥、身份文件解析、来源优先级、与解析器的页面对接
- 新增 `tests/plugins/fanqienovel/test_fetcher_appapi.py`（6 例）：App 接口优先、三种回退路径、设备只发现一次
- `test_parser.py` 增加 2 例：App 页面不触发字体告警、不做字形替换
- `tests/apps/test_embedded_server.py`：chunked 请求改用原始 socket。服务端一见 `Transfer-Encoding: chunked` 就回 501 并断开，`http.client` 有时会在写完终止块前抛 `BrokenPipeError`，导致用例随机失败（本机 3 次里失败 2 次）

## v1.5.3 (2026-11-06)

### Fix

- **fanqienovel**: 锁章预览不再被当成正文——番茄对匿名访问按章加锁，锁定的章节在阅读页只返回约 200 字预览，且 HTML 结构与正常章节完全一致。此前解析器照单全收，会把预览写成章节：整本书静默残缺，进度却显示 100%。现在解析层识别 `isChapterLock`/`needPay`，命中即判失败并进入缺章账本；目录层用同一判定，下载前就跳过，不逐章白抓

### Docs

- 新增 `docs/notes/fanqie-client-protocol.md`：从发布物静态还原的番茄客户端接口地图（主机集合、内容/搜索路径、请求参数模板、设备注册回退链、持久化文件），以及「匿名读全文」卡在哪一环的实测结论
- 故障排查补中英两节：为什么有些番茄章节很短、如何拿到登录态

### Test

- 新增 `tests/plugins/fanqienovel/test_parser.py`（7 例）——此前该站点只有 searcher 测试，解析层零覆盖

## v1.5.2 (2026-11-06)

### Fix

- **updater**: 清单把 OS 写死成 Windows——Linux/macOS 资产会被标成 Windows，同版本双 OS 发布直接建清单失败；现从文件名解析 OS，多 APK 生成 `android-<abi>` 键（单 APK 仍是 `android`），不再互相抢占
- **updater**: 构建清单失败抛的是 `ManifestError` 而非 `ReleaseError`，用户看到 traceback 而不是干净的退出码；已统一包装
- **build**: `--version` 可与 `__version__` 分裂——产物按 `__version__` 构建、发布目录按 `--version` 查找，可能把上一轮的资产挂到新 tag 下。现在启动即比对，并校验 Android `versionName`，发布前还要求工作区干净（`--allow-dirty` 可跳过）
- **build**: 清单里的 URL 在发布路径上从未被校验——现在逐条比对是否指向当前 tag，且 `latest.json` 必须带远端 digest，否则中止
- **build**: 发布上多余资产此前被静默保留且文档声称「精确一致」——现在文档改为如实描述，并在发现多余资产时打印警告（不自动删除）
- **android**: 清单缺 `android` 条目时被判为「无更新」且记录 24h 节流，导致有包也永久漏更；现在返回空结果回退 GitHub API 再确认。同时支持多 ABI 清单键，按设备 ABI 选择
- **cli**: 交互模式下载更新时，若发布方未提供 SHA-256 仍会下载（只有 `--yes` 会拒绝）——现在两条路径一致：无法校验就不下载，引导到发布页手动获取
- **desktop**: 有更新但当前平台没有对应包时，更新条只隐藏按钮、不说明原因；现改为明确文案并高亮「发布说明」

### Docs

- 文档全面对齐代码：CLI 子命令 12→14（补 `batch`、`update`）、`download --strict`、JSON 契约新增 `failed`/`latency_ms`/`http_status`/`update` 八字段、书源字段表补 9 个新 DSL 键并删除英文版「规则不支持节流参数」的错误说法、`configuration` 补 `update_check`、`android-app` 补内置更新与 16 KB 对齐、`security` 补更新链威胁模型、`recipes` 把 `batch` 列为批量下载首选、官网文案与子命令数同步、英文 README 补三节并把链接改到 en 版

### Test

- 新增 `tests/test_build_update_manifest.py`（30 例）、清单签名与 fail-closed 用例、发布端版本/清单/多余资产的用例；Android 222 项、Python 8818 项全绿

## Unreleased

### Feat

- **updater**: 更新清单签名机制（默认关闭）——`latest.json` 可用 Ed25519 签名，客户端在配置了可信公钥后**拒绝**未签名、未知密钥或签名不符的清单。私钥不进仓库（`--keygen` 写到指定路径、权限 0600），启用步骤写在 `AGENTS.md`。默认 `TRUSTED_UPDATE_KEYS` 为空，行为与现在完全一致
- **build**: `release.py` 新增 `--sign-key`，发版时直接产出签名清单；`--check --public-key` 可在维护流程中复核签名

### Test

- 新增 `tests/test_build_update_manifest.py`（30 例：清单构建、目标键映射、URL 必须指向当前 tag、严格 diff、签名往返与篡改检测、CLI 退出码）与客户端签名校验用例；全仓 8815 passed

## v1.5.1 (2026-11-05)

### Feat

- **android**: 更新检查改用 `latest.json` 清单——与桌面/CLI 走同一条链路，一次小请求即可完成检查，不再受 GitHub API 速率限制；清单不可用时回退 API。清单里没有安卓条目时明确判定「该版本不含安卓包」，而不是把版本号当成有包

### Fix

- **android**: 清单解析对 `schema`、`platforms`、`https` URL 与 64 位 SHA-256 做硬校验，任一项不合法就整份作废并回退——避免用一份半坏的清单去下载

## v1.5.0 (2026-11-05)

### Feat

- **updater**: 发布物自带 `latest.json` 更新清单——每个 release 附带一份小文档，列出各平台资产的确切 URL、大小与 SHA-256；客户端优先读它（`releases/latest/download/latest.json`，一次请求、不受 API 速率限制），失败时回退 GitHub API。清单由**实际资产重建**而非构建期状态，缺哪个平台就没有那条，不会指向上一版的文件
- **updater**: 清单使用包形态专属的 key（`windows-x64-installer` / `windows-x64-portable` / `android`），从安装器安装的副本只会更新到安装器，不会被静默换成免安装包
- **build**: 发布前的 APK 必须通过 **16 KB 页对齐**硬门禁——同时检查 zip 条目偏移（仅未压缩的 `lib/**`）与 ELF `PT_LOAD` 的 `p_align`；任何一项不达标即中止发布，不打 tag。Android 15+ 的 16 KB 设备会拒绝加载 4 KB 对齐的 `.so`，此前整条流水线都不会发现
- **build**: 发布前的资产泄漏审计——扫描待上传产物的归档成员（`.git`、`.env`、`id_rsa`、`*.pdb`、`*.map`、路径穿越）与原始字节（`github_pat_`、`BEGIN OPENSSH PRIVATE KEY`、`storePassword=`、`NOVELDL_DEPLOY_PASSWORD` 等），分块读取并保留 96 字节重叠，命中只报标记名、不回显明文；另拒绝零字节、异常大小与未知命名
- **ci**: 供应链契约测试——把 workflow 当规格来测：所有 `uses:` 必须 pin 到 40 位 commit SHA、禁止 `pull_request_target`、`run:` 块内禁止 `${{ }}` 插值、权限最小化、禁止 `gh release upload` / `curl | bash` / force push、checkout 不得持久化凭据、workflow 文件集合固定

### Fix

- **ci**: 23 个 GitHub Action 引用**全部从 tag 改为真实 commit SHA**（经 API 逐个解析并二次核对），8 个 checkout 补上 `persist-credentials: false`——tag 可变，攻击者能重写 tag 让流水线执行任意代码
- **build**: `release.py` 现在同时构建 Inno 安装器——此前只调 `build_release.py`，安装器要另外手动补一轮，v1.4.0 首次发布因此少了 `setup.exe`

### Test

- 新增 `tests/test_check_android_alignment.py`（12 例，含合成 ELF/zip）、`tests/test_audit_release_assets.py`（65 例，含 96 字节跨块重叠与 100 MB 性能上限）、`tests/test_workflow_contract.py`（12 例）、`tests/test_release_script.py` 的清单/审计/对齐门禁用例；清单解析另有 17 例覆盖 schema 拒绝、形态优先级、非 https 与坏摘要

### Docs

- 新增 `docs/notes/fanqie-release-engineering.md`——竞品发布运维仓的完整拆解：Draft-first 发布、更新器清单与渠道别名、资产泄漏审计、16 KB 对齐、静默执行器、issue 自动化，以及每条「值得抄 / 不该抄」的判断

## v1.4.1 (2026-11-01)

### Feat

- **updater**: 三端内置更新检测——桌面、CLI、安卓都会读取 GitHub release 并按平台挑选正确资产（Windows 安装包优先、macOS/Linux zip、安卓单/多 ABI APK）；检查失败一律静默降级，不打断任何正常操作（CLI 命令、桌面启动、手机启动）
- **desktop**: 应用内更新——顶部更新条显示「发现新版本 vX」，点「下载并安装」后下载到用户目录、**强制 SHA-256 校验**、启动安装器并自动退出；「稍后」只在本次会话隐藏；刷新页面不会重复联网（24h 节流 + 结果缓存）
- **cli**: 新命令 `novel-cli update`——`--check` 只检查、`--yes` 下载并校验、`--json` 输出结构化结果、`--no-mirror` 绕过镜像；**只下载不执行**，安装留给用户；新增 `general.update_check = auto|manual|off` 控制是否在命令结束后静默提示（默认 auto，每天最多一次，只写 stderr，绝不污染 `--json` 的 stdout）
- **android**: 应用内更新——启动时按 24h 节流检查，仅在有新版时弹窗；下载后校验 SHA-256，再经 `FileProvider` 拉起系统安装器；未授予「安装未知应用」时引导到系统设置并在返回后自动续装；设置页新增更新区块（当前版本、上次检查时间、自动检查开关、立即检查）

### Fix

- **updater**: 明文 HTTP 下载可执行文件——桌面端更新下载原先接受 `http://`，而该文件随后会被执行，中间人可改写安装包；现仅接受 `https://`，且缺失 SHA-256 的发布一律拒绝自动下载
- **updater**: 并发重复启动安装器——页面禁用按钮挡不住直接调用桥接接口，两个并发调用会各自校验成功后各拉起一个安装器；现改为锁内原子认领，失败路径归还以便重试
- **updater**: 安装前不复算校验——下载时校验过一次，之后磁盘文件被替换/截断仍会被安装（TOCTOU）；桌面与安卓现都在**启动安装器前重新计算 SHA-256**
- **cli**: 更新提醒的节流失效——时间戳在请求之后才写、而进程退出会杀掉后台线程，导致节流文件永不生成，**每条命令都固定多等 2 秒**且 5 条并发命令发 5 次请求；现改为请求前落盘 + 0.75 秒等待预算 + `O_EXCL` 原子认领（首条 0.82s，其后 ~0.07s，24 小时内仅 1 次请求）
- **cli**: `music platforms --json` 这类用默认 dest 的命令会被误判为非 JSON 而触发提醒；Ctrl+C 落在提醒等待期会抛裸 traceback
- **android**: 脏偏好值导致启动崩溃——`checked_at` 被写成垃圾字符串时 `Preferences.get` 的自动转型在调用点抛 `ClassCastException`，会连带打挂整条 settings flow；读取改为运行时类型检查并回退默认值
- **android**: 超长资产名永远下载失败（300 字符超过 255 字节上限）；安装调用链的 `getUriForFile` 异常原先在 `runCatching` 之外会直接崩
- **installer**: 新增 `AppMutex=NovelDownloader-UpdateMutex`，配合桌面进程持有的同名互斥量，让 Inno Setup 的 Restart Manager 能在替换文件前确定性地关闭运行中的实例

### Test

- 桌面 54 项、CLI 43 项、安卓 34 项 JVM 单测，全部离线（注入 opener/OkHttp 假实现），覆盖：非 https 拒绝、digest 缺失拒绝、校验失败删文件、路径穿越、并发下载/安装、节流与 TTL、脏数据回退、跨线程关窗

### Docs

- `settings-reference` 补 `update_check`（中英双语），示例配置同步

## v1.4.0 (2026-11-01)

### Feat

- **rule**: 规则 DSL 表达力对齐并超过同期竞品——`Field.steps` 后处理链（`js` 走 mini_js、`py` 走 11 个白名单纯函数，按 regex → replace → steps 顺序执行且空值跳过；未用上游那种 `selector@js:code` 字符串 DSL，因为 `@` 会截断 XPath 属性谓词 `//div[@class=...]`）、`Field.fallback` 选择器回退链（字符串项继承主字段的 attr/regex/steps，dict 项独立）、`book.chapters` / `chapter` 的 `preprocess` + `container`（先按 display:none 裁诱饵、把 onclick 改写成 href，再重新解析）、`pages.stop_when`（正则命中即停）与 `pages.loop_guard`（重复 URL/正文指纹即停，默认开启且实测 6 种真实分页形状不漏页）、`book.chapters.is_desc`（按新→旧排列的站点：先跨页拼装去重再整体反转一次）、`limits`（每源并发/间隔/重试，`dataclasses.replace` 不可变合并，`concurrency=0` 拒绝以防信号量死锁）、`search.single_result_fallback`（整站精确匹配 302 到详情页时合成一条结果），全部为可选字段且缺省行为不变
- **rule**: ReDoS 静态拒绝——`(a+)+$` 这类形状此前在 28 字符输入上耗时 48 秒、5000 字符等同永久挂起，现于规则解析期拒绝「被无界量词修饰且可匹配空串/单原子重复」的组，覆盖 `stop_when` / `drop` / `id_regex` / `book_id_regex` / `field.regex`（此前字段正则完全绕过校验）与运行期 `regex_replace`；20 个真实模式零误报
- **search**: 查询意图判定——先判断关键词更像书名还是作者（按 ≤4 / 5–9 / ≥10 字分档加权，差距不足 15% 判为证据不足），再决定排序主键；搜作者名时不再被「书名恰好含该字」的书压过去，默认与证据不足时行为与旧版逐分数一致（5000 组随机输入零漂移），`--json` / CLI / API / 桌面共用同一条聚合路径
- **download**: 缺章账本——进度 100% 不再等于章节完整：抓取重试耗尽、空解析、访问受限、存储失败都会写入 `<raw>/chapter.failures.jsonl` 并计入 `DownloadSummary`（`total == succeeded + failed`、`skipped <= succeeded`），账本是**本次运行**的诊断文件（开跑即清空，避免上次的陈旧失败冒充本次缺口，且成功不写任何东西时账本不存在即代表零失败）
- **cli**: `download --strict`——有缺章时拒绝导出并以非零退出；默认模式仍导出但明确告警（前 5 条 + 账本路径），且不再先打绿色 `downloaded.` 再打缺章告警（`on_complete` 携带 `failed`，有缺章时直接显示 `downloaded with N missing chapter(s)`）；`--json` 每本书新增 `failed` 字段，机器消费者可区分「跑完」与「完整」
- **cli**: 新命令 `batch`——按「书名 作者」书单批量搜索下载，未匹配项写回 `logs/not_found_<site>.txt`（保持同格式并以 `#` 结尾，可直接作为下一次 `--file` 或换源 `--from-not-found` 的输入）；书名含空格时用 Tab 显式分隔；`--strict-match` 要求作者也精确；`--out` 同时重定向原始缓存到 `<out>/raw_data`；单本失败不中断整批，缺章计入退出码；`--interval` 可选限速
- **cli**: 进度条与日志的自动仲裁——TTY 下动态进度条、非 TTY（重定向/CI/管道）自动降级为每 10% 一行的纯文本且无 ANSI，进度条初始化失败同样降级；进度条激活期间控制台日志按原 Handler 格式缓冲，结束后分组补发（上限 500 条并报告丢弃数），不再出现「警告只在磁盘上、终端看不见」，异常路径与嵌套也保证恢复
- **sources**: 真实健康探测 `sources list --probe`——并发 HEAD（405/501 退化 GET）取延迟与状态码，`--by-latency` 按延迟排序且不可达排最后，`--probe-timeout` / `--probe-concurrency` 可控；`--check` 保持纯离线结构校验（此前曾被改成联网，现已恢复契约，退出码语义不变）
- **processors**: cleaner 新增 PUA/格式字符水印清除（按 `unicodedata.category` 删 `Cf`/`Co`，保留换行、制表符、组合字符与全角空格）、正文开头重复章节名剔除（收缩到空白/冒号/文末边界，短标题另加保护）、空标签清理（默认关闭：默认链路正文是纯文本，开启会误删字面量）、章节名规范化（默认关闭）
- **config**: 处理器未知选项不再静默忽略——拼错的键（如 `remove_formt_chars`）会产生可操作告警并给出最接近的已知项，`general.strict_config = true` 时直接报错；`ConfigError` 现在会穿透 `load_config` 的兼容性兜底，否则严格模式等于没开
- **updater**: 更新检查补全平台资产解析（Windows 安装包优先、桌面 zip、Android 单/多 APK 两种命名）、镜像前缀回退（自动补尾斜杠、已是完整前缀不重复拼接）、SHA256 流式校验；仍只提示不自动安装

### Fix

- **exporters**: 原子写「失败降级为直接写」会把旧成品截断成半个——三处导出全部改为**失败即抛错、旧成品保留**（宁可失败不可损坏），并修掉 Windows 上 `fdopen` 失败时 fd 未关闭导致临时文件无法删除的泄漏；HTML 导出改为 staging 目录构建后整体换入，重导出不再残留上次多出的章节/媒体文件；`include_picture` 在 HTML 导出中被忽略的问题已修
- **exporters**: 长文件名/长路径在 Windows 上直接让整本导出失败——超长章节 id（400+ 字符）触发 `WinError 3`、400 字书名使 txt/html/epub 全部失败、截断后只差尾部的长标题互相覆盖；现按真实输出目录深度动态计算预算并追加 `~<hash>` 区分后缀（保留扩展名），EPUB 空卷不再输出空的 `<ol>`
- **download**: 目录里重复出现的 `chapterId` 会让同一章被抓两次、`total` 虚增、一个缺口报两遍——章节计划现在按首次出现去重
- **ledger**: 账本跨运行累积陈旧失败（第一轮缺 5 章、第二轮全成功后 `read_all()` 仍报 5 章）——改为 per-run；同时修掉 `attempts` 一个坏字段吞掉整条缺章记录、10 万层嵌套 JSON 让 `read_all` 直接抛、两个账本实例写同一文件时行交错
- **batch**: 三个会丢数据的缺陷——第 3 本下载失败会中断第 4–10 本（现逐本隔离）、Ctrl+C 会丢掉已匹配但尚未写盘的未找到结果（现匹配完成即写盘）、`--not-found` 指向输入书单时会把它截断（现拒绝并提示换路径）；`--not-found` 目标若不是本命令产出的文件也会被拒，需显式 `--force`
- **cli**: 进度仲裁期间 `WARNING`/`INFO` 在终端消失——旧测试甚至把这个回归当成契约锁死；现改为缓冲后在结束后按原格式补发，`ERROR` 不入缓冲以免重复，`--json` 的 stdout 不受进度输出污染
- **sources**: 探测错误信息泄漏凭据——`Authorization: Basic <base64>` 只被吃掉了 scheme 单词、`Cookie` 只脱敏了第一对键值；现已修正并调整脱敏顺序（先 Cookie 后 Authorization，否则标记会被二次吞掉），会话初始化失败时也不再泄漏
- **updater**: Windows 保留设备名会让下载挂死——目标名为 `con` 时打开 CON 设备阻塞 15 秒；`NUL`/`COM1` 报权限或找不到路径；镜像无尾斜杠会拼成 `ghproxy.comhttps://...`；探测误用下载用的 30 秒超时导致多镜像最坏卡 90 秒；桌面端 5 个返回分支里有 4 个缺少文档承诺的契约键
- **cleaner**: 正文只有标题时会被清空、长标题后跟句读会削掉正常首句——标题剔除现在只在空白/冒号/文末命中，且删除后为空则保留原文；病理输入 `<p` 序列的 O(n²) 回溯（160KB 耗时 28 秒）已限定属性段长度

### Test

- 新增 `tests/plugins/rule/test_rule_hardening.py`（129 例）、`test_extended_features.py`、`test_rule_docs.py`（文档字段与 `model.py` 真实集合逐字段比对，防止文档漂移）、`test_download_failures_adversarial.py`（并发失败归属、ContextVar 任务隔离、fail→success 两轮序列）、`test_site_probe.py`、`test_cli_batch.py`（64 例）、`test_cli_ui_progress.py`、`test_updater.py`，以及各导出器的原子写/长路径/目录换入/离线可用性断言；全仓 7,900+ 项测试在此次改动后全绿

### Docs

- 新增 `docs/guide/rule-dsl.md`（字段参考 + 新能力示例 + mini_js 真实能力边界 + 三层转义说明）与两份可执行示例规则，并修正 `book-sources.md` 中模板变量可用位置、`priority` 实际语义、JSON 数组展开规则等 15 处与代码不符的表述
- 新增 `docs/notes/so-novel-audit.md`——竞品 so-novel v1.12.0 的完整代码审计：架构、规则 DSL、并发模型、清洗管线、导出实现，以及「静默缺章」「每线程 sleep 当限流」「共享可变配置」「设备指纹上报」等反面教材与取舍理由
- `processors-reference` 补齐四个新增清洗开关（中英双语），`cli-examples` 修正 `sources list --check` 退出码语义，`book-sources` 补充 `--probe` 用法

## v1.3.0 (2026-11-01)

### Feat

- **search**: 同书多源合并正式接线四端——`search` 新增 `group=True`（CLI `--group`、API `group` 参数、桌面「合并同书」开关）：跨站同书结果折叠为每书一条，主条目即组内最优来源，附 `sources` 胶囊（最优置前去重，桌面最优高亮、超 6 折叠 +N）；limit 按书本数计；缓存存扁平快照、命中后现分组（同关键词换视图不重搜）；默认路径响应形状不变（Android 兼容锁定测试）——v1.1.0 承诺的功能至此兑现
- **doctor**: 深度诊断——新增依赖、缓存目录、站点插件抽样、网络出口探测（`--online` 触发、非致命）、日志目录可写性与磁盘剩余空间检查，支持结构化 `--json` 输出；`storage` 子命令按 raw/cache/downloads/logs 分桶统计占用、列出最大子目录、报告可回收总量与磁盘剩余空间
- **embedded**: 独立引擎环回服务全面对齐 FastAPI 的 REST 契约——同款 422（带 errors 列表）与 `{"detail": ...}` 错误体、405 附 `Allow` 头、409/413/501/503/504 语义、HEAD 按无体 GET 响应、keep-alive 请求体排干、并发信号量与 1MB 请求体上限（与 API 中间件共享）；端口绑定失败显式报错而非静默漂移到 Kotlin 端无从得知的端口，停止服务保留引擎事件循环便于重启复用（凭据 broker 状态存活）；嵌入式服务器测试 8→42
- **website**: 官网无障碍与 SEO——修复九组未达 WCAG AA 的对比度（新增 `--accent-ink` 令牌、加深 text-3、大字号与按钮加深强调色，焦点环加背景晕在强调色表面仍可见），选项卡补全 tablist/tab/tabpanel ARIA 与方向键漫游、装饰性 SVG aria-hidden、Esc 关闭移动端菜单并归还焦点；新增 JSON-LD SoftwareApplication、canonical、中英 hreflang、og:image 等元数据；快速上手新增独立引擎第五页签与两条 FAQ，页签记忆跨刷新/切语言、GitHub API 缓存 30 分钟 TTL、`?lang=` 深链直达、主题切换同步 theme-color
- **i18n**: 新增 27 条 zh_CN 翻译覆盖 doctor/storage 与新增界面文案，前端桥接目录 66 键全覆盖并由测试锁定；修复 `update_pot` 再生成不可复现——模板未声明 POT-Creation-Date 头时 Babel 会注入当前时间导致每次重写整个文件，现仅在头部实际声明且可解析时复用该日期
- **android**: 可选「独立引擎」模式——Chaquopy 内嵌 Python 3.12 运行时，84 下载站点 / 59 搜索源的完整下载服务层直接跑在手机本地，App 经环回 REST API 访问，无需电脑；关闭开关仍为遥控电脑端 novel-api 的原有模式（APK 约 68MB；独立模式下音乐功能暂为空态）

### Fix

- **sessions**: IP 直连站点的登录 cookie 此前被 aiohttp 默认 CookieJar 端到端静默丢弃（IP 源的 Set-Cookie 被拒收、已保存的 cookie 也不会发往 IP 主机），现以 unsafe=True 运行 CookieJar——IP 站点登录生效、跨域隔离保留，保存/加载往返也不再意外放宽作用域；三后端重定向上限由 10/21/31 漂移统一为 10 并抛统一的 TooManyRedirectsError（刻意不做可重试）；缺失后端库时报出可用后端并保留原因链（不做会丢浏览器指纹的静默兜底）、损坏 gzip 三后端统一映射为 ConnectionError、嵌套 async-with 不再拆掉外层会话的连接池；新增 47 条对拍本地真实服务器的测试
- **core**: 取消成为终态——已取消的任务不再被流水线复活：导出入队拒绝取消任务、下载后的取消检查使 CANCELLED 不会被 PROCESSING/EXPORTING 覆盖、worker 三路分支不再导出失败或已取消的书；线程侧 UI 回调（导出/处理/登录）在 CANCELLED 后拒绝翻转状态，关闭工作线程与事件循环取消之间的竞态窗口；登录中途取消会释放凭据请求，不再在 broker 中留下僵尸登录表单；完成任务登记由 O(n) 全表身份扫描改为 O(1) id 集合去重
- **desktop**: 深度审计修复 11 项缺陷（52 条回归测试）——`call()` 重抛把每个异步 DOM 处理器变成未处理 rejection；Ctrl+F/D/1-5 在输入框打字时也触发（输入守卫 + Enter 的 IME 组合输入检查）；多显示器负坐标窗口原点被钳制到主显示器，现按 Win32 虚拟屏边界恢复；音乐结果计数器跨下载线程竞态可能超出总数；静默吞掉的桥接错误（添加任务/音乐下载失败、载荷错误呈空态）现在浮出；音乐下载按钮双击竞态与偏好设置加固（非数字时间戳、主题校验）；28 个参数化 XSS 注入点守卫覆盖 esc/highlight，Node 冒烟测试以 `<img onerror>` 载荷验证
- **cli**: 修复多书选择从未可用——`select_books` 宣称支持 `1,3,5` 多选，但受限 choice 提示拒绝逗号输入，多选分支对真实用户是死代码；现改为读取自由格式输入并逐段 isdecimal 校验（`'²'.isdigit()` 为 True 但 `int()` 失败，isdigit 判断全部替换）
- **search**: `sites=[]` 现在搜索空集而非被注册层当作全站（对齐跨层契约并写入文档）；时区 aware/naive 混用使整个聚合排序崩溃（分组走同一路径）——aware 时间统一折算到本地 naive 时间线再相减；重复站点选择去重搜索器类并把缓存键折叠成集合（`sites=['a','a']` 只搜一次）；缓存键补上超时维度——不同预算会不同地丢弃慢站点，不得共享条目；非列表搜索器载荷隔离为单站点失败并发出真实 SearchEvent 而非崩溃聚合
- **api**: music 错误详情现在只携带异常类名——原始文本此前直接泄漏给客户端（503/500 处理器本就遵循类名契约），完整信息保留在服务端日志；`sites=[]` 在 JSON 与 SSE 两路由一致短路；音乐任务进入终态后冻结进度快照，超时停止的 worker 不再改动客户端正在读取的快照；token 在 create_app 与 api_main 两侧都做 strip——带空白 padding 的 `NOVEL_API_TOKEN` 此前永远匹配不上请求侧已 strip 的值，所有调用被永久锁死；SDK 的 music_search 补显式 offset（默认 0）对齐服务端 schema
- **exporters**: 手写元数据中的显式 null（`'title': null`）使三个导出器全部崩溃——`.get()` 默认值仅在键缺失时生效，所有 title/content/extra 访问改为 null 安全；EPUB `add_volume` 此前绕过章节准备，嵌套章节 `a/b` 与 `a_b` 净化后同名相互覆盖——分卷章节现在走与顶层相同的 id/文件名去重（nav/ncx/manifest/spine 一并引用新名，分卷简介与封面也登记 id）；HTML 导出新增书级已用文件名集合与递归碰撞后缀
- **rule**: 分页模板缺 `{page}` 或渲染出已见 URL 时，同一页此前最多被重复抓取 50 次、正文被拼接 51 次——页 URL 现以首页种子做去重；模板渲染由顺序 str.replace 改为单遍替换，堵住恶意页面以 `chapter_id='{page}'` 级联注入后续抓取 URL 的攻击面；规则 JSON 严格校验——重复键（静默 last-wins）、拼错的结构键（fomrat/selectr/prioroty）、未知字段名（静默忽略）与携带 CRLF 的头值现在一律拒绝或清洗，负向正则组在加载与求值两端报错而非裸 IndexError，XPath 项选择器返回裸字符串节点不再崩溃
- **music**: 下载器加固——206 续传响应现在校验 Content-Range 起点：起点不符直接报错而非拼接可疑字节、served=0 丢弃分片整段重写，并移除 `Content-Range: bytes 0-*/` 下损坏文件直通的旧兜底；供应商提供的扩展名截取首个字母数字段、`[id]` 兜底文件名过 sanitize()——两者此前均可逃逸输出目录；同一批次内重复曲目对同一目标的并发写（自 skip-existing 到标签/歌词落盘）由按目标引用计数锁覆盖；on_tick 回调与其节流状态串行化，兑现「不刷爆 UI」的文档承诺
- **music**: 提供方审计——health_check 的 KeyError→未知平台翻译现在仅包裹 create()，平台自身 search 抛出的 KeyError 按原样报告不再误判；musicdl 容忍不可解析的时长、拒绝非字符串下载 URL、`track.extra=None` 不再使解析/歌词崩溃；weapi 凭据 cookie 文件（MUSIC_U）先以 0o600 落盘再原子替换，song_detail 补齐 301 登录过期守卫（与 user_playlists/song_urls 一致）
- **translator**: youdao 空白文本短路现在先 strip，对齐 Google/Edge 语义（纯空白标题此前仍会发往远程服务）；youdao `_ensure_keys` 与 edge `_get_token` 以双重检查锁合并并发凭据获取（锁内复查，`force=True` 仍总是重新获取），处理器跑在工作线程上不阻塞事件循环
- **infra**: 书源 URL 解析补五处归一化——粘贴携带的 BOM/空白、大写 scheme、默认端口、userinfo 与协议相对 `//host` 此前均无法解析，现对齐注册表的裸主机键（非默认端口保留）；state.json 为合法 JSON 但非字典时整个应用在语言解析处启动崩溃——损坏文件隔离为 `<name>.corrupt` 保留字节、保存原子化（pid 后缀临时文件 + os.replace，失败清理）；章缓存 refetch 标记仅在批量提交后发布——写失败此前会让缓存声称章节存在而数据库里没有，静默丢失内容；截断的 .mo 不再在每次翻译调用抛 struct.error（警告一次后回退）；cookie 解析把 \x0b/\x0c 计入分隔符、日志行剥离等级名等小修一并落地
- **android**: `Api.configure()` 无条件重建 @Volatile Retrofit 单例——远程↔独立引擎切换此前直到进程重启仍在请求旧主机；configure 经 toHttpUrlOrNull 校验、畸形 URL 回退默认值并返回可用性供设置页使用；Chaquopy 引导（Python.start + 环回服务器）整体移入命名后台线程，消除主线程 ANR 风险；SettingsStore 读取损坏时回退空偏好，独立模式开关即时重配 Api；测试 133→154

### Test

- **perf**: 为优化热点加计数式回归锁（审计测量确认无需改源码）——quality 排序保证每条目恰评分一次（N=300 即 300 次调用，比较器式回归会退化 O(n log n) 立即失败）、grouping 同保证且每条目恰归入一组、normalize_text LRU 命中率与 EPUB 500 章导出（zip 完整性、≥501 条目、<10s，实测留有约 40–70 倍余量）一并锁定；实测记录：quality 排序 3.7–5.0 ms/500 条（较优化前快 10.6 倍）、分组 4.6 ms、EPUB 构建 62 ms / 峰值 1.1 MiB、缓存命中 0.62 µs

### Build

- **ci**: CI 工作流此前因本地 exclude 一直未入库（README 徽章长期指向空仓库），本次提交 build/ci/release 三份工作流并首次真实运行；首跑修复 Windows 开发机上不可见的失败——win32 专属属性（winsound 提示音、subprocess.CREATE_NEW_PROCESS_GROUP）移入 sys.platform 字面量分支使非 Windows 的 mypy 整体跳过，CI 安装改为 `.[ci,api,music]`（测试导入 uvicorn/websocket，仅装 [ci] 时矩阵连收集都失败）
- **packaging**: 构建产物可信化——build_release 汇总表在产物列表为空（全部失败的运行）时崩溃且打不出失败报告；归档先经 testzip() CRC 校验再计算哈希，截断的 zip 从此不可能携带「有效」校验和，损坏孤儿产物删除，EOCD 尾损坏引发的 zlib.error/OSError 转为统一干净的 RuntimeError；build_desktop 安装器编译失败退出码 5 而非静默成功、_app_version 直读源码；spec 共享排除补 apps.embedded（Android 经 Chaquopy 加载，PyInstaller 永不打包）并从控制台包剔除桌面 app；docker-compose healthcheck 与镜像同带 auth token，Chaquopy 构建钉住 Python 3.12
- **release**: 一站式发版脚本 `scripts/release.py`——从 CHANGELOG.md 提取版本段作为 release notes，调 `build_release.py` 构建 cli/api/desktop 三端，`gradlew assembleRelease` 产出 Android 正式 APK（签名 keystore 缺失则警告跳过），合并 APK/安装器校验和进 `SHA256SUMS.txt`，创建 `v<version>` tag 并推送（PAT 取自 `GITHUB_TOKEN` 环境变量或 `git credential fill`，绝不硬编码、绝不打印），经 REST 创建 GitHub Release 并上传全部资产；全程幂等（已有 tag/release/资产按 SHA-256 摘要跳过或替换，`--skip-build` 支持断点续跑），`--dry-run` 只打印计划不执行任何命令
- **ci**: 新增 `.github/workflows/release.yml`——push tag `v*` 触发，在 Windows runner 上用 `GITHUB_TOKEN` 自动完成构建与发版；Android APK 因签名密钥不入库，仍需本地跑同一脚本补传（幂等）

### Docs

- **docs**: 全部文档对照代码交叉核对（68 条链接检查、每处修复抽查 3 个代码事实）——api.md 补记此前未成文的限流（`--rate-limit` 120 / `--rate-limit-window` 60，按客户端 IP 滑动窗口、429 + Retry-After、健康检查豁免、预认证计数）并把 429 加入错误表；cookie 与缓存位置改写为真实的 app-data cache/ 布局（原文误作 `./novel_cache`）、字体缓存目录名修正为 fixed_font_maps；README/index 站点数更新为真实的 59 搜索 / 84 下载；settings-reference 补 locale_style/user_agent/headers/split_mode/precision 与 CRITICAL 日志级；cli-examples 修正 clean 的 --stage 取值（raw/cleaner）并为 download/export 补 `--json` 与 `--format html`；官网 follow add 示例改为位置参数、追更五信号列表补全完成状态

## v1.2.0 (2026-10-31)

### Feat

- **desktop**: 应用内更新检查——每 24 小时向 GitHub Releases 查询一次，新版本以可点击 toast 提示直达下载页；离线/限流静默降级
- **desktop**: 成熟度补全——运行日志落盘（app 目录 logs/ 下 1MB×3 轮转）、单实例锁（二次启动弹原生提示并退出，防双实例争抢下载队列）
- **packaging**: Inno Setup 安装器脚本（`packaging/noveldl_installer.iss`，按用户安装免 UAC、自动建桌面+开始菜单图标、卸载保留用户数据），`build_desktop.py` 检测到 ISCC 时自动产出 setup exe，未安装则优雅跳过
- **website**: 官网新增「下载」区（Windows 桌面 / PyPI / Android 工件三卡直达）
- **desktop**: 新增「创建桌面快捷方式」——侧边栏一键在桌面和开始菜单生成 .lnk（PowerShell COM），打包版首次启动自动提示；补齐萌新从 zip 解压到桌面图标启动的最后一公里
- **android**: UI 审计——退后台暂停 2 秒轮询（此前永久烧电耗流量；取舍：任务完成通知改为回到前台时浮现，完整后台通知需前台服务/WorkManager）；`ACTION_DOWNLOAD_COMPLETE` 接收器改 NOT_EXPORTED 防伪造广播消费下载 id；登录对话框输入 4096 截断+提交防重+提交中禁关闭；导出文件行 48dp 触控目标；文件大小补 GB 档；分区标题走复数安全资源
- **sources**: `check`/`add` 改用严格规则 JSON 解析——重复键从静默 last-wins 变为早期本地化报错，check 读取加 BOM 容忍（可加载的带 BOM 文件此前必然 check 失败）
- **android**: 任务页新增登录凭据请求 UI——轮询 `/api/v1/login/requests`，需要登录的站点在任务页顶部显示凭据卡片，点开对话框按字段输入（password/cookie 掩码、必填校验、prefill 回填），提交后任务自动继续；含 MockWebServer 契约测试
- **android**: 完成发布签名链路（`android/keystore.properties` + `release.jks`，git-ignored），`assembleRelease` 产出已验签的正式 APK
- **desktop**: 桌面端重写为 pywebview + 单文件 HTML/CSS 前端（Edge WebView2 渲染）——完整 CSS 动效（页面过渡、卡片悬浮、渐变进度条、toast、模态）、暖纸色明暗双主题、SVG 图标、快捷键、平台健康检查面板、音乐实时进度；窗口几何持久化改在 `closing` 事件读取
- **desktop**: `ApiBridge` 暴露搜索/任务/历史/追更/音乐/偏好设置共 20+ 方法给前端，音乐下载与健康检查在后台线程运行并通过轮询状态上报
- **packaging**: 桌面打包迁移到 pywebview（隐藏导入 Windows 后端 + WebView2Loader 二进制），全部产物排除 tkinter；构建验证含 8 秒启动冒烟

### Fix

- **desktop**: 修复 `sys.argv[0]` 为相对路径时 pywebview 内联 HTML 基准 URI 解析崩溃（chdir 前先绝对化）
- **desktop**: 修复窗口关闭时几何保存读 `window.width` 抛 `TypeError`（后端已拆除）
- **browser/login**: 修正指向不存在的 `desktop` extra 的安装提示（pywebview 已是基础依赖）
- **docs**: 清理 api.md / architecture / index / installation / 站点文档中的 Web UI、Tkinter 陈旧表述

### Refactor

- **desktop**: 删除旧 UI 遗留死代码——formatting.py 裁剪至 4 个在用函数（958→133 行）、preferences.py 整体移除、notifications.py 仅保留提示音、window_state.py 去掉 Tk 几何串解析；同步剪枝 107 个失效测试与 15 条孤儿翻译

### Breaking

- **desktop**: 移除 CustomTkinter 实现（`ui.py`/`theme.py`），桌面依赖由 `customtkinter` 换为 `pywebview`；功能面不变

## v1.1.0 (2026-10-24)

平台整合、搜索扩展与全面加固版本。

### Feat

- **android**: 新增原生 Kotlin/Compose 客户端（搜索/任务/历史/音乐/设置），通过本地 REST API 工作
- **desktop**: 新增原生 Tkinter 桌面客户端（搜索、任务进度、历史、音乐页签）
- **music**: 网易云下载引擎并入主项目（WEAPI 客户端、断点续传、标签/歌词/封面、25 个平台）
- **cli**: 新增 `music`（platforms/search/download/export-m3u/health）、`follow`（追更）、`doctor`、`storage`、`completion` 子命令
- **api**: 本地 REST API 增强 —— SSE 流式搜索、任务事件流、分页、音乐异步任务、追更端点、Webhook、Python SDK
- **search**: 同书多源合并（按书名聚合、来源站点胶囊、最优来源标记）、最近搜索、关键词高亮、站点过滤
- **search**: 新增 16 个经过真实验证的搜索站点（飞卢、17K、刺猬猫、SF轻小说、小説家になろう 等，搜索站点 41→57）
- **cache**: 搜索结果 TTL/LRU 缓存
- **i18n**: 全部新增界面文案的中英翻译

### Perf

- **search**: 排序与分组优化 8–48 倍（日期解析、关键词覆盖度、同书聚类），输出经对拍与 fuzz 证明等价
- **translate**: 翻译处理器增加 LRU 缓存、批内去重与并发合并，失败不缓存

### Fix

- **sessions**: 三后端行为统一（重定向、超时/连接异常类型、encoding、cookie 冲突），cookie 原子持久化与作用域保留
- **sessions**: 非法 charset/BOM/压缩响应解码统一，JSON 错误可读
- **export**: EPUB 原子写入（失败不再截断旧归档）、章节文件名净化、重复章节去重、同名分卷不覆盖
- **processors**: cleaner/corrector/zh_convert/translator 的 None、非法配置、单行异常中断等缺陷
- **cli**: 退出码统一为 0/1/2 契约，非 TTY 不再卡交互，rich markup 转义
- **api**: token 常量时间比较、请求体上限、端点超时、统一错误体、路径穿越防护
- **core**: 任务管理器传输类失败重试、进程/导出阶段可取消、worker 崩溃不卡队列
- **plugins**: 笔趣阁系公共解析器 10+ 处崩溃/截断修复；规则书源校验前置
- **sites**: 修复 westnovel_sub 搜索后端失效、czbooks 调试输出、alicesw/esjzone 元数据缺失

### Test

- 测试套件从 4200 增至 6300+（规则引擎、处理器、导出器、任务管理器、CLI、API、会话、libs、schemas、Android JVM 单测）

### Build

- **release**: PyInstaller 打包（novel-cli / novel-api / NovelDownloader）、发布脚本与 SHA256 清单
- **docker**: API 容器化方案（默认强制 token）
- **docs**: 文档全面重写为中英双语（架构、故障排查、配方、安全审计），站点文档按代码事实生成

### Breaking

- **cli**: 错误路径退出码由 0 改为非零（1 运行失败 / 2 用法错误）
- **web**: 移除 NiceGUI Web UI（含桌面壳与 PWA），产品面收敛为 CLI + API + 桌面/Android 客户端

## v1.0.0 (2026-10-16)

首个发布版本。

### Feat

- **rules**: 规则书源子系统 —— 用一份 JSON 描述站点，搜索、详情、目录、正文、分页与链接解析全部由规则驱动，新增站点无需编写 Python 代码
- **rules**: 内置 `biquge5r` 规则源，并提供覆盖全部字段的规则模板
- **rules**: `book` / `chapter` 段可单独设置请求头，且请求头值支持 `{base_url}` / `{book_id}` / `{chapter_id}` 模板变量
- **cli**: 新增 `sources list` / `sources check` 用于查看与校验规则书源

### Fix

- **rules**: 修复分页页被重复收集导致分页章节正文重复的问题
- **rules**: 空页或退化页面统一报 `RuleError`，不再泄漏底层解析异常；正文解析会跳过这类页面
- **cli**: `prompt_choice` 捕获 EOF，管道或脚本运行时不再崩溃

### Docs

- 补充规则书源文档、示例规则模板与导航入口
