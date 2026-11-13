## 安全说明与审计

`novel-downloader` 是一个运行在本机的下载与导出工具。它既会访问大量第三方
站点，也可以启动一个本地 REST API 供其他设备调用，因此需要同时考虑
**远程内容、局域网访问、本地文件权限和凭据存储** 四类风险。

本文给出项目的威胁模型、敏感数据的存放位置、自查方法，以及漏洞报告渠道。

!!! warning "先记住三条"
    1. 默认只把 API 绑定在 `127.0.0.1`，不要在没有额外防护时改成 `0.0.0.0`。
    2. 设置 `--token` 后，`/api/v1/*` 与 `/downloads/*` 都需要鉴权；未设置 token 时两者均对可达网络开放，但非回环来源的浏览器请求会被 Origin 门禁拒绝（见下文），除非你显式开启 `--cors`。
    3. Cookie、平台凭据和 `settings.toml` 都是明文；不要提交、同步或分享。

---

### 威胁模型

#### 需要保护的资产

| 资产 | 位置 | 泄露后果 |
| --- | --- | --- |
| 站点 Cookie / 登录态 | `novel_cache/<site>/*.cookies` | 他人可冒充你的账号访问站点 |
| 音乐平台凭据 | 平台数据目录下的 `session.json`、`platform_creds.json` | 网易云 / TIDAL 等账号被接管 |
| API token | 命令行参数或 `NOVEL_API_TOKEN` | 可控制下载任务、读取导出目录 |
| 下载与导出文件 | `general.output_dir`（默认 `./downloads`） | 暴露阅读记录、书籍元数据 |
| 原始 HTML / 日志 | `general.raw_data_dir`、`general.debug.log_dir` | 可能包含 Cookie、请求头和页面隐私内容 |
| 本地配置文件 | `settings.toml` | 可能含站点密码、Cookie、代理凭据 |

#### 攻击面与信任边界

| 入口 | 风险 |
| --- | --- |
| 本地 REST API | 绑定到 `0.0.0.0` 时，同局域网（甚至公网，取决于防火墙）可访问；无 token 时可控制下载 |
| `/downloads` 静态目录 | 设置 token 时受同一鉴权保护；未设置 token 时只靠端口可达性保护 |
| 浏览器跨源请求 | 默认有 Origin 门禁兜底（见下文）；`--cors` 开启后任意网站都能从浏览器调用本机 API |
| 目标站点返回内容 | HTML、JS、字体、图片均来自第三方；解析器和插件按不可信输入处理 |
| 本地插件 | `[plugins] enable_local_plugins = true` 时会加载并执行本地插件代码 |
| 第三方依赖 | 依赖漏洞不在本审计脚本范围内，需另行使用 `pip-audit` 等工具 |
| 云同步 / 共享目录 | 项目放在 OneDrive、Dropbox、网络共享盘时，Cookie 和导出文件会随目录同步 |

#### 不在防护范围内的情况

- 已经取得你操作系统账户权限的攻击者（可读取同一用户的任何文件）。
- 你在命令行中明文传入的 token（会进入 shell 历史和进程列表）。
- 你主动把 `novel_cache/`、`settings.toml` 或 `downloads/` 打包发给他人。
- 目标站点本身被入侵或返回恶意内容；本项目不做内容沙箱。

---

### API token 的使用

API 默认监听 `127.0.0.1:8081`，未配置 token 时不做鉴权。这仅适用于
“只有本机能访问”的场景。

配置 token 的方式：

```bash
# 推荐：环境变量，不进入 shell 历史
export NOVEL_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
novel-api

# 或命令行参数（会出现在 shell 历史和进程列表中）
novel-api --host 127.0.0.1 --port 8081 --token "$NOVEL_API_TOKEN"
```

客户端需携带以下任意一个请求头：

```http
Authorization: Bearer <token>
```

```http
X-API-Key: <token>
```

实现细节：token 比较使用 `secrets.compare_digest`，避免时序侧信道；token
为空时直接放行。

#### 浏览器来源门禁（Origin gate）

"只绑定 `127.0.0.1`" 能挡住远程客户端，却挡不住浏览器：你访问的任意网页都可以
向 `http://127.0.0.1:<port>` 发起跨源请求（浏览器对跨源 POST 一定会带
`Origin` 头）。因此 API 默认安装一道 Origin 门禁
（`apps/api/middleware.py` 的 `OriginCheckMiddleware`）：

- 带有**非回环** `Origin` 的请求一律返回 `403`——包括形如
  `127.0.0.1.evil.com` 的仿冒主机、`Origin: null`（沙箱 iframe）等；
- 非浏览器客户端（CLI、SDK、curl）不发 `Origin` 头，完全不受影响；
- 回环来源（`http://127.0.0.1:任意端口`、`http://localhost`、`http://[::1]`）
  放行，本机网页工具继续可用；
- `--cors` 是跨源浏览器访问的显式开关：加上该参数后**门禁不安装**，由 CORS
  策略约束浏览器能做什么。

已知残余风险：同源 DNS rebinding 页面（攻击者域名先解析到公网加载页面、再
rebind 到 `127.0.0.1`）发出的请求是"同源"的，**不带 `Origin` 头**，门禁无法
识别。要完整防御这类读取/驱动攻击，请配置 `--token`——这也是文档反复建议
"暴露即加 token" 的原因。另一个相关输入校验：任务与追更请求中的 `book_id`
会成为 `raw_data/<site>/<book_id>` 目录名，API 会拒绝含路径分隔符、`..`、
盘符冒号等字符的取值（`422`），防止把章节缓存写到原始数据目录之外。

#### 绑定到 `0.0.0.0` 时

`--host 0.0.0.0` 会监听所有网卡。启动时如果未设置 token，程序会向 stderr
打印警告。更重要的是：

- `/api/v1/*` 与 `/downloads/<文件名>` 都受 token 保护；
- `/docs`、`/redoc` 与 `/openapi.json` 在非回环绑定下**自动关闭**，不会把完整路由与参数表暴露到局域网；
  它们仅在回环监听时提供，且不经过 token 依赖。

因此，真正需要跨设备访问时，请在设置 token 的前提下开放端口，并配合防火墙限制来源网段。建议：

1. 设置高强度 token；
2. 用防火墙只允许可信网段访问端口；
3. 在反向代理层对 `/downloads` 也加鉴权；
4. 不要把 `--cors` 与非回环地址一起使用。

#### Webhook 出站数据

设置 `NOVEL_API_WEBHOOK` 后，任务完成时会把任务信息（标题、站点、导出路径
等）POST 到该地址。请只指向你信任的服务，避免把阅读记录发送到第三方。

Webhook URL 常在查询串里携带共享密钥（`?token=...`）。投递失败写入日志时，
URL 会先脱敏（只保留 `scheme://host[:port]/path`），urllib3 异常文本里内嵌的
查询串也会被同样抹掉——日志里永远不会出现查询串或基本认证信息，与访问日志
"只记路径、不记查询串"的策略一致。

---

### 下载目录与文件共享

相关配置：

```toml
[general]
output_dir = "./downloads"    # API 的 /downloads 静态目录
raw_data_dir = "./raw_data"   # 原始 JSON / 章节内容
cache_dir = "./novel_cache"   # 缓存 + Cookie
```

风险点：

- `downloads/` 与 `raw_data/` 默认位于当前工作目录，容易被整体压缩、备份或
  同步到云端。
- API 的 `/downloads` 直接映射 `output_dir`，文件名和历史记录接口会暴露
  “你下载了什么”。
- `raw_data/`、`logs/` 可能包含完整 HTML、请求头和异常堆栈，比成品电子书
  更容易泄露 Cookie。
- 在多用户机器上，目录若为全局可读，其他本地用户也能读取导出文件。

建议：

- 把 `output_dir`、`raw_data_dir`、`cache_dir` 放到仅当前用户可访问的目录；
- 不要把上述目录放进 OneDrive / Dropbox / Git 仓库；
- 对外提供 API 时，只暴露必要的导出目录，并在反向代理层鉴权；
- 定期清理不再需要的 `raw_data/` 和日志。

---

### Cookie 与凭据的存储位置

| 类型 | 默认位置 | 覆盖方式 |
| --- | --- | --- |
| 站点 Cookie | `<cache_dir>/<site>/aiohttp.cookies`，默认 `./novel_cache/<site>/` | `general.cache_dir`；登录助手和部分搜索器还支持 `NOVEL_CACHE_DIR` |
| 音乐登录态 | `%LOCALAPPDATA%\novel_downloader\music\session.json`（Windows） | `NOVEL_MUSIC_HOME` |
| 音乐平台凭据 | 同上目录的 `platform_creds.json` | `NOVEL_MUSIC_HOME` |
| 站点配置中的 Cookie / 密码 | `settings.toml` 的 `[sites.<site>]` | 手动编辑 |
| 应用状态 | `%LOCALAPPDATA%\novel_downloader\state.json` | `NOVEL_HOME` |

Cookie 文件是 JSON 数组，条目形如
`{"name": "...", "value": "...", "domain": "...", "path": "..."}`，**值为明文**。
写入时使用临时文件 + `os.replace` 原子替换，并在 POSIX 系统上尽力执行
`chmod 0600`；Windows 没有等价的 POSIX 权限位，文件继承父目录 ACL。

检查 Windows 权限：

```powershell
icacls "$env:LOCALAPPDATA\novel_downloader"
icacls .\novel_cache
```

建议：

- 不要提交 `novel_cache/`、`settings.toml`、`downloads/`、`logs/`；
  这些条目已在 `.gitignore` 中，但请不要用 `git add -f` 绕过。
- 不要把带有 Cookie 的配置粘贴到 issue、聊天记录或截图中。
- 退出登录、怀疑泄露或换机时，删除对应的 `*.cookies` / `session.json`，
  并在站点侧注销所有会话。
- 定期检查 `settings.toml` 是否残留 `cookies = "..."` 或 `password = "..."`。

---

### 更新链的威胁模型

自动更新把"第三方内容"换成了"可执行程序", 因此单独列出。链路上的信任锚:

| 环节 | 约束 | 代码位置 |
| --- | --- | --- |
| 清单获取 | 固定 HTTPS 端点 `releases/latest/download/latest.json`; `schema` 不符直接拒绝 | `apps/common/updater.py` 的 `fetch_update_manifest` / `parse_update_manifest` |
| 下载地址 | 清单中的资产地址必须是 **HTTPS**; 安卓端同样只接受 HTTPS | `_manifest_asset`、`UpdateChecker.downloadRequestFailure` |
| 完整性 | 资产必须带 **SHA-256**: 桌面端与安卓端缺失即拒绝下载, CLI 在非交互 (`--yes`) 下同样拒绝 (交互模式会跳过校验); 下载后复算, 失败立即删除半成品 | `download_asset` / `verify_sha256` |
| 安装前 | 启动安装器**前再复算一次摘要**, 关闭"校验后到安装前被替换"的 TOCTOU 窗口 | `webview_app._install_verified`、`installPrecheck` |
| 发布侧 | 上传前运行 `scripts/audit_release_assets.py` 做**资产泄漏审计** (允许清单、归档成员路径、用 `scan_secrets` 规则扫描归档内文本成员、原始字节中的私钥/部署口令/令牌), 命中只报标记名, 不回显明文 | `scripts/release.py` 的 `audit_release_content` |

**可选的清单签名**: 更新清单支持 Ed25519 签名。`updater.TRUSTED_UPDATE_KEYS` 默认为空, 即**默认不验签**, 信任锚是 HTTPS 加每个资产的 SHA-256。一旦配置可信公钥, 清单缺失、未签名、签名不符或使用未知 `key_id` 都会被拒绝, 且客户端**不再回退** GitHub API——否则攻击者删掉清单即可绕过验签。

残余风险:

- 默认未启用签名时, 能同时篡改清单与资产的攻击者 (例如仓库控制权失守) 可以下发一个摘要自洽的恶意包; 需要更强保证时请启用清单签名, 并在客户端内置公钥。
- 资产泄漏审计只覆盖发布产物, 不能替代对构建机与发布凭据的保护。

---

### 自行审计

仓库提供离线审计脚本 `scripts/security_audit.py`，**不联网**，不会查询 CVE。

```bash
# 人类可读报告
python scripts/security_audit.py

# JSON 报告（stdout 只有 JSON）
python scripts/security_audit.py --json

# 额外审计指定配置文件
python scripts/security_audit.py --config path/to/settings.toml

# 连 tests/ 中的假凭据一起扫描
python scripts/security_audit.py --scan-tests
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 审计完成，没有高优先级问题 |
| `1` | 审计完成，发现至少一个高优先级问题 |
| `2` | 参数错误或审计本身无法运行 |

检查项：

| 检查 | 内容 |
| --- | --- |
| `dependencies` | 对照 `pyproject.toml` 与 `importlib.metadata`，报告缺失或版本不满足的依赖；核心依赖缺失为高优先级 |
| `configuration` | `verify_ssl = false`、配置文件中的明文 token/cookie/password、配置文件权限过宽 |
| `code_tls` | 源码中硬编码的 `verify=False` / `ssl.CERT_NONE` |
| `api_docs` | 文档里 `--host 0.0.0.0` 示例是否提示 token、是否同时启用 `--cors` |
| `storage` | 明文 Cookie / 凭据文件、云同步目录、POSIX 权限 |
| `credentials` | 跟踪文件中的 cookie、token、API key、私钥等模式；只报告位置和脱敏长度 |

脚本**不会**打印匹配到的凭据值，输出中的 `evidence` 只包含键名、行号和
`<redacted:N chars>`。

限制：

- 不做版本 CVE 查询，也不做动态行为分析；
- Windows 下不校验 ACL，需要手动运行 `icacls`；
- 可能把文档中的真实示例或测试夹具误报为凭据，请结合上下文判断；
- 插件和站点解析器的安全边界依赖代码审查，不在自动检查范围内。

---

### 仓库凭据扫描

`scripts/security_audit.py` 检查的是本机运行环境；要把**仓库本身**（工作区、暂存区、历史提交）当作凭据泄漏面来检查，使用 `scripts/scan_secrets.py`。它只依赖 Python 标准库和 `git`，完全离线，输出中只有规则名和位置，**不会回显命中的值**，因此终端记录和 CI 日志不会变成新的泄漏源。

```bash
# 工作区：扫描 Git 跟踪的所有文件
python scripts/scan_secrets.py

# 暂存区：只扫描已 git add 的改动（新增/复制/修改/重命名）
python scripts/scan_secrets.py --staged

# 历史：只读扫描每个提交新增的行
python scripts/scan_secrets.py --history

# 限定版本范围与提交数（只能与 --history 同用）
python scripts/scan_secrets.py --history --since v1.20.0 --max-commits 200

# 机器可读输出：stdout 只有 JSON
python scripts/scan_secrets.py --json
```

输出形如 `path:line: rule`（历史扫描为 `commit:path:line: rule`）。`--json` 输出 `count` 与 `findings`（历史还包含 `commits_scanned`、`skipped_commits`、`truncated`），同样不含明文。

| 规则 | 识别对象 |
| --- | --- |
| `private-key` | PEM 私钥头（RSA/EC/OpenSSH/DSA/PGP），只匹配块标记 |
| `aws-access-key` | `AKIA` / `ASIA` 前缀的 AWS access key id |
| `github-token` | `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` 与 `github_pat_` 令牌 |
| `slack-token` | `xoxa-`/`xoxb-`/`xoxp-`/`xoxr-`/`xoxs-` 令牌 |
| `bearer-token` | 字面 `Bearer` 凭据（排除占位符） |
| `generic-secret` | `password`、`secret`、`token`、`api_key` 等键的字面赋值（排除环境变量/配置属性引用与占位符） |
| `sensitive-file` | 被跟踪的 `*.jks`、`*.keystore`、`*.p12`、`*.pfx`、`*.bks`、`keystore.properties`、`id_rsa` 等文件名 |

范围与限制：

- 只读取 Git 已知的文件（`git ls-files`；`--staged` 读取索引中的暂存 blob）。被 `.gitignore` 忽略的构建产物和本地凭据文件不在范围内。
- 二进制文件、未知扩展名和大于 2 MiB 的文件跳过内容扫描；但 keystore/私钥**文件名**本身就会命中 `sensitive-file`。
- `--history` 只扫描 `git log -p -U0` 中每个提交新增的行，因此已从工作区删除的凭据仍会被发现。合并提交默认不产生 patch；单个提交的 patch 超过 8 MiB 时整段跳过，累计读取超过 256 MiB 时提前停止（输出会注明），可用 `--max-commits` 缩小范围。扫描全程只读，不 checkout、不改写历史。
- `--staged` 与 `--history` 互斥；`--since` / `--max-commits` 必须与 `--history` 同用，`--max-commits` 至少为 1。

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 没有命中 |
| `1` | 至少一个命中，或扫描无法运行（参数冲突、`git` 失败等；错误只写 stderr，不含文件内容） |

#### 可选的本地 pre-commit 钩子

如果希望在 `git commit` 之前自动扫描暂存区，可以显式安装仓库级钩子：

```bash
# 安装/更新 .git/hooks/pre-commit（写入本工具的标记）
python scripts/install_git_hooks.py

# 已存在其他工具的 pre-commit 时默认拒绝；--force 会先备份为 pre-commit.bak
python scripts/install_git_hooks.py --force

# 只删除由本工具安装的钩子；其他钩子和其他工具的 pre-commit 不受影响
python scripts/install_git_hooks.py --uninstall
```

钩子优先使用仓库内 `.venv` 的 Python，找不到时回退到 `python3`/`python`，然后运行 `scripts/scan_secrets.py --staged`；仓库路径含空格也能正常工作。安装器只写当前仓库的 `.git/hooks/`，**不会**修改全局 Git 配置，也不会默认安装。若设置了 `core.hooksPath`，Git 不会读取 `.git/hooks`，该钩子此时不生效。

这是开发者本机的便利门禁，不是强制安全边界；CI 里的 `secrets` job 与每周历史扫描才是最终门禁，未安装或绕过本地钩子都不会降低 CI 的检查强度。

#### CI 中的两道检查

| 工作流 | 触发 | 行为 |
| --- | --- | --- |
| `.github/workflows/ci.yml` 的 `secrets` job | 推送到 `main`、任何 pull request、手动 dispatch | 运行 `python scripts/scan_secrets.py` 扫描工作区；只做 checkout 和 Python 安装，5 分钟超时。命中即 job 失败，PR 在评审/发布前被拦下。 |
| `.github/workflows/security.yml` | 每周一 **03:17 UTC**（`cron: "17 3 * * 1"`）与手动 dispatch | `fetch-depth: 0` 拉取完整历史；先后运行工作区与 `--history` 两次扫描，任一命中都以非零退出。两份 `--json` 报告写入文件（不打印到日志），作为 artifact `security-scan-reports` 上传，**保留 30 天**。 |

`security.yml` 的两次扫描总会都执行，避免一次失败掩盖另一次；并发组固定为 `security-scan` 且不取消进行中的任务。GitHub 会在仓库 60 天无活动后暂停定时工作流，此时用 `workflow_dispatch` 手动补跑。

#### 发现命中后

1. **定位**：按输出的路径与行号（历史命中还带提交号）查看上下文，例如 `git show --stat <commit>`、`git log -1 <commit>`。不要复制或粘贴命中的值；确认它是否仍是有效凭据、是否被其他文件复用。
2. **先轮换，再清理**：值一旦进入提交就应视为已泄露。到签发方吊销/轮换（令牌、密码、Access Key），更新环境变量或密钥存储，检查签发方的使用日志；私钥则更换密钥对并从 `authorized_keys` 移除旧公钥。只删掉代码里的那一行并不能撤回已经克隆或缓存的副本。
3. **清理工作区/暂存区**：删除该值或改为环境变量引用，再运行 `python scripts/scan_secrets.py --staged`，确认干净后提交。
4. **清理历史（谨慎）**：只有确有必要时才改写历史（`git filter-repo`、BFG 等）。

!!! warning "改写历史不可逆"
    改写会改变该提交之后的所有 commit ID，需要 force push，并使其他人的克隆、fork、开放的 PR、标签全部失配；GitHub 还可能通过 API 缓存旧提交，必要时需联系支持清除。操作前先做完整镜像备份并通知协作者。轮换凭据永远是第一步——历史改写无法覆盖已经 clone、fork、缓存或出现在 CI 日志里的副本。

#### 误报与白名单

扫描器把明显的占位符当作误报：值里出现 `your`、`example`、`placeholder`、`dummy`、`<...>`、`${...}`，或赋值来自环境变量/配置对象（如 `os.environ`、`settings.`）时不会命中。`tests/` 等测试夹具目录另有按规则的窄白名单（见下文 `ALLOWLIST`）。

仍需例外时，在 `scripts/scan_secrets.py` 的 `ALLOWLIST` 中追加一条 `(路径 glob, 规则集合, 原因)`：

```python
ALLOWLIST: tuple[tuple[str, frozenset[str], str], ...] = (
    # ...
    (
        "docs/guide/example.md",          # 尽量精确到文件，不要用 *
        frozenset({"generic-secret"}),    # 只放宽确认误报的规则
        "documented placeholder example", # 说明为什么安全
    ),
)
```

约束：

- 未列出的规则在该路径下仍然生效；不要用 `*` 关闭全部规则。
- 只放宽**确认是合成值**的那一条规则，路径尽量精确到文件或最小目录。
- 优先把示例改成明显的占位符或环境变量引用，而不是加白名单；白名单越宽，真实凭据越容易漏过。
- 白名单的理由里不要写入真实值，测试夹具也只用合成凭据。

---

### 发布资产内容扫描 {#release-asset-scan}

上传前的最后一道内容是**发布资产内容扫描**：`scripts/release.py` 在构建完成、生成 `latest.json` 之后调用 `scripts/audit_release_assets.py`，把每个 zip / apk，以及实际是 zip 的 `.exe`（PyInstaller 单文件包）里的**文本成员**逐行交给 `scripts/scan_secrets.py` 的规则扫描。它直接复用仓库扫描器的规则、占位符判断和路径白名单，因此两边不会各自演化出一套定义。

命中以 `secret-marker` 标记，形如：

```text
PROBLEM [secret-marker] <资产名>: <成员路径>:<行号>: <规则名>
```

只报资产、成员、行号和规则名，**不回显命中的值**。

扫描边界（刻意控制成本，不要理解成“归档里的所有内容都被检查了”）：

- 单个成员超过 **2 MiB** 时整个跳过（与 `scan_secrets` 的单文件上限一致）；单个资产的文本成员扫描合计上限为 **64 MiB**，整次审计上限为 **256 MiB**，超出预算的成员或资产直接跳过。
- 前 8 KiB 内出现 NUL 的成员按二进制跳过；目录、符号链接和嵌套的 `.zip` / `.exe` / `.apk` 成员不扫描，嵌套归档也**从不解压**。
- 只把能作为 ZIP 打开的 `.zip` / `.apk`，以及实际是 ZIP 的 `.exe` 当作归档；不是 ZIP 的 `.exe` 不会按归档打开。
- 读不出的成员会作为 `unreadable-archive` 问题报告，而不是当作干净内容放过；归档成员数超过 200,000 时停止该归档的成员扫描并报 `archive-too-large`。
- 成员路径同样套用 `scan_secrets` 的路径白名单（例如测试夹具的窄豁免），但这里是归档内路径，不是仓库路径。

它和构建前的工作区扫描是**互补关系**，不是重复：

- 构建前：`release.py` 运行 `scan_release_secrets`，即 `scripts/scan_secrets.py` 的工作区模式，只扫描 **Git 跟踪的文件**；`--skip-secret-scan` 可以关闭这一步（不推荐）。
- 构建后、上传前：资产内容扫描检查的是**真正要上传的字节**，能发现源码扫描看不到的东西，例如构建时生成、从依赖收集或误打包进归档的文件。它没有对应的跳过开关。

失败退出码：

| 运行方式 | 退出码 | 含义 |
| --- | --- | --- |
| `python scripts/audit_release_assets.py --dir <目录>` | `0` | 没有问题 |
| 同上 | `1` | 至少一个 `secret-marker` 或其他审计问题 |
| 同上 | `2` | 参数或 I/O 错误，审计无法运行 |
| `scripts/release.py` | `2` | 资产审计命中会抛出 `ReleaseError`，发布在打 tag 和上传前中止 |

局限：它只覆盖上述文本成员和原始字节检查，不检查二进制成员的内部结构、不解压嵌套归档、不做熵或语义分析，也不能替代对构建机和发布凭据的保护。

---

### 报告漏洞

如果你发现安全漏洞，请**不要**先创建公开 issue。优先使用 GitHub 仓库的
“Report a vulnerability”（私有安全公告）功能：

<https://github.com/lingyi-ling/novel-downloader/security/advisories/new>

报告中请包含：

1. 漏洞描述与影响范围；
2. 复现步骤或最小复现样例；
3. 受影响的版本 / 提交；
4. 建议的缓解或修复方式。

备用联系方式：可通过上述 GitHub 私有安全公告与维护者联系。

我们会在确认问题后评估影响并尽快发布修复；在修复发布前，请不要公开细节。

---

### 加固清单

- [ ] API 保持默认 `127.0.0.1`；确需远程访问时设置 token、防火墙和反向代理。
- [ ] 不在 `0.0.0.0` 上使用 `--cors`；仅在确实需要浏览器跨源调用时才开启
      `--cors`（开启即关闭 Origin 门禁）。
- [ ] `settings.toml` 权限设为 `600`（POSIX）或仅当前用户可读（Windows ACL）。
- [ ] `novel_cache/`、`downloads/`、`logs/` 不进入云同步、备份共享或 Git。
- [ ] 定期删除不再需要的 `*.cookies`、`session.json`、`platform_creds.json`。
- [ ] 怀疑泄露时立即在站点侧注销会话并重新登录。
- [ ] 运行 `python scripts/security_audit.py`，确认没有高优先级问题。
- [ ] 提交前运行 `python scripts/scan_secrets.py --staged`，并保持 CI 的 `Secret scan` 与每周历史扫描为绿色。
- [ ] 关注依赖安全公告，必要时使用 `pip-audit` 等工具补充 CVE 检查
      （`pip-audit` 对本仓库的 editable 安装需配合 `--skip-editable`）。