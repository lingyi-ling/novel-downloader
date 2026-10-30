# 交接手册 · novel-downloader

> 面向「下一个接手本项目的 AI」。
> 只讲**项目周边**（环境、凭证、规范、当前状态、坑），不讲代码实现细节 —— 代码请自行阅读。
> 本文件为临时交接材料，**未纳入 git 版本控制**；交接完成后可自行删除。

---

## 0. 一句话现状

仓库：`c:\Users\lingy\Desktop\闲散项目\下载器`（Windows，PowerShell 5）
一句话：**「Web UI 重写为 Claude/Anthropic 风格」的代码改动已全部完成、静态检查与全量测试全绿，但浏览器端视觉验收被用户中断，尚未提交推送。**

---

## 1. 五分钟上手

| 项 | 值 |
| --- | --- |
| 工作目录（必须） | `c:\Users\lingy\Desktop\闲散项目\下载器` |
| 虚拟环境解释器 | `.venv\Scripts\python.exe`（Python **3.12.10**） |
| 包安装方式 | 源码可编辑安装，`novel_downloader` 指向 `src/novel_downloader` |
| 默认分支 | `main`，跟踪 `origin/main` |
| 远程仓库 | `https://github.com/lingyi-ling/novel-downloader.git` |
| 许可证 | **MIT**（`LICENSE`，Copyright (c) 2025 Saudade Z） |

**为什么工作目录必须是项目根**：页面模块在导入时按**相对路径**解析配置（`settings.toml`、`./downloads`、`./logs`），
`apps/web/main.py::setup_app` 特意把 `import ...pages` 放在函数内部，就是为了保证 CWD 已固定。换目录启动会读到错误配置。

```powershell
# 启动 Web UI（默认端口 8080；被占用时换端口，例如 8090）
.venv\Scripts\python.exe -m novel_downloader.apps.web.main --port 8090
```

三个入口（`[project.scripts]`）：

| 命令 | 入口 | 说明 |
| --- | --- | --- |
| `novel-cli` | `apps.cli.main:cli_main` | 命令行主入口 |
| `novel-web` | `apps.web.main:web_main` | Web UI（可选依赖 `[web-ui]`） |
| `novel-desktop` | `apps.desktop.main:desktop_main` | 桌面窗口（可选依赖 `[desktop]`，复用 `setup_app(native=True)`） |

可选依赖组（`pyproject.toml`）：`web-ui` / `desktop` / `image-utils` / `httpx` / `curl_cffi` / `all-backends` / `all` / `docs` / `tests` / `ci` / `dev`。

---

## 2. Git 凭证与提交规范 ⚠️ 重点

### 2.1 身份：本机**没有**配置 user.name / user.email

实测结论（重要）：

- `git config user.name` → **空**
- `git config user.email` → **空**
- 仓库级 `.git/config` 里**也没有**这两项（只有 remote / branch 配置）
- 但历史提交作者均已是 `lingyi-ling <316542251+lingyi-ling@users.noreply.github.com>`

⇒ **提交时必须显式用 `-c` 覆盖身份，否则会因「不知道你是谁」而失败或写错作者**：

```powershell
git -c user.name="lingyi-ling" -c user.email="316542251+lingyi-ling@users.noreply.github.com" commit -F "$env:TEMP\commit-msg.txt"
```

### 2.2 PowerShell 5 约束（提交信息不能用 heredoc）

PowerShell 5 不支持 heredoc，也**不支持 `&&`**。两种写法二选一：

```powershell
# 写法 A：临时文件 + git commit -F（推荐，ascii/中文都稳）
Set-Content -Path "$env:TEMP\commit-msg.txt" -Value "feat(web): restyle UI in a warm Claude-like theme" -Encoding utf8
git -c user.name="lingyi-ling" -c user.email="316542251+lingyi-ling@users.noreply.github.com" commit -F "$env:TEMP\commit-msg.txt"

# 写法 B：多条命令用 ; 串联（不要用 &&）
git add a.py; git add b.py; git commit -F "$env:TEMP\commit-msg.txt"
```

### 2.3 推送认证链路（凭证存在哪里）

| 层 | 实际值 |
| --- | --- |
| 仓库级 credential helper | **未配置** |
| 全局级 credential helper | **未配置** |
| **系统级** credential helper | `manager`（Git Credential Manager，见 `git config --system --list`） |
| Windows 凭据管理器 | 存在条目 `LegacyGeneric:target=git:https://github.com`，**User = `lingyi-ling`**（令牌/密码即存于此） |
| `~/.git-credentials` | **不存在**（没有明文凭据文件） |
| `GIT_ASKPASS` | 被设为 Trae CN IDE 的 askpass shim：`...\Trae CN\resources\app\extensions\git\dist\askpass.sh`（凭据弹窗经 IDE 转发） |

⇒ 推送认证**依赖 Windows 凭据管理器里那条 `git:https://github.com` 条目**。若报 403 / 认证失败：
多半是令牌失效，需要用户重新授权（重新授权后**要开新会话**新令牌才生效）。

### 2.4 网络：推送依赖用户侧 VPN/代理

- 环境变量 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 全部为**空** ⇒ git 不会自动走代理。
- 全局 git 已做慢链路调优（说明这条链路历史上很慢/不稳）：
  `http.version=HTTP/1.1`、`http.postbuffer=2147483647`（2GB）、`http.lowspeedlimit=0`、`http.lowspeedtime=999999`、`safe.directory=*`。
- **历史故障**：曾出现「关闭 VPN 后 `git push` 失败」的情况。推送前先确认用户的 VPN/代理处于开启状态。
- 全局还有 `credential.https://gitee.com.provider=generic`（仅为 gitee 配置，与 github 推送无关）。

### 2.5 不要用 `gh` CLI

本机**未安装 `gh`**（`Get-Command gh` 报 CommandNotFound）。
GitHub 相关操作若需要 API 能力，走 IDE 的 **GitHub 插件（MCP 工具）**；但提交/推送本身走 git CLI 即可。
注意：该 GitHub 连接器**缺少仓库创建权限**（重新授权后需开新会话才能生效，否则持续 403）。

### 2.6 提交信息与版本规范

- 使用 **Conventional Commits**（`commitizen` / `cz_conventional_commits`），`commitizen-branch` 钩子挂在 **pre-push**。
- 版本号唯一来源：`src/novel_downloader/__init__.py` 的 `__version__`；tag 形如 `v$version`；CHANGELOG 增量维护。
- 提交前先看 `git log --oneline -6` 对齐现有风格（示例：`feat(search): ...`、`test(config): ...`、`chore(release): ...`）。

---

## 3. 质量门禁（提交前必须全绿）

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q
```

- **基线**：`4200 passed, 25 skipped`（本次改动后实测一致）。测试有 2 条 aiohttp `DeprecationWarning`，属既有噪声，不算失败。
- 配置要点（`pyproject.toml`）：
  - ruff：`line-length = 88`、`target-version = py311`、`quote-style = double`，
    规则集 `E, F, B, C4, I, UP, SIM` ⇒ **`E501` 生效，且对字符串字面量内部同样生效**（写 CSS/SQL 等长文本时要注意换行）。
  - mypy：`strict = true`，两处例外 —— `novel_downloader.apps.web.*` 关闭 `untyped-decorator`（NiceGUI 装饰器无类型），
    `novel_downloader.libs.image_utils` 关闭 `call-overload`。
  - pytest：`pythonpath = ["src"]`、`testpaths = ["tests"]`、`addopts = --strict-markers`。
- pre-commit 钩子（`.pre-commit-config.yaml`）：ruff-check(`--fix`) → ruff-format → 空白/EOF/yaml/json/toml/大文件(>200KB)/换行符 → mypy → commitizen。
  注意 `check-yaml` **排除 `mkdocs.yml`**（该文件含 `!!python/...` 标签）。

### 已知坑：编辑器/工具会自动重排 Python 文件

观测到过多次「**未编辑的** Python 文件被自动格式化」导致 `ruff format --check` 失败（长行被展开、尾随换行被增删）。
⇒ 提交前必跑 `ruff format --check src tests`；如有漂移，用 `ruff format` 还原后再提交。

---

## 4. i18n 与文档

### 4.1 运行时多语言（Babel）

- 所有**用户可见字符串**必须走 `from novel_downloader.infra.i18n import t`，写成 `t("...")`。
- msgid 必须存在于：`src/novel_downloader/locales/zh_CN/LC_MESSAGES/messages.po`
- 当前语言：**zh_CN（默认）** + 英语；运行时 `get_language()` / `set_language()` 同进程切换（切换后 `ui.navigate.reload()`）。
- 工具链（跨平台，用 Babel 不用 GNU gettext）：
  - `python scripts/update_pot.py` → 重建 `locales/messages.pot`
  - `python scripts/update_po.py` → 合并进 `.po`
  - `python scripts/compile_mo.py` → 生成 `.mo`
- **`.mo` 是构建产物**：已 git-ignore，由 `setup.py` 在打包时重新生成；不要提交 `.mo`。
- 常用其它脚本：`scripts/gen_site_docs.py`、`scripts/check_sites_health.py`、`scripts/site_data_tool.py`、
  `scripts/i18n_strings.py`、`scripts/clean_cache.py`。

### 4.2 文档站（mkdocs）

- `mkdocs-material` + **`mkdocs-static-i18n`**，采用**后缀式**多语言文件（如 `foo.md` / `foo.en.md`），导航翻译集中在 `mkdocs.yml`。
- 文档图片在 `docs/assets/images/`（Web UI 截图会随 UI 改版而过时）。
- 根 `README.md` / `README.en.md` 为双语入口。

---

## 5. 许可证红线（硬约束）

- 项目**只允许 MIT 基底**：现有 `LICENSE` 为 MIT（Copyright (c) 2025 Saudade Z）。
- **禁止**引入 AGPL-3.0 / GPL-3.0 等项目作为基底或直接复制其代码、规则文件 —— 传染性许可会污染整个项目。
- 架构基底：`saudadez21/novel-downloader`（MIT）；借鉴 `so-novel`（书源规则思路）、`go-novel-dl`（多源聚合思路），
  **只借鉴设计、不直接复制代码/规则文件**。

---

## 6. 运行时配置与数据位置

| 项 | 位置 / 说明 |
| --- | --- |
| 默认配置 | 仓库内 `settings.toml`；用户级配置走 `platformdirs` |
| 配置隔离（测试用） | 环境变量 **`NOVEL_HOME`**：指向空目录可让配置类测试在任何机器上稳定（勿依赖宿主本地状态） |
| 导出目录 | `general.output_dir`，默认 `./downloads`；Web 端以 `/downloads/<file>` 静态挂载 |
| 日志 | `general.debug.log_dir`，默认 `./logs`；本次运行日志 `logs\novel_downloader.log` |
| 番茄登录 Cookie | 统一缓存于缓存目录的 `fanqienovel/aiohttp.cookies`；可通过 `FANQIENOVEL_COOKIE` 环境变量、`novel-cli login`、或 Web 搜索页登录按钮三种方式写入 |
| Web 端口 | 默认 8080；被占用时须显式换端口（本次实测用 **8090**） |

---

## 7. 当前未提交状态（下一个 AI 请从这里接手）

`git status`（截至交接时）：

```
 M src/novel_downloader/apps/web/components/navigation.py
 M src/novel_downloader/apps/web/pages/download.py
 M src/novel_downloader/apps/web/pages/history.py
 M src/novel_downloader/apps/web/pages/progress.py
 M src/novel_downloader/apps/web/pages/search.py
 M src/novel_downloader/apps/web/services/client_dialog.py
?? src/novel_downloader/apps/web/theme.py      ← 本次新增（唯一的 UI 主题模块）
?? .trae/                                     ← IDE 本地目录（含已批准的 UI 重写计划书）
```

最后提交：`3c81147 test(config): make the missing-config test host-independent`

### 已完成

- 新增 `src/novel_downloader/apps/web/theme.py`：Claude 风格设计令牌 + Quasar 覆盖 CSS（暖米白 / 珊瑚橙 / 衬线标题 / 1px 细描边、去投影），
  纯 CSS 实现、**明暗两套**，通过 `ui.add_css(..., shared=True)` 全站生效，**未新增任何依赖**。
- 改写顶栏与 4 个页面 + 登录弹窗的**外观**（`classes()` / `props()` 级别），**功能逻辑零改动**。
- 验证：`ruff check` / `ruff format --check` / `mypy src` / `pytest -q` **全部通过**（4200 passed, 25 skipped）。

### 未完成（唯一遗留）

- **浏览器端视觉验收被用户中断**：原计划用浏览器子代理在 8090 上逐页截图（`/`、`/download`、`/progress`、`/history`）并核对明/暗两套主题。
- 服务已停止（端口 8090 无监听、无 python 进程残留），无需清理。
- 建议的收尾动作：启动服务 → 人工/浏览器过一遍上述 4 个页面的**明暗两套主题** → 若观感 OK 即按 §2 规范提交并推送。

### 尚未做的文档同步

- `docs/guide/web-examples.md` 与 `.en.md` 内嵌的 UI 截图（`docs/assets/images/NiceGUI_*.png`）仍是**旧蓝色主题**，改版后已过时，需要重新截图替换。
- 同页排序默认值描述写的是 `Priority`，而代码默认已是 `Quality`（优选）——顺带可修正。

---

## 8. 用户偏好与协作注意事项

- 用户偏好**基于 MIT 开源项目做二次开发**、**借鉴多个成熟项目的设计**而非复制代码；**授权助手自主决定技术选型**，并要求**后续提交自动完成**（做完即提交推送，不必反复确认）。
- 改动落地后要按仓库规范**自己完成 lint/format/mypy/pytest 并提交推送**，不要留「待用户手动提交」的状态。
- ⚠️ **不要擅自接管用户的浏览器**：本次用户**主动中断**了浏览器自动化子代理（未说明原因），
  推测其不希望 AI 驱动本机浏览器/弹窗。需要视觉验收时，优先请用户自己看一眼，或先征求同意再自动化。
- **仓库不留临时探查产物**：临时文件放 `$env:TEMP`，收尾时清理；不要把草稿/调试输出留在仓库里。
- 未经验证的站点规则**不要提交到仓库**（避免制造假覆盖）；站点规则需用真实登录凭证现场验证后再合并。

---

## 9. AI 记忆（Trae）位置

Trae 会在会话开始时自动注入记忆，下一个 AI 一般无需手动读取；如需深入排查历史决策：

| 文件 | 用途 |
| --- | --- |
| `c:\Users\lingy\.trae-cn\memory\user_profile.md` | 跨项目用户画像（偏好、技术栈） |
| `c:\Users\lingy\.trae-cn\memory\projects\-c-Users-lingy-Desktop-----------p2-21544417cd86e586cb30\project_memory.md` | 本项目规则、约束、约定、经验教训 |
| 同目录 `YYYYMMDD\topics.md` + `session_memory_<id>.jsonl` | 按日期的议题摘要与逐条会话记忆 |

---

## 10. 常用命令速查

```powershell
# 环境
.venv\Scripts\python.exe -V

# 启动 Web UI（工作目录必须是项目根）
.venv\Scripts\python.exe -m novel_downloader.apps.web.main --port 8090

# 质量门禁（提交前）
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q

# 翻译
.venv\Scripts\python.exe scripts\update_pot.py
.venv\Scripts\python.exe scripts\update_po.py
.venv\Scripts\python.exe scripts\compile_mo.py

# 提交（PowerShell 5：不要用 && ，不要用 heredoc）
git add <files>
Set-Content -Path "$env:TEMP\commit-msg.txt" -Value "feat(web): ..." -Encoding utf8
git -c user.name="lingyi-ling" -c user.email="316542251+lingyi-ling@users.noreply.github.com" commit -F "$env:TEMP\commit-msg.txt"
git push            # 需用户侧 VPN/代理已开启
```
