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
python -c "import importlib.util as u; print('httpx', bool(u.find_spec('httpx'))); print('curl_cffi', bool(u.find_spec('curl_cffi')))"
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

   * Apple Music / SoundCloud 需要 `pywidevine`。
   * YouTube 需要 `ytmusicapi` 和可执行的 Node（`node` 或 `nodejs`）。
   * 部分 HLS/流媒体格式需要系统安装 `ffmpeg`。

   检查：

   ```bash
   python -c "import importlib.util as u, shutil; print('pywidevine', bool(u.find_spec('pywidevine'))); print('ytmusicapi', bool(u.find_spec('ytmusicapi'))); print('node', shutil.which('node') or shutil.which('nodejs')); print('ffmpeg', shutil.which('ffmpeg'))"
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

## 8. 仍有问题？

带上下面的信息提交 Issue：

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.platform()); print(platform.python_version()); print(novel_downloader.__version__)"
tail -n 200 logs/novel_downloader.log
```

同时说明：目标站点、执行的完整命令、`settings.toml` 中相关片段（请先删除账号、Cookie、token 等敏感信息），以及是否能稳定复现。