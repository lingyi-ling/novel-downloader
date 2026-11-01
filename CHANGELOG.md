## Unreleased

### Feat

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
