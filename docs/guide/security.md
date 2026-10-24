## 安全说明与审计

`novel-downloader` 是一个运行在本机的下载与导出工具。它既会访问大量第三方
站点，也可以启动一个本地 REST API 供其他设备调用，因此需要同时考虑
**远程内容、局域网访问、本地文件权限和凭据存储** 四类风险。

本文给出项目的威胁模型、敏感数据的存放位置、自查方法，以及漏洞报告渠道。

!!! warning "先记住三条"
    1. 默认只把 API 绑定在 `127.0.0.1`，不要在没有额外防护时改成 `0.0.0.0`。
    2. `--token` 只保护 `/api/v1/*` 接口，**不保护** `/downloads` 静态文件。
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
| `/downloads` 静态目录 | FastAPI `StaticFiles` 直接挂载，**不经过 token 校验**；只靠端口可达性保护 |
| `--cors` | 开启后任意网站都能从浏览器调用本机 API |
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

#### 绑定到 `0.0.0.0` 时

`--host 0.0.0.0` 会监听所有网卡。启动时如果未设置 token，程序会向 stderr
打印警告。更重要的是：

- `/api/v1/*` 受 token 保护；
- `/downloads/<文件名>` 由 `StaticFiles` 提供，**不受 token 保护**；
- `/docs`、`/openapi.json` 也不经过 token 依赖。

因此，即使设置了 token，只要端口对局域网开放，其他人仍然可以枚举并下载
`general.output_dir` 中的文件。若确实需要跨设备访问，建议：

1. 设置高强度 token；
2. 用防火墙只允许可信网段访问端口；
3. 在反向代理层对 `/downloads` 也加鉴权；
4. 不要把 `--cors` 与非回环地址一起使用。

#### Webhook 出站数据

设置 `NOVEL_API_WEBHOOK` 后，任务完成时会把任务信息（标题、站点、导出路径
等）POST 到该地址。请只指向你信任的服务，避免把阅读记录发送到第三方。

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

### 报告漏洞

如果你发现安全漏洞，请**不要**先创建公开 issue。优先使用 GitHub 仓库的
“Report a vulnerability”（私有安全公告）功能：

<https://github.com/lingyi-ling/novel-downloader/security/advisories/new>

报告中请包含：

1. 漏洞描述与影响范围；
2. 复现步骤或最小复现样例；
3. 受影响的版本 / 提交；
4. 建议的缓解或修复方式。

备用联系方式：`<security@example.com>`（占位，维护者确认后替换）。

我们会在确认问题后评估影响并尽快发布修复；在修复发布前，请不要公开细节。

---

### 加固清单

- [ ] API 保持默认 `127.0.0.1`；确需远程访问时设置 token、防火墙和反向代理。
- [ ] 不在 `0.0.0.0` 上使用 `--cors`。
- [ ] `settings.toml` 权限设为 `600`（POSIX）或仅当前用户可读（Windows ACL）。
- [ ] `novel_cache/`、`downloads/`、`logs/` 不进入云同步、备份共享或 Git。
- [ ] 定期删除不再需要的 `*.cookies`、`session.json`、`platform_creds.json`。
- [ ] 怀疑泄露时立即在站点侧注销会话并重新登录。
- [ ] 运行 `python scripts/security_audit.py`，确认没有高优先级问题。
- [ ] 关注依赖安全公告，必要时使用 `pip-audit` 等工具补充 CVE 检查。