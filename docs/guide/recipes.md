## 常见任务配方

本页给出可以直接复制执行的常见任务配方。所有命令都假设 `novel-cli` 已在 `PATH` 中；可用 `where novel-cli` (Windows) 或 `which novel-cli` 确认。

配置查找顺序为 `--config` 指定文件 → 当前目录 `settings.toml` → 应用主目录 (`NOVEL_HOME` 或平台数据目录)。**计划任务/脚本一定要先 `cd` 到配置所在目录**，或使用接受 `--config` 的命令，否则会读到另一份配置。

---

### 1. 批量下载书单

#### 同一站点、多个书籍 ID

`download --site` 接受多个书籍 ID：

```bash
novel-cli download --site b520 8_8187 12345 23456
```

> `--start` / `--end` 只作用于**第一个** `book_id`。需要给每本书指定范围时，请分多条命令执行，或使用配置文件中的结构化 `book_ids`。

#### 通过配置文件维护书单（推荐）

```toml
# settings.toml
[sites.b520]
book_ids = ["8_8187", "12345", "23456"]

[sites.qidian]
book_ids = ["1010868264"]
```

```bash
# 省略 book_ids 时从配置文件读取
novel-cli download --site b520
novel-cli download --site qidian
```

#### 跨站点批量执行

Git Bash:

```bash
for site in b520 qidian n23qb; do
  novel-cli download --site "$site" || exit 1
done
```

Windows `cmd.exe`:

```bat
for %s in (b520 qidian n23qb) do novel-cli download --site %s
```

PowerShell:

```powershell
foreach ($site in 'b520', 'qidian', 'n23qb') {
    novel-cli download --site $site
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

#### 从 URL 列表逐行下载

URL 模式下一次只接受**一个** URL，因此用循环：

```bash
# books.txt 每行一个书籍 URL，空行会被跳过
while IFS= read -r url; do
  [ -z "$url" ] && continue
  novel-cli download "$url" || exit 1
done < books.txt
```

PowerShell:

```powershell
Get-Content .\books.txt | Where-Object { $_.Trim() } | ForEach-Object {
    novel-cli download $_
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

---

### 2. 只导出，不下载

`export` 只读取已经存在的原始数据 (`general.raw_data_dir`)，不会访问网络；书籍必须先下载过。

```bash
# 把已下载的书导出为 EPUB
novel-cli export --site b520 12345 --format epub

# 同时导出 TXT 和 EPUB
novel-cli export --site b520 12345 --format txt epub

# 从原始阶段导出（跳过 cleaner 处理）
novel-cli export --site b520 12345 --format epub --stage raw
```

* `--site` 和 `book_ids` 都给出时不会进入交互式选择，适合脚本。
* 输出目录为 `general.output_dir`（默认 `./downloads`）。
* `--start` / `--end` 仍然只作用于第一本书。
* 如果没有对应的原始数据，命令以退出码 `1` 结束。

省略 `--site` 或 `book_ids` 时会进入交互模式；非交互环境请显式提供两者。

---

### 3. 定时追更（Windows 计划任务）

思路：先用 `follow` 维护书单，再让计划任务定期执行 `follow check` 并写日志。

#### 第一步：加入追更列表

```bat
novel-cli follow add b520 8_8187 --title "示例小说"
novel-cli follow add qidian 1010868264 --title "另一本书"
novel-cli follow list
```

`--title` 很重要：`follow check` 会用它到站点搜索最新章节。省略标题时只能拿书籍 ID 当关键字，命中率低。

#### 第二步：创建批处理脚本

新建 `C:\novel\follow-check.cmd`（路径不要带空格）：

```bat
@echo off
setlocal
cd /d "C:\novel"

rem 用 where novel-cli 查到的实际路径替换下面这一行
set "CLI=C:\Users\me\AppData\Local\Programs\Python\Python312\Scripts\novel-cli.exe"
set "LOG=C:\novel\follow.log"

echo [%DATE% %TIME%] follow check >> "%LOG%"
"%CLI%" follow check --all >> "%LOG%" 2>&1

rem 可选：断点续传式更新指定书籍，只抓取缺失/新章节
"%CLI%" download --site b520 8_8187 >> "%LOG%" 2>&1

rem 或者：让 CLI 自动逐本下载有新章节的书并等待完成
rem "%CLI%" follow check --download --wait --wait-timeout 1800 >> "%LOG%" 2>&1

exit /b %ERRORLEVEL%
```

注意：

* `cd /d "C:\novel"` 让 `settings.toml`、`downloads`、`raw_data`、`logs` 都解析到预期位置。
* `follow check --download` 默认只**打印**下载命令，不会自行下载。需要自动更新时，可以像上面那样显式写 `download` 命令，也可以在计划任务里改用 `follow check --download --wait`：CLI 会把有新章节的书逐本排队下载并等待完成。`--wait-timeout`（默认 1800 秒）限定每本的等待上限，超时的任务会被取消并在汇总中计为「已取消」，不影响后续书籍。
* `follow check` 只要有任意一本书检查失败就以退出码 `1` 结束，但脚本会继续执行后面的下载命令；使用 `--wait` 时，只要有下载未成功完成（失败/取消/超时），退出码同样是 `1`。最终退出码可在 `follow.log` 中看到。

#### 第三步：注册计划任务

在**管理员**命令提示符中执行：

```bat
schtasks /Create /TN "NovelFollowCheck" /SC DAILY /ST 08:00 /TR "C:\novel\follow-check.cmd" /F
```

每 6 小时执行一次：

```bat
schtasks /Create /TN "NovelFollowCheck" /SC HOURLY /MO 6 /TR "C:\novel\follow-check.cmd" /F
```

立即运行一次、查看状态或删除任务：

```bat
schtasks /Run /TN "NovelFollowCheck"
schtasks /Query /TN "NovelFollowCheck" /V /FO LIST
schtasks /Delete /TN "NovelFollowCheck" /F
```

> 默认计划任务只在用户登录时运行。需要“无论用户是否登录都运行”时，在“任务计划程序”图形界面中修改该任务的常规选项并保存凭据。
>
> 运行结果写入 `C:\novel\follow.log`；计划任务本身不会弹出窗口。

---

### 4. 桌面端一键追更

不想配置计划任务时, 可用桌面客户端 (`novel-desktop`) 手动追更。追更列表与 `novel-cli follow` 共用同一份 `watchlist.json`, 两边添加的条目互通。

1. 启动 `novel-desktop`, 在「搜索」页签搜到书籍后选中并点「追更」; 也可在「追更」页签点「添加」, 手动录入站点与书籍 ID (建议同时填写标题, 供检查更新时搜索)。
2. 在「追更」页签点「检查更新」, 桌面端会逐本搜索最新章节并刷新列表。
3. 发现新章节后, 选中条目点「下载所选」加入下载队列; 未选中任何条目时会下载全部追更书籍。下载进度与导出文件分别在「下载」「历史」页签查看。

> 桌面端的音乐页签另有「检查平台」健康检查, 可一键探测各音乐平台是否可用。

---

### 5. Android 连接电脑

Android 客户端通过本地 REST API 访问电脑端，电脑与手机必须在同一局域网。

#### 第一步：启动 API 并监听局域网

```bash
novel-api --host 0.0.0.0 --port 8081 --token "change-me"
```

> `--host 0.0.0.0` 会监听所有网卡。未设置 token 时，同一局域网内任何人都能访问导出目录，请务必设置 token。

#### 第二步：放行防火墙端口

以管理员身份打开命令提示符：

```bat
netsh advfirewall firewall add rule name="Novel API 8081" dir=in action=allow protocol=TCP localport=8081
```

删除规则：

```bat
netsh advfirewall firewall delete rule name="Novel API 8081"
```

#### 第三步：查找电脑的局域网 IP

```bat
ipconfig
```

在输出中找到当前网卡的 `IPv4 地址`（例如 `192.168.1.23`）。也可用 PowerShell：

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' } |
  Select-Object InterfaceAlias, IPAddress
```

#### 第四步：在 App 中填写地址

在 App 右上角“设置”中填写：

| 场景 | API 地址 |
| --- | --- |
| 模拟器访问电脑 | `http://10.0.2.2:8081/` |
| 真机访问电脑 | `http://192.168.1.23:8081/` |

如果启动 API 时设置了 token，把同一个值填入“API 密钥”。

#### 第五步：验证

在电脑本机：

```bash
curl http://127.0.0.1:8081/api/v1/health
```

在手机自带浏览器中打开 `http://192.168.1.23:8081/docs`，能看到接口文档即表示连通。

---

### 6. 用 API 集成到自己的程序

API 依赖可选组 `api`：

```bash
pip install "novel-downloader[api]"
novel-api --port 8081 --token "my-secret"
```

未设置 token 时可省略下面的 `Authorization` 请求头（仅建议在 `127.0.0.1` 上这样做）。

#### 搜索 → 建任务 → 轮询 → 取文件（curl）

```bash
BASE=http://127.0.0.1:8081
TOKEN=my-secret
AUTH="Authorization: Bearer $TOKEN"

# 1. 搜索
curl -s -X POST "$BASE/api/v1/search" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"keyword":"斗破苍穹","sites":["qidian"],"per_site_limit":5}'

# 2. 新增下载任务（响应里有 task_id）
curl -s -X POST "$BASE/api/v1/tasks" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"title":"斗破苍穹","site":"qidian","book_id":"123456"}'

# 3. 查询任务列表或单个任务
curl -s "$BASE/api/v1/tasks" -H "$AUTH"
curl -s "$BASE/api/v1/tasks/<task_id>" -H "$AUTH"

# 4. 列出导出文件并下载
curl -s "$BASE/api/v1/history" -H "$AUTH"
curl -OJ "$BASE/downloads/斗破苍穹.txt"
```

#### Python 示例（轮询到任务结束）

```python
import time

import requests

BASE = "http://127.0.0.1:8081"
HEADERS = {"Authorization": "Bearer my-secret"}

# 搜索并选第一条结果
resp = requests.post(
    f"{BASE}/api/v1/search",
    headers=HEADERS,
    json={"keyword": "斗破苍穹", "sites": ["qidian"], "per_site_limit": 5},
    timeout=30,
)
resp.raise_for_status()
best = resp.json()["results"][0]

# 入队
resp = requests.post(
    f"{BASE}/api/v1/tasks",
    headers=HEADERS,
    json={"title": best["title"], "site": best["site"], "book_id": best["book_id"]},
    timeout=30,
)
resp.raise_for_status()
task_id = resp.json()["task_id"]

# 轮询
while True:
    snapshot = requests.get(f"{BASE}/api/v1/tasks", headers=HEADERS, timeout=30).json()
    tasks = snapshot["pending"] + snapshot["running"] + snapshot["completed"]
    task = next((item for item in tasks if item["task_id"] == task_id), None)
    if task is None:
        raise RuntimeError("task disappeared")
    if task["status"] in {"completed", "failed", "cancelled"}:
        break
    time.sleep(2)

print(task["status"], task["progress"], task["exported_paths"])
```

任务对象字段：`task_id`、`title`、`site`、`book_id`、`status`、`chapters_done`、`chapters_total`、`progress`、`error`、`exported_paths`、`created_at`、`finished_at`。`status` 取值包括 `queued`、`running`、`processing`、`exporting`、`completed`、`cancelled`、`failed`。

#### 追更接口

```bash
# 加入追更列表
curl -s -X POST "$BASE/api/v1/watchlist" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"site":"b520","book_id":"8_8187","title":"示例小说"}'

# 检查新章节；download=true 时对有更新的条目返回可直接 POST 到 /tasks 的 task 请求体
curl -s -X POST "$BASE/api/v1/watchlist/check" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"download":true}'
```

* `GET /api/v1/watchlist` 列出全部追更条目。
* `DELETE /api/v1/watchlist/{site}/{book_id}` 移除条目。
* `/watchlist/check` 只检查、不下载；`needs_download` 中的条目带有 `task` 字段，由调用方自行提交。
* 需要实时进度时可用 SSE：`GET /api/v1/tasks/stream`；搜索流为 `GET /api/v1/search/stream`。SSE 需要自定义请求头，请用支持设置请求头的客户端。

`/downloads/*` 与 `/api/v1/*` 一样受 token 保护（设置 token 时需带同样的请求头）；对外暴露 API 时仍建议配合防火墙限制来源。

---

### 7. 常见问题 (FAQ)

**`novel-cli: command not found` / `'novel-cli' 不是内部或外部命令`**

```bash
where novel-cli
```

若没有输出，找到 Python 的 `Scripts` 目录（例如 `C:\Users\me\AppData\Local\Programs\Python\Python312\Scripts`）并加入 `PATH`，或直接用绝对路径调用 `novel-cli.exe`。

**计划任务运行了但没有效果**

检查 `follow.log`；确认脚本里有 `cd /d` 到配置目录、`CLI` 使用的是 `where novel-cli` 得到的绝对路径，并在“任务计划程序”中查看“上次运行结果”。

**`follow check` 总是找不到书籍**

给条目补上 `--title`：

```bash
novel-cli follow add b520 8_8187 --title "准确书名"
```

需要登录的站点先执行 `novel-cli login <site>`。

**配置加载时提示 `cookie` 或 `use_truncation` 告警**

参见 [配置说明](./configuration.md) 的“旧配置迁移”和 [设置项参考](./settings-reference.md)。`cookie` 应改为 `cookies`，`use_truncation` 应移到 `[sites.<site>.parser]`。

**频繁出现 403 / 503**

降低 `general.max_rps`、增大 `general.request_interval`，或切换 HTTP 后端。详见 [设置项参考](./settings-reference.md)。

**导出文件在哪里**

默认是运行目录下的 `./downloads`，可通过 `general.output_dir` 修改；`novel-cli storage` 会显示实际路径。