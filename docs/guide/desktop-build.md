# 桌面应用构建（Windows / PyInstaller）

本文说明如何把 `novel-desktop`（NiceGUI + pywebview 原生窗口）打包成可分发的 Windows 桌面应用。

构建使用 **onedir** 模式：NiceGUI 的静态资源较大，pywebview 还要在运行时加载 `.NET` / WebView2 互操作 DLL。onedir 启动更快，也能直接看到缺失的依赖，比 onefile 更稳。

相关文件：

* `packaging/novel_desktop.spec` —— PyInstaller 配置（收集项、排除项、图标、窗口模式）
* `packaging/desktop_entry.py` —— 冻结入口，负责 `multiprocessing.freeze_support()` 与崩溃日志
* `scripts/build_desktop.py` —— 一键构建脚本

---

## 1. 环境准备

建议始终在仓库内的虚拟环境中构建，避免把全局环境的无关包收进产物：

```bash
python -m venv .venv
.venv\Scripts\activate
```

安装桌面运行依赖与 PyInstaller：

```bash
pip install -e ".[desktop]"
pip install pyinstaller
```

> `.[desktop]` 会安装 `nicegui[native]`（包含 pywebview / pythonnet）。
> 如果还需要 WebUI 中的音乐下载、图片处理等功能，可改用 `pip install -e ".[all]"`，或在构建前按需安装对应可选依赖。

音乐页中基于 vendored musicdl 的平台还依赖 `pathvalidate`（未写入项目 extras）。
如需这些平台可用，请在构建前安装：

```bash
pip install pathvalidate
```

---

## 2. 构建

### 2.1 一键构建（推荐）

```bash
.venv\Scripts\python.exe scripts\build_desktop.py
```

脚本会：

1. 检查 PyInstaller / nicegui / webview 是否可用，缺失时打印安装命令；
2. 清理 `build/novel_desktop` 旧工作目录；
3. 调用 PyInstaller 构建 spec；
4. 检查产物和关键资源（图标、`.mo` 翻译、内置书源 JSON、WebView2 DLL、NiceGUI 静态资源）；
5. 打印可执行文件路径、单文件大小和整包大小。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--clean-dist` | 同时删除上一次的 `dist/NovelDownloader` |
| `--no-clean` | 保留 `build/novel_desktop`（增量式排查时使用） |

### 2.2 直接调用 PyInstaller

```bash
.venv\Scripts\python.exe -m PyInstaller packaging\novel_desktop.spec ^
    --noconfirm --clean ^
    --distpath dist --workpath build
```

PowerShell 中把续行符 `^` 换成反引号 `` ` `` 即可。

### 2.3 spec 的关键收集项

| 收集项 | 原因 |
| --- | --- |
| `collect_all("nicegui")` | NiceGUI 的 `static/`（Vue、ESM 库）、`templates/`、脚本与 native 桥接；同时过滤掉 `nicegui.testing` |
| `collect_all("webview")` | pywebview 各平台后端；Windows 下还包含 `webview/lib` 里的 WebView2 / WinForms 互操作 DLL |
| `pythonnet/_pyinstaller`（hookspath） | 收集 `Python.Runtime.dll`，pywebview 的 WinForms 后端需要 |
| `collect_data_files("novel_downloader")` | `locales` 的 `.mo/.po`、`resources`（图标/CSS/JS/JSON/示例配置）、`plugins/rule/builtin/*.json`、`py.typed` |
| 遍历 `plugins/sites/**/*.py` | 站点插件由 `importlib` 动态加载，且子目录没有 `__init__.py`，`collect_submodules` 看不到它们 |
| `collect_submodules("novel_downloader.plugins.rule")` | 书源规则模块在 registry 中延迟导入 |
| `collect_submodules("novel_downloader.plugins.processors")` | 处理器同样按名称动态解析 |
| 遍历 vendored `musicdl` 源码树 | 音乐平台按类名动态 `import_module`，需逐个打包 |
| 显式 uvicorn 子模块 | uvicorn 的 loop / protocol / lifespan 实现是运行时选择的 |

排除项包括 `pytest`、`mypy`、`ruff`、`playwright`、`mkdocs`、`babel`、`tkinter`、`customtkinter`、科学计算/Notebook 相关包，以及仅作参考的 `plugins/archived`。

---

## 3. 产物

构建成功后：

```text
dist/
└── NovelDownloader/
    ├── NovelDownloader.exe      # 双击启动
    └── _internal/               # Python 运行时、依赖、静态资源
```

参考体积（Windows x64，Python 3.12）：

* `NovelDownloader.exe`：约 20 MiB
* 整个 `NovelDownloader/` 目录：约 126 MiB

分发时请打包**整个目录**（或压缩成 zip），不要只复制 exe。

`build/` 与 `dist/` 已在 `.gitignore` 中忽略，不要提交构建产物。

> 注意：`.gitignore` 中还有一条全局 `*.spec` 规则，因此
> `packaging/novel_desktop.spec` 默认也不会被 Git 跟踪。若需要纳入版本控制，
> 请使用 `git add -f packaging/novel_desktop.spec`，或由维护者调整 `.gitignore`。

---

## 4. 首次启动

直接运行：

```bash
dist\NovelDownloader\NovelDownloader.exe
```

首次启动会：

* 在 `%LOCALAPPDATA%\novel_downloader\` 创建应用目录；
* 从内置模板复制一份 `settings.toml`；
* 在该目录下创建 `downloads/`、`logs/` 等运行目录，并把工作目录固定到这里。

因此程序**可以从任意位置启动**，配置文件与下载结果不受 exe 所在目录影响。

日志：`%LOCALAPPDATA%\novel_downloader\logs\novel_downloader.log`
启动崩溃：`%LOCALAPPDATA%\novel_downloader\logs\desktop-crash.log`（窗口模式无控制台，崩溃堆栈写在这里）

自定义数据目录：

```bash
NovelDownloader.exe --home D:\NovelData
```

也支持 `--lang en`、`--browser`、`--window-size 1280x800`、`--check` 等与 `novel-desktop` 相同的参数。
其中 `--check` 在窗口模式下没有可见输出，主要用于排查环境；正常使用不必加。

运行依赖 **Microsoft Edge WebView2 Runtime**（Windows 11 自带；Windows 10 可能没有）。
缺少时程序会打印提示并回退到默认浏览器模式；如需强制使用浏览器模式，可设置环境变量 `NOVEL_NO_NATIVE=1`。

---

## 5. 已知限制

* **体积**：基础包约 126 MiB，主要来自 NiceGUI 静态资源、numpy、Pillow、lxml、WebView2 互操作 DLL。若确定不需要图片/音乐功能，可进一步裁剪 spec 的收集项，但可能影响部分站点。
* **杀毒软件误报**：PyInstaller 生成的未签名 exe 偶发被拦截。正式分发建议做代码签名，并在发布页说明。
* **升级方式**：用新版本整个替换 `NovelDownloader/` 目录即可；用户数据在 `%LOCALAPPDATA%\novel_downloader`，不会被覆盖。
* **浏览器辅助搜索**：`fanqienovel` 的浏览器辅助搜索依赖 `sys.executable -m novel_downloader.apps.browser` 子进程，冻结后该方式不可用（会失败并回退到普通请求）。这是源码层的调用方式，未在打包层修改。
* **Tk 相关可选路径**：为减小体积排除了 `tkinter`，vendored musicdl 中少数只在探测屏幕尺寸时使用 Tk 的代码路径不可用，不影响主流程。
* **音乐平台依赖**：musicdl 后端在导入时使用 `pathvalidate`。若构建环境未安装，相关平台在运行时会报缺少模块；请在构建前安装（见第 1 节）或改用 NetEase 等非 musicdl 通道。
* **构建环境**：产物与构建机的 Python 版本、架构绑定（当前为 CPython 3.12 x64）。跨版本构建请在目标环境重新执行。

---

## 6. 故障排查

1. 确认 `dist\NovelDownloader\NovelDownloader.exe` 与 `_internal\` 同级存在。
2. 查看 `%LOCALAPPDATA%\novel_downloader\logs\novel_downloader.log`。
3. 若无日志，查看 `logs\desktop-crash.log`；仍无输出时，用命令行加 `--check`，或临时设置 `NOVEL_NO_NATIVE=1` 判断是否为 WebView2 问题。
4. 依赖缺失或收集不全时，删除 `build/` 与 `dist/NovelDownloader/` 后重新构建，避免旧 TOC 残留：

```bash
rm -rf build dist/NovelDownloader
.venv\Scripts\python.exe scripts\build_desktop.py
```
