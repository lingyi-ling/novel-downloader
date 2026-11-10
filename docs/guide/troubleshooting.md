# 故障排查

下面是常见问题的排查步骤。命令默认在项目根目录或已安装 `novel-cli` 的环境中执行；Windows 下可使用 Git Bash 或 PowerShell。示例中的 `<site>`、`<book_id>`、`<IP>` 需要替换为实际值。

---

## 0. 先收集基本信息

先运行内置诊断命令，它会检查 Python 版本、包安装、`settings.toml`、输出目录、站点/搜索器、音乐依赖与 API 端口占用：

```bash
novel-cli doctor

# 检查其它端口是否可用
novel-cli doctor --port 8080
```

失败项会给出建议；`✓` 表示通过，`✗` 表示失败（可选依赖会标注 `optional`）。

然后收集版本与日志：

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.python_version(), novel_downloader.__version__)"
tail -n 100 logs/novel_downloader.log
```

如果日志目录不是默认的 `./logs`，查看配置中的 `general.debug.log_dir`：

```bash
grep -n "log_dir\|log_level" settings.toml
```

需要更详细日志时，把 `settings.toml` 中的 `general.debug.log_level` 改为 `DEBUG` 后重试。

---

## 1. 站点返回 403 / Cloudflare 验证

**现象**：请求被拒绝、返回 `403`，或出现 Cloudflare 验证页、TLS 握手失败。

**验证**：先用浏览器同款请求头直接访问站点（把 `example.com` 换成目标站点）：

```bash
curl -I -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36" "https://example.com/"
```

若 `curl` 返回 `403` 或 `5xx`，说明站点对当前网络特征敏感，通常是 TLS 指纹或 HTTP 版本问题。

检查可选后端是否安装：

```bash
python -c "import importlib.util as u; mods=["mutagen", "pathvalidate", "PIL", "qrcode", "websocket", "m3u8", "emoji", "bleach", "filetype", "puremagic", "fake_useragent", "json_repair", "tinytag", "tabulate", "prettytable", "wcwidth", "prompt_toolkit", "aigpy", "ytmusicapi", "pywidevine"]; print([m for m in mods if not u.find_spec(m)])"
```

**解决**：

1. 安装后端：

   ```bash
   pip install "novel-downloader[all-backends]"
   ```

2. 在 `settings.toml` 中切换后端，优先尝试 `curl_cffi` 的浏览器仿真：

   ```toml
   [general]
   backend = "curl_cffi"
   impersonate = "chrome136"

   # 或尝试 httpx + HTTP/2：
   # backend = "httpx"
   # http2 = true
   ```

3. 重试目标站点：

   ```bash
   novel-cli download --site <site> <book_id>
   ```

4. 若返回 `503 Service Temporarily Unavailable`，降低请求频率：

   ```toml
   [general]
   max_rps = 1.0
   request_interval = 1.0
   ```

   已下载的章节会从 `chapter.raw.sqlite` 续爬，不会重复抓取。

> 不同站点对后端的反应可能相反：有的站点在 HTTP/2 下正常，有的反而必须使用 HTTP/1.1。请按站点分别调整，不要全局强行使用同一种后端。

---

## 2. 登录失效 / Cookie 过期

**现象**：站点提示未登录、只能看到部分章节，或返回权限错误。

**验证**：

```bash
# 查看支持交互式登录的站点
novel-cli login --list

# 检查缓存的 Cookie 文件是否存在
ls -l novel_cache/<site>/*.cookies
```

**解决**：

1. 重新走交互式登录（推荐）：

   ```bash
   novel-cli login <site>
   ```

   登录成功后会自动写入 `<缓存目录>/<site>/aiohttp.cookies`。

2. 或清理该站点的缓存后重试：

   ```bash
   novel-cli clean cache --site <site> --yes
   ```

3. 也可以在配置中提供 Cookie 或账号密码：

   ```toml
   [sites.<site>]
   login_required = true
   cookies = "完整的 Cookie 字符串"
   # 或者
   # username = "your_username"
   # password = "your_password"
   ```

4. 确认配置中的站点键与实际使用的 `--site` 一致（例如 `fanqienovel`，不是显示名）。

> Cookie 方式不会自动续期；过期后需要重新复制或重新登录。

---

### 番茄小说：部分章节只有很短的内容 / 被标记为失败

番茄对未登录的匿名访问会**按章**加锁：目录页给每个章节带 `isChapterLock`，
锁定的章节在阅读页只返回约 200 字的预览。

下载器的行为：

- 目录里标为锁定的章节记为 `accessible: false`，默认**直接跳过**，不会逐章白抓；
- 如果强制抓取（`fetch_inaccessible = true`），解析层会识别预览并判为失败，
  不会把 200 字预览当成正文写进书里。

想拿到这些章节需要登录态：

```bash
novel-cli login fanqienovel
```

或把浏览器里的 Cookie 写进配置（见上一节）。匿名状态下能下载的是本书中未锁定的章节——
这一点与是否使用本工具无关，是站点的访问控制。

---

## 3. API 返回 401 / 端口被占用

### 3.1 401 Unauthorized

**原因**：`novel-api` 启动时设置了 `--token` 或环境变量 `NOVEL_API_TOKEN`，请求没有携带匹配的 token。

**验证**：

```bash
# 未带 token 时返回 401
curl -i http://127.0.0.1:8081/api/v1/health

# 带上正确 token 后返回 200
export NOVEL_API_TOKEN="your-token"
curl -i -H "Authorization: Bearer $NOVEL_API_TOKEN" http://127.0.0.1:8081/api/v1/health
```

PowerShell 中设置环境变量：

```powershell
$env:NOVEL_API_TOKEN = "your-token"
curl.exe -i -H "Authorization: Bearer $env:NOVEL_API_TOKEN" http://127.0.0.1:8081/api/v1/health
```

也可以使用 `X-API-Key`：

```bash
curl -i -H "X-API-Key: your-token" http://127.0.0.1:8081/api/v1/health
```

**解决**：

* 确认客户端与服务器使用同一个 token。
* 如果只是本机使用，可以不设置 token：

  ```bash
  novel-api
  ```

* Android 端在“设置”中填写同一个“API 密钥”。

### 3.2 端口被占用

**现象**：启动时报 `[Errno 10048]`（Windows）或 `Address already in use`（Linux/macOS）。

**验证**：

```bash
netstat -ano | grep ":8081"
```

PowerShell：

```powershell
Get-NetTCPConnection -LocalPort 8081 | Select-Object LocalAddress,LocalPort,OwningProcess
Get-Process -Id <PID>
```

**解决**：

1. 结束占用端口的进程，或
2. 换一个端口启动：

   ```bash
   novel-api --port 8082
   ```

   同时把 Android 的 API 地址改为 `http://<IP>:8082/`。

3. 确认访问路径包含 `/api/v1`，例如 `http://127.0.0.1:8081/api/v1/sites`；根路径没有接口。

---

## 4. 路径包含中文或空格

**现象**：导出失败、资源文件找不到、第三方工具报编码错误，或 Android 下载链接异常。

**验证**：确认 Python 能正常读写中文路径：

```bash
python -c "import sys; from pathlib import Path; p = Path.cwd(); print('filesystem encoding:', sys.getfilesystemencoding()); q = p / '中文测试目录'; q.mkdir(exist_ok=True); print('ok:', q.resolve()); q.rmdir()"
```

若上面的命令成功，说明 Python 本身可以处理中文路径；问题通常来自终端代码页、旧版第三方工具或配置中的路径写法。

**解决**：

1. 把数据目录显式指向纯 ASCII 路径（路径使用正斜杠，避免 TOML 转义问题）：

   ```toml
   [general]
   raw_data_dir = "C:/novel-downloader-data/raw_data"
   output_dir = "C:/novel-downloader-data/downloads"
   cache_dir = "C:/novel-downloader-data/cache"
   ```

2. 或通过 `NOVEL_HOME` 把应用主目录移到纯 ASCII 路径：

   ```bash
   # Git Bash
   export NOVEL_HOME="C:/novel-downloader-home"
   ```

   ```powershell
   # PowerShell
   $env:NOVEL_HOME = "C:\novel-downloader-home"
   ```

3. Windows 控制台出现乱码时，切换到 UTF-8 代码页：

   ```powershell
   chcp 65001
   ```

4. 如果路径位于 OneDrive 等同步目录，先暂停同步再导出，避免文件被占用。

---

## 5. Android 连不上电脑

**现象**：App 搜索/任务一直转圈，提示连接失败或超时。

**验证与解决**：

1. 确认 API 绑定到局域网地址，而不是仅本机：

   ```bash
   novel-api --host 0.0.0.0 --port 8081 --token "your-token"
   ```

2. 查询电脑的局域网 IPv4 地址：

   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '127.*'} | Select-Object IPAddress,InterfaceAlias
   ```

   在 Git Bash 中也可以直接运行 `ipconfig`，找到当前 Wi-Fi/以太网适配器的 IPv4 地址。

3. 确认手机与电脑在同一网段：

   ```bash
   ping -n 4 <电脑IP>
   ```

4. 用手机浏览器打开 `http://<电脑IP>:8081/docs`，能打开 Swagger 页面说明网络可达；若打不开，继续检查防火墙。

5. 放行 Windows 防火墙端口（管理员 PowerShell）：

   ```powershell
   New-NetFirewallRule -DisplayName "NovelDownloader API 8081" -Direction Inbound -Protocol TCP -LocalPort 8081 -Action Allow
   ```

   或使用命令行：

   ```bash
   netsh advfirewall firewall add rule name="NovelDownloader API 8081" dir=in action=allow protocol=TCP localport=8081
   ```

6. 模拟器访问电脑使用 `http://10.0.2.2:8081/`；真机使用 `http://<电脑IP>:8081/`。保留地址末尾的 `/`。

7. 关闭路由器的“AP 隔离/客户端隔离”，并确认手机没有连接到访客网络。

8. Android App 使用 HTTP 明文访问；跨网络时请使用 VPN（如 Tailscale）或 HTTPS 反向代理，并始终设置 token。

---

## 6. 音乐下载缺少依赖

**现象**：`novel-cli music ...` 报模块缺失，或平台列表为空、下载失败。

**验证**：

```bash
novel-cli music platforms
python -c "import importlib.util as u; print('mutagen', bool(u.find_spec('mutagen'))); print('pathvalidate', bool(u.find_spec('pathvalidate'))); print('PIL', bool(u.find_spec('PIL'))); print('qrcode', bool(u.find_spec('qrcode'))); print('websocket', bool(u.find_spec('websocket')))"
```

**解决**：

1. 安装音乐可选依赖：

   ```bash
   pip install "novel-downloader[music]"
   ```

2. 重新查看平台列表：

   ```bash
   novel-cli music platforms
   ```

3. 部分平台有额外要求：

   * Apple Music / SoundCloud 需要 `pywidevine`；DRM 解密还需自备 CDM（`.wvd` 设备文件），SoundCloud 的 HLS 下载还需要外部 `N_m3u8DL-RE`。
   * YouTube 搜索需要 `ytmusicapi`（不需要 Node）；下载依赖第三方解析服务，网络不可达时会失败。
   * 部分 HLS/流媒体格式需要系统安装 `ffmpeg`。

   检查：

   ```bash
   python -c "import importlib.util as u, shutil; print('pywidevine', bool(u.find_spec('pywidevine'))); print('ytmusicapi', bool(u.find_spec('ytmusicapi'))); print('ffmpeg', shutil.which('ffmpeg'))"
   ```

4. 需要账号凭据的平台（如 Spotify、TIDAL、汽水音乐）还要在平台凭据中配置 Cookie 或 token，否则搜索可能为空。

5. 单个平台失败时，换平台验证基础功能是否正常：

   ```bash
   novel-cli music search "关键词" --platform netease --limit 5
   ```

---

## 7. 代理 / VPN 导致超时或 SSL 错误

**现象**：所有站点都超时、出现 SSL 证书错误，或开启/关闭 VPN 后行为不一致。

**验证**：

```bash
# 查看当前环境变量
env | grep -i proxy

# 测试代理是否可用
curl -x http://127.0.0.1:7890 -I https://www.google.com
```

**解决**：

1. 在 `settings.toml` 中显式配置代理：

   ```toml
   [general]
   proxy = "http://127.0.0.1:7890"
   # 代理需要认证时：
   # proxy_user = "username"
   # proxy_pass = "password"
   trust_env = false
   ```

2. 如果希望复用系统环境变量中的代理，设置：

   ```toml
   [general]
   trust_env = true
   ```

3. 避免同时启用“系统代理”和配置中的 `proxy`，造成双重代理。

4. SSL 错误通常来自代理的中间人证书。可临时关闭校验定位问题（仅用于排查，不建议长期使用）：

   ```toml
   [general]
   verify_ssl = false
   ```

5. 切换后端验证是否为 TLS 兼容性问题：

   ```toml
   [general]
   backend = "httpx"
   http2 = true
   ```

6. 音乐平台可能自行读取系统代理；若小说可下载但音乐超时，优先检查系统代理与平台可达性。

---

## 8. 音乐聚合搜索里部分平台没反应 / 很慢

**现象**：选择「全部平台」搜索时，进度条走完只回来几首；某些平台一直没有结果，或者整次搜索都要等到预算用完。单独指定该平台时却可能正常。

**机制**（`apps/music/aggregate.py`）：

* 每个平台一个独立线程，整次聚合搜索有墙钟预算：默认 5 秒（`DEFAULT_TIMEOUT`），桌面渐进式聚合搜索传入 6 秒（`SEARCH_SITE_TIMEOUT`）。
* 只要有一个平台先返回结果，再等 2 秒就收工（`EARLY_EXIT_GRACE`）；没回来的平台本次直接略过，不计为失败。
* 抛异常的平台，以及预算耗尽时仍未返回的平台，记为失败并熔断 **10 分钟**（`FAILURE_TTL`），期间聚合搜索会跳过它；因 2 秒早退被略过的平台不记失败。
* 连续两次耗时 ≥2 秒且零结果，会被判为「慢且空」，跳过 **5 分钟**（`SLOW_EMPTY_STREAK` / `SLOW_EMPTY_TTL`）。
* 这些状态写入音乐数据目录的 `search_stats.json`，重启后仍然生效。默认位置是 `%LOCALAPPDATA%\novel_downloader\music\search_stats.json`；设置 `NOVEL_MUSIC_HOME` 可以整体替换音乐数据目录。

**排查**：

1. 先单平台验证，显式指定平台时不会走熔断跳过：

   ```bash
   novel-cli music search "关键词" --platform netease --limit 5
   ```

2. 等 10 分钟（失败熔断）或 5 分钟（慢空跳过）自动恢复；也可以先退出所有客户端，再删除 `search_stats.json`，下次搜索会重建。
3. 想保留原设置又要一个干净状态，可临时把 `NOVEL_MUSIC_HOME` 指向空目录：

   ```bash
   # Git Bash
   export NOVEL_MUSIC_HOME="$TEMP/novel-music-clean"
   ```

   ```powershell
   # PowerShell
   $env:NOVEL_MUSIC_HOME = Join-Path $env:TEMP "novel-music-clean"
   ```

4. 如果所有平台都在熔断期，代码会自动清空一次缓存再重试；仍然全空时，按第 6 节检查依赖、凭据与网络。

---

## 9. 桌面自动更新：安装版与便携版行为不同

**现象**：点更新后应用退出但没有升级；升级后出现两份程序；或者提示找不到 PowerShell。

**机制**：

* 客户端先判断安装形态：可执行文件位于 `%LOCALAPPDATA%\Programs\NovelDownloader` 视为**安装版**，其它已打包目录视为**便携版**；源码运行则是 `source`。
* **安装版**下载 `NovelDownloader-setup-<version>-x64.exe`，退出后以静默参数启动 Inno Setup，原地覆盖安装目录。安装器会先删除 `_internal` 再写入新版本，并通过命名互斥量 `NovelDownloader-UpdateMutex` 关闭仍在运行的旧实例。
* **便携版**下载 zip，退出后由 PowerShell 辅助脚本 `updates/_apply_portable_update.ps1` 等待进程结束，解压到 `%TEMP%` 再覆盖当前程序目录并重新启动；不会另装一份到 `%LOCALAPPDATA%`。
* 便携版更新依赖 `powershell` 或 `pwsh`。两者都找不到时，更新会中止并提示手动解压。

**排查**：

1. 更新包在 `%LOCALAPPDATA%\novel_downloader\updates\`。失败后 zip/安装包通常仍在这里，便携版可以直接解压覆盖 `NovelDownloader.exe` 所在目录，再手动启动。
2. 清理规则：`.part` 未完成文件保留 1 天，完整安装包/zip 保留 7 天，过期自动删除。
3. 提示找不到 PowerShell：手动解压 `updates\` 里的 zip，把所有文件覆盖到当前程序目录。
4. 怀疑升级没生效或出现两份：检查是否同时存在安装版目录 `%LOCALAPPDATA%\Programs\NovelDownloader` 和一个便携目录，确认自己启动的是哪一份。侧栏底部显示的 `v...` 是当前运行版本。
5. 日志在 `%LOCALAPPDATA%\novel_downloader\logs\novel-desktop.log`，可搜索 `Launched the update installer` 或 `Launched the portable update helper` 确认更新器是否真正启动。

安装、升级、卸载时的用户数据行为见[安装](./installation.md)一节。

---

## 10. TIDAL / YouTube / Apple Music / SoundCloud：能搜到不等于能下载

**现象**：这些平台出现在列表里，但搜索为空、只返回试听，或下载时报「HLS 流暂不支持直链下载」「该平台未返回可下载直链」。

**说明**：

* 依赖已打包：桌面安装包和 `pip install "novel-downloader[music]"` 都包含 `aigpy`（TIDAL）、`ytmusicapi`（YouTube）、`pywidevine`（Apple Music / SoundCloud 的 DRM），不需要再装 Node。
* 「能搜索」和「能下载」是两件事：这些平台在搜索阶段就会尝试解析可下载直链；拿不到直链时，列表可能为空，或只有试听片段。
* **TIDAL**：必须在平台凭据里配置 token JSON，否则搜索为空。凭据保存在 `<音乐数据目录>/platform_creds.json` 的 `tidal` 键下；默认路径是 `%LOCALAPPDATA%\novel_downloader\music\platform_creds.json`。旧版音乐桌面工具有「平台凭据」对话框可写入，也可以按 JSON 直接编辑。
* **Apple Music**：需要账号才能拿到完整曲目；没有账号通常只有预览片段，且需要能访问 `music.apple.com`。
* **SoundCloud**：搜索可用；DRM/HLS 曲目需要自备 CDM（`.wvd`）与外部 `N_m3u8DL-RE`。
* 对这四个 musicdl 平台，内置下载器只接受非 HLS 的直链：解析时遇到 HLS(m3u8) 会直接返回「HLS 流暂不支持直链下载」。因此即使准备了 CDM 和 `N_m3u8DL-RE`，Apple Music / SoundCloud 的 DRM/HLS 曲目也不能在应用内下载，需要用支持 Widevine 与 `N_m3u8DL-RE` 的外部工具。TIDAL / YouTube 只有在平台返回 HTTP 直链时才能下载。

**验证**：

```bash
novel-cli music platforms
novel-cli music search "关键词" --platform tidal --limit 5
```

* 平台列表里没有它：说明依赖或注册条件不满足（例如缺 `aigpy` 时 TIDAL 不会注册）。
* 能看到但搜索为空：先查凭据与网络。
* 能搜到但下载失败：看错误原文；「HLS 流暂不支持直链下载」「该平台未返回可下载直链」都属于当前版本的预期限制，不是网络故障。

依赖安装与自检命令见第 6 节。

---

## 11. 搜索结果点「下载」提示「站点和书籍 ID 不能为空」

**现象**：在搜索结果卡片点「下载」或「追更」，弹出「站点和书籍 ID 不能为空」，但卡片上明明有书名和站点。

**原因**：v1.28.1 之前的桌面页面把站点写在 `data-add` / `data-follow` 属性上，点击处理却读取 `dataset.site`，永远取到空字符串。v1.28.1 已改为读取 `dataset.add` / `dataset.follow`，下载和追更恢复正常。

**解决**：

1. 升级到 v1.28.1 或更高版本。侧栏底部的 `v...` 是当前运行版本；安装版和便携版并存时，确认启动的是刚升级的那一份。
2. 如果升级后仍出现，收集以下信息：
   * 侧栏底部显示的版本号；
   * 日志 `%LOCALAPPDATA%\novel_downloader\logs\novel-desktop.log`；
   * 出问题的站点 key、搜索关键词，以及同一关键词下是否所有结果都失败。
3. 如果只有某个站点的结果失败、其它站点正常，可能是该站点的搜索结果本身没有返回书籍 ID（卡片的 `data-bid` 为空）。空参数在进入任务队列前就被拦下，日志中不会出现 `Failed to queue`；这类情况请在 Issue 中附上站点和关键词。

---

## 12. 仍有问题？

带上下面的信息提交 Issue：

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.platform()); print(platform.python_version()); print(novel_downloader.__version__)"
tail -n 200 logs/novel_downloader.log
```

同时说明：目标站点、执行的完整命令、`settings.toml` 中相关片段（请先删除账号、Cookie、token 等敏感信息），以及是否能稳定复现。