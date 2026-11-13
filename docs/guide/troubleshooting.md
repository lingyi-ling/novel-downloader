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

如果怀疑是「目录缺失 / 临时文件堆积」这类本地状态问题，先预览自动修复，再执行：

```bash
novel-cli doctor --fix --dry-run   # 只报告会创建/删除什么，零改动
novel-cli doctor --fix             # 创建缺失的输出/缓存/日志目录，清理过期 .tmp/.part
```

`--fix` 只在配置的输出、缓存、日志三个目录内动作，拒绝 `..` 与符号链接，不改配置、导出文件或登录态；默认只删除 7 天前的临时文件（`--fix-age-days` 可调）。配置错误、依赖缺失、端口占用等问题它不会代修，仍需按检查项的建议处理。

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

4. 用手机浏览器打开 `http://<电脑IP>:8081/api/v1/health`。绑定非回环地址时 `/docs` 会自动关闭，因此改用健康检查端点：返回 `401`（`Invalid or missing API token...`）说明网络可达且鉴权生效；若连接超时或被拒绝，继续检查防火墙。

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

### 5.1 自定义下载目录 (SAF) 不可用

**现象**：设置里选过下载目录，但下载仍落到系统「下载」目录；或设置页出现红色提示「自定义下载目录不可用，请在设置中重新选择」。

**原因**：Android 通过 SAF (`ACTION_OPEN_DOCUMENT_TREE`) 授权目录，应用保存的是持久化 URI 权限。以下情况会让授权失效：用户在系统设置中撤销权限、卸载/更换了提供该目录的应用、目录被删除或移走、备份恢复到另一台设备。

**行为与处理**：

* 设置页会保留原来选的目录（方便直接替换），同时明确提示不可用；历史页下载会**自动回退到系统 Downloads**，并提示「自定义下载目录不可用，已改为保存到系统 Downloads」，不会把文件写到错误位置，也不会让下载直接失败。
* 重新点「选择目录」并确认系统授权即可恢复；选择时会请求并保存读/写权限，保存失败会提示「无法保留该目录的写入权限，请换一个目录」，此时不会覆盖原来可用的目录。
* 点「恢复默认」会释放持久化权限并回到系统 Downloads。
* 文件实际写入所选目录下的 `NovelDL/` 子目录；同名文件按 `name (1).ext`、`name (2).ext` … 去重（大小写不敏感），不会静默覆盖已有文件。
* 该设置只影响「历史」页导出文件的下载；音乐文件仍按音乐引擎自己的目录/系统下载流程保存。

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

**歌词没有生成（`.lrc` 缺失）**：

1. 确认音乐设置里的 `save_lyric` 未被关闭（默认 `true`）；它位于音乐数据目录的 `settings.json`。
2. 平台可能本来就没有歌词，或只返回占位值；此时不会创建文件，也不影响音频成功。
3. 歌词文件与音频同名：`<歌名> - <歌手>.lrc`，有译文时为 `.zh.lrc`（只有译文时会写进 `.lrc`）。若音频用了 ` [<id>]` 去重后缀，歌词也带同一后缀——到音频所在目录按同名查找。
4. 音频已存在且被 `skip_existing` 跳过时，只有 `.lrc` 不存在才会补写；已有歌词不会被覆盖，也不会重新联网。
5. 超过 1 MiB 的歌词响应会被当作错误页跳过（日志里有 WARNING）。把日志级别调到 `DEBUG` 可以看到具体原因。

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

* 客户端先判断安装形态：优先读取卸载注册表的 `InstallLocation` / `DisplayIcon`（`{AppId}_is1` 键），其次看可执行文件旁是否有 `unins000.exe`，最后才检查默认目录 `%LOCALAPPDATA%\Programs\NovelDownloader`。因此安装到自定义目录仍算**安装版**；其它已打包目录视为**便携版**；源码运行则是 `source`。
* **安装版**下载 `NovelDownloader-setup-<version>-x64.exe`，退出后以静默参数启动 Inno Setup，原地覆盖安装目录。安装器会先删除 `_internal` 再写入新版本，并通过命名互斥量 `NovelDownloader-UpdateMutex` 关闭仍在运行的旧实例；静默安装结束后会直接启动新版本，无需手动打开。
* **便携版**下载 zip，退出后由 PowerShell 辅助脚本 `updates/_apply_portable_update.ps1` 等待进程结束，解压到 `%TEMP%`，先删除当前程序目录中的 `_internal` 再覆盖并重新启动，因此已移除的插件不会残留；`app_home` 下的设置与下载不受影响。不会另装一份到 `%LOCALAPPDATA%`。
* 便携版更新依赖 `powershell` 或 `pwsh`。两者都找不到时，更新会中止并提示手动解压。

**排查**：

1. 更新包在 `%LOCALAPPDATA%\novel_downloader\updates\`。失败后 zip/安装包通常仍在这里，便携版可以直接解压覆盖 `NovelDownloader.exe` 所在目录，再手动启动。
2. 清理规则：`.part` 未完成文件保留 1 天，完整安装包/zip 保留 7 天，过期自动删除。
3. 提示找不到 PowerShell：手动解压 `updates\` 里的 zip，把所有文件覆盖到当前程序目录。
4. 怀疑升级没生效或出现两份：安装版目录由卸载注册表（`InstallLocation` / `DisplayIcon`）或 `unins000.exe` 确定，未必是默认的 `%LOCALAPPDATA%\Programs\NovelDownloader`；确认自己启动的是哪一份。侧栏底部显示的 `v...` 是当前运行版本。
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

## 12. 音乐歌单导出返回 400 / 音乐任务返回 429

**现象**：`POST /api/v1/music/export` 返回 `400`；或 `POST /api/v1/music/jobs`
返回 `429`，即使并没有触发按 IP 的请求限流。

**先看 `detail`**（两种情况的消息都是固定的，不会回显你提交的路径）：

| `detail` | 含义 |
| --- | --- |
| `Music out_dir must be inside the music output directory` | 下载目录越界 |
| `Playlist out_path must be a .m3u/.m3u8 file inside the music output directory` | 歌单路径越界或后缀不合法 |
| `Too many music jobs are already running (limit 4); retry later` | 活跃音乐任务已达上限 |

**排查 `out_dir` / `out_path` 的 400**：

1. 允许写入的根目录只有两个：音乐设置里的 `out_dir`，以及平台默认音乐目录
   （通常是 `~/Music/novel_downloader`）。Windows 上音乐设置文件在
   `%LOCALAPPDATA%\novel_downloader\music\settings.json`，检查其中的 `out_dir`；
   设置 `NOVEL_MUSIC_HOME` 会整体替换音乐数据目录。

   ```bash
   # Git Bash
   cat "$LOCALAPPDATA/novel_downloader/music/settings.json"
   ```

2. 把 `out_dir` 改成上述根目录内的绝对路径，或直接省略该字段使用默认目录。
   相对路径是相对**默认目录**解析的，不是相对 API 进程的当前目录。
3. `out_path` 只要带后缀，就必须是 `.m3u` / `.m3u8`；`.txt`、`.json` 等会被拒绝。
   不带后缀的值按目录处理。`..`、指向根目录之外的软链接、以及根目录之外的绝对路径
   都会被拒绝，即使目标目录确实存在。
4. 如果返回的是 `500` 而不是 `400`，说明路径已通过校验但写入失败（权限、磁盘、
   目录被占用）。`detail` 只含异常类名（`"Could not write playlist: <类名>"`），
   具体原因在服务端日志里。

**排查 `429`**：

1. 确认 `detail` 是 `Too many music jobs...` 而不是限流消息。两者都带
   `Retry-After` 头：任务上限的 `429` 固定为 `5` 秒（等待约一个下载槽位），
   限流 `429` 的值取决于限流窗口何时排空。
2. 同时 `queued` / `running` 的音乐任务最多 `4` 个（`done` / `failed` 不计数）。
   先轮询已有任务，等它们进入终态后再提交：

   ```bash
   curl -s "$BASE/api/v1/music/jobs/<job_id>" -H "Authorization: Bearer $TOKEN"
   ```

3. 批量下载也可以改用同步端点 `POST /api/v1/music/download`（会阻塞到全部完成），
   或拆成更小的批次。

---

## 13. 自动更新拒绝 http 资产 / 下载被清理

**现象**：桌面端更新提示 `Refusing to download an update from an insecure source`；
`novel-cli update --yes` 以退出码 `1` 结束；`novel-cli update --json --yes`
输出里 `error` 为 `insecure-url`、`missing-digest` 或 `no-cli-asset`；下载中断后
`updates\` 里留下 `.part` 文件。

**机制**（桌面端与 CLI 共用 `apps/common/updater.py`）：

* `novel-cli update` 显式请求 `shape="cli"`，只接受 `novel-cli-<version>-<platform>.zip`。
  即使清单/API 只返回桌面安装包，也会丢弃该回退并停止，不会下载或运行
  `NovelDownloader-setup-*.exe`。对应地，`--json` 的 `asset_shape` 在选中 CLI 资产时
  为 `cli`，没有 CLI 资产或检查失败时为 `null`。
* 发布清单提供的资产 URL 必须是 `https://`。CLI 在询问/下载之前就拒绝明文地址；
  桌面端同样拒绝。
* 下载器本身只接受 `http://` / `https://`；如果 HTTPS 请求被重定向到明文 HTTP，
  会直接拒绝（校验和只能保证完整性，不能保证保密性）。
* 单个资产上限 `1 GiB`（`1024**3` 字节），超过即拒绝，避免被篡改的清单/镜像写满磁盘。
* 下载失败时：非续传模式在**下一次重试**前删除 `.part`；启用续传时保留已下载前缀，
  下次用 `Range` 继续；SHA-256 校验失败则一定删除 `.part`（旧版本残留没有继续使用的价值）。
  所有重试都失败后，未完成文件可能留在 `updates\`，桌面端会在 1 天后自动清理。

**排查**：

1. `insecure-url`：确认用的是官方发布页或 HTTPS 镜像。`NOVEL_UPDATE_MIRROR`
   是用户自己配置的前缀，但镜像若把 HTTPS 重定向到 HTTP，也会在下载阶段被拒绝。
2. `missing-digest`：该 release 没有为你的平台资产提供 SHA-256，客户端不会下载
   无法校验的文件；到发布页手动下载并核对摘要。
3. `no-cli-asset`：当前平台没有匹配的 CLI 资产（`novel-cli-*.zip`，例如发布漏传或架构不受支持）。CLI 不会回退到桌面安装包；请改用 `pip install -U novel-downloader`，或到发布页手动下载对应 CLI zip。
4. 反复校验失败：关闭应用后删除 `%LOCALAPPDATA%\novel_downloader\updates\`
   下对应的 `.part` 再重试；桌面端也会自动清理超过 1 天的 `.part`。
5. 需要机器可读原因时用 `novel-cli update --json --yes`：它不会进入交互，
   下载失败的原因在 `error` 字段里，`asset_shape` 标明实际选择的资产形态
   （CLI 只会是 `cli` 或 `null`；只加 `--json` 等价于 `--check`，不会尝试下载）。

---

## 14. 清理或恢复前先确认会删什么

`clean cache`、`clean book` 与 `storage --clean` 都支持 `--dry-run`: 只报告路径与占用,
**不删除任何文件**, 并与真实删除共用同一套路径校验 (越界站点名、保护目录同样报错)。

```bash
novel-cli clean cache --site qidian --dry-run
novel-cli clean book --site qidian 1010868264 --all --dry-run
novel-cli storage --clean --dry-run
```

脚本消费时加 `--json` (必须与 `--dry-run` 同用), 输出单行
`{"dry_run", "command", "site", "book_id", "book_ids", "items", "paths", "files", "bytes"}`。

如果章节数据库已被误删, 可用备份恢复:

```bash
# 先备份所有章节数据库
novel-cli storage backup --out ~/novel-backups

# 只读校验 raw_data 里所有在用数据库 (不修改任何文件)
novel-cli storage verify

# 从备份恢复; 覆盖已有数据库需要 -y
novel-cli storage restore --from ~/novel-backups/<site>/<book>/chapter.raw.<时间戳>.sqlite -y
```

`restore` 会先校验备份文件 (完整性、schema 版本、必需列) 再原子替换; 覆盖已有数据库时
旧库会先保存为同目录的 `*.pre-restore-*.bak`。目标路径必须位于 `raw_data_dir` 内。

备份只含 `raw_data` 下的章节数据库, 不含 Cookie/token、`settings.toml`、日志或媒体文件;
恢复后仍需重新登录, 且备份是明文, 请按敏感数据保管。完整的
[「备份 → 校验 (SHA-256 / `PRAGMA integrity_check`) → 恢复」流程](./cli-examples.md#9-storage)
见 CLI 用法示例。

定时备份时可用 `--keep` 自动轮转。先预览会删哪些旧备份，确认后再真正执行：

```bash
# 只报告会删除的旧备份，新备份仍会写入，零删除
novel-cli storage backup --out ~/novel-backups --keep 7 --dry-run

# 每本书、每个 stage 只保留最新 7 份
novel-cli storage backup --out ~/novel-backups --keep 7
```

`--keep` 只删除 `--out` 下严格匹配 `chapter.<stage>.<UTC 时间戳>.sqlite` 的文件，目录、符号链接、手工改名或旧版本留下的文件都不会被碰；本次运行刚写出的备份永远受保护。`--keep 0` 关闭清理，负数或以 `--dry-run` 单独使用会以用法错误 (`2`) 退出。清理失败时命令以 `1` 退出，但已完成的备份保留。

---

## 15. 搜索过滤后没有结果

**现象**：加了 `--min-words` / `--max-words` / `--updated-after`（或 API 的 `min_words` / `max_words` / `updated_after`）后，结果从很多变成 0，但书名明明存在。

**原因**：过滤只保留**能解析出对应字段**的行。启用字数过滤时，站点没返回 `word_count` 或格式无法解析的行会被丢弃；启用日期过滤时，没有 `update_date` 的行同样被丢弃。这是有意的「宁缺毋滥」，不是请求失败。

**验证与解决**：

1. 先去掉过滤，确认书本身能搜到：

   ```bash
   novel-cli search "书名" --site <site>
   ```

2. 逐项加上过滤，定位是哪一项把结果筛掉：

   ```bash
   # 先不带过滤
   novel-cli search "书名" --site <site>

   # 只加日期
   novel-cli search "书名" --site <site> --updated-after 2024-01-01

   # 只加字数
   novel-cli search "书名" --site <site> --min-words 300000
   ```

3. 注意单位与边界：字数是**按本**的数值（`"30万字"` 会解析为 300000），上下界都含边界；`--updated-after` 是**含当天**的下界，格式必须严格为 `YYYY-MM-DD`，否则命令以 `2` 退出而不是静默忽略。
4. 跨站合并 (`--group`) 与 `--limit` 在过滤之后执行，因此过滤会让最终行数变少是预期行为。

---

## 16. 仍有问题？

带上下面的信息提交 Issue：

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.platform()); print(platform.python_version()); print(novel_downloader.__version__)"
tail -n 200 logs/novel_downloader.log
```

同时说明：目标站点、执行的完整命令、`settings.toml` 中相关片段（请先删除账号、Cookie、token 等敏感信息），以及是否能稳定复现。