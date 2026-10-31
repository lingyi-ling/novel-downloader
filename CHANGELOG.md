## Unreleased

### Feat

- **android**: 可选「独立引擎」模式——Chaquopy 内嵌 Python 3.12 运行时，84 下载站点 / 59 搜索源的完整下载服务层直接跑在手机本地，App 经环回 REST API 访问，无需电脑；关闭开关仍为遥控电脑端 novel-api 的原有模式（APK 约 68MB；独立模式下音乐功能暂为空态）

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
