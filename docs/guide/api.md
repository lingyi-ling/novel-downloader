# 本地 REST API

除了命令行、Web UI 和桌面端，本项目还提供一个供其他软件调用的本地 REST
API。它复用与 Web UI 完全相同的服务层（任务管理器、搜索插件、音乐下载引擎），
因此任务队列、导出目录和站点支持范围都保持一致。

API 由 FastAPI 实现，默认监听 `127.0.0.1:8081`，只应在本机使用。

## 安装与启动

API 依赖是可选组 `api`：

```bash
pip install "novel-downloader[api]"
```

安装后可直接使用入口脚本：

```bash
novel-api
# 等价于 uvicorn novel_downloader.apps.api.app:fastapi_app --host 127.0.0.1 --port 8081
```

常用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址。除非配置 token，否则不要改成 `0.0.0.0`。 |
| `--port` | `8081` | 监听端口。 |
| `--token` | 环境变量 `NOVEL_API_TOKEN` | 设置后所有 `/api/v1/*` 请求都要鉴权。 |
| `--cors` | 关闭 | 允许任意来源跨域调用（`Access-Control-Allow-Origin: *`）。 |

```bash
# 带 token 启动
novel-api --host 127.0.0.1 --port 8081 --token my-secret

# 或使用环境变量
NOVEL_API_TOKEN=my-secret novel-api
```

若 `--host` 不是回环地址（`127.0.0.1`/`localhost`/`::1`）且未设置 token，
启动时会向 stderr 打印显著警告：此时任何能访问该端口的人都可以控制下载并
读取 `/downloads` 下的导出文件。在非本地监听上同时使用 `--cors` 会额外告警。

启动后：

- Swagger UI：<http://127.0.0.1:8081/docs>
- OpenAPI JSON：<http://127.0.0.1:8081/openapi.json>
- 导出文件静态目录：`/downloads/<文件名>`（对应配置 `general.output_dir`）

## 鉴权

未设置 token 时不校验，适用于仅绑定 localhost 的场景。

设置 token 后，每个 `/api/v1/*` 请求必须携带以下任意一种请求头：

```http
Authorization: Bearer my-secret
```

```http
X-API-Key: my-secret
```

缺失或错误返回 `401`：

```json
{"detail": "Invalid or missing API token"}
```

## 端点一览

所有业务端点前缀为 `/api/v1`，请求与响应通常为 JSON（`search/stream` 与
`tasks/stream` 两个 SSE 端点除外）。
下文示例中的 `$BASE` 指 API 根地址（如 `http://127.0.0.1:8081`），
`$TOKEN` 指启动时配置的 token。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 健康检查，返回 `{status, version}`。 |
| `GET` | `/api/v1/sites` | 搜索/下载支持的站点，返回 `{search: {...}, download: {...}}`。 |
| `POST` | `/api/v1/search` | 聚合搜索多个站点。 |
| `GET` | `/api/v1/search/stream` | 以 SSE 流式返回各站点搜索结果。 |
| `GET` | `/api/v1/tasks` | 任务快照：`{pending, running, completed}`，支持分页。 |
| `GET` | `/api/v1/tasks/stream` | 以 SSE 每秒推送一次任务快照。 |
| `GET` | `/api/v1/tasks/{task_id}` | 查询单个任务；不存在返回 `404`。 |
| `POST` | `/api/v1/tasks` | 新增下载任务。 |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | 取消待执行或运行中的任务。 |
| `GET` | `/api/v1/history` | 列出导出目录中的 `txt`/`epub` 文件。 |
| `GET` | `/api/v1/music/platforms` | 音乐平台列表与使用提示。 |
| `POST` | `/api/v1/music/search` | 在指定音乐平台搜索歌曲。 |
| `POST` | `/api/v1/music/download` | 同步下载一首或多首歌曲。 |
| `POST` | `/api/v1/music/jobs` | 创建异步音乐下载任务，返回 `202 {job_id}`。 |
| `GET` | `/api/v1/music/jobs/{job_id}` | 查询异步音乐任务状态与进度。 |

Swagger UI（`/docs`）按 `system`、`search`、`tasks`、`history`、`music`
分组展示以上端点。

任务对象字段：

`task_id`、`title`、`site`、`book_id`、`status`、`chapters_done`、
`chapters_total`、`progress`、`error`、`exported_paths`、`created_at`、
`finished_at`。

`status` 取值：`queued`、`running`、`processing`、`exporting`、
`completed`、`cancelled`、`failed`。

## 搜索

请求体：

```json
{
  "keyword": "斗破苍穹",
  "sites": ["qidian", "biquge5"],
  "per_site_limit": 5,
  "timeout": 5.0,
  "sort": "quality",
  "order": "desc",
  "limit": 20
}
```

- `keyword` 必填。
- `sites` 省略时搜索全部启用站点；留空数组则不搜索任何站点。
- `per_site_limit` 为每个站点的结果上限，默认 `5`。
- `timeout` 为单个站点的请求预算（秒）。
- `sort` 可选 `quality`、`priority`、`site`、`title`、`author`、
  `words`、`update`。
- `limit` 为合并后的总结果上限。

响应：

```json
{
  "total": 2,
  "results": [
    {
      "site": "qidian",
      "book_id": "123456",
      "book_url": "https://...",
      "cover_url": "https://...",
      "title": "斗破苍穹",
      "author": "天蚕土豆",
      "latest_chapter": "第 1 章",
      "update_date": "2024-01-01",
      "word_count": "100万字",
      "priority": 1
    }
  ]
}
```

## 流式搜索（SSE）

`GET /api/v1/search/stream` 在搜索过程中逐站点推送结果，适合边搜边展示。
查询参数与 `POST /search` 的请求体对应：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `keyword` | string | 必填 | 搜索关键词。 |
| `sites` | string | 全部启用站点 | 逗号分隔的站点列表，如 `qidian,biquge5`。 |
| `per_site_limit` | int | `5` | 每个站点的结果上限。 |
| `timeout` | float | `5` | 单个站点的请求预算（秒）。 |

响应为 `text/event-stream`，事件格式：

```
event: results
data: [{"site":"qidian","book_id":"123456","title":"斗破苍穹", ...}]

event: results
data: [{"site":"biquge5","book_id":"...","title":"...", ...}]

event: done
data: {"total":2}
```

- 每个站点完成时推送一个 `results` 事件，`data` 是该站点的结果数组。
- 全部站点结束后推送一个 `done` 事件，`data.total` 为本次推送的总条数。
- 客户端断开连接时，服务端会关闭底层搜索生成器并取消仍在进行的站点请求。
- 响应头包含 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`，
  使用反向代理时也不会被缓冲。

用 `curl -N` 观察实时事件流：

```bash
curl -N "http://127.0.0.1:8081/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian,biquge5&per_site_limit=5&timeout=5" \
  -H "Authorization: Bearer $TOKEN"
```

!!! note "鉴权与客户端选择"
    SSE 端点同样受 token 保护，只能通过 `Authorization` 或 `X-API-Key`
    请求头传递。浏览器的 `EventSource` 无法自定义请求头，因此请改用
    `fetch` 读取响应流，或使用 `curl` 等可设置请求头的客户端。

## 下载任务

新增任务：

```json
{
  "title": "斗破苍穹",
  "site": "qidian",
  "book_id": "123456"
}
```

`title` 可省略，此时自动生成 `<site> (id = <book_id>)`。

### 分页

`GET /api/v1/tasks` 支持 `offset` / `limit` 查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `offset` | int | `0` | 跳过的任务数，必须 `>= 0`。 |
| `limit` | int | `50` | 返回的任务数，范围 `1..200`；超过 `200` 返回 `422`。 |

分页作用于 `pending + running + completed` 的拼接顺序（各组内部顺序不变），
响应仍是按组划分的三个数组：

```json
{
  "pending": [{"task_id": "a", "...": "..."}],
  "running": [],
  "completed": [{"task_id": "b", "...": "..."}],
  "total": 6,
  "offset": 1,
  "limit": 3
}
```

- `total` 是分页前的任务总数，`offset` / `limit` 回显实际生效的值。
- **兼容旧客户端**：`offset` 和 `limit` 都不传时不做分页，返回全部任务
  （此时响应中的 `limit` 等于 `total`），三个数组字段与旧版本完全一致。
- 只传其中一个时，另一个取默认值（`offset=0`、`limit=50`）。
- `offset` 超出总数时返回空的三个数组，`total` 仍为真实总数。

```bash
# 第 2 页（每页 20 条）
curl -s "$BASE/api/v1/tasks?offset=20&limit=20" -H "Authorization: Bearer $TOKEN"
```

### 任务事件流（SSE）

`GET /api/v1/tasks/stream` 立即推送一次任务快照，之后每秒推送一次：

```bash
curl -N "$BASE/api/v1/tasks/stream" -H "Authorization: Bearer $TOKEN"
```

```
event: snapshot
data: {"pending":[],"running":[{"task_id":"a","status":"running", ...}],"completed":[]}

event: snapshot
data: {"pending":[],"running":[],"completed":[{"task_id":"a","status":"completed", ...}]}
```

- 每个事件的 `data` 与 `GET /api/v1/tasks` 同结构（`pending` / `running` /
  `completed`），是**全量快照**而不是增量补丁，客户端可直接替换本地状态。
- 该流不分页；需要分页时请轮询 `GET /api/v1/tasks`。
- Stream 的生命周期与请求一致：客户端断开后服务端立即停止轮询并释放资源，
  不会留下后台任务。因此没有 `Last-Event-ID` 重放，也不跨请求共享状态。
- 响应头包含 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`。
- 鉴权方式与搜索流相同：通过 `Authorization: Bearer` 或 `X-API-Key`
  请求头传递，因此浏览器端需使用 `fetch` 读取流，不能直接用 `EventSource`。

查询单个任务：

```bash
curl -s "$BASE/api/v1/tasks/<task_id>" -H "Authorization: Bearer $TOKEN"
```

返回与任务列表中相同的任务对象；任务不存在返回 `404`。

取消任务成功返回 `{"ok": true}`；任务不存在返回 `404`。

## 导出文件

```json
{
  "total": 1,
  "files": [
    {
      "name": "斗破苍穹.txt",
      "ext": "txt",
      "size": 1048576,
      "mtime": 1710000000.0,
      "download_url": "/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt"
    }
  ]
}
```

`download_url` 是相对于 API 根地址的路径，直接拼接
`http://127.0.0.1:8081` 即可下载。

`GET /api/v1/history` 同样支持 `offset` / `limit`（默认 `0` / `50`，上限
`200`），排序（按修改时间倒序）在分页之前完成：

```json
{
  "total": 120,
  "offset": 20,
  "limit": 20,
  "files": [{"name": "...", "ext": "txt", "size": 1, "mtime": 1.0, "download_url": "/downloads/..."}]
}
```

- `total` 始终是分页前的文件总数。
- 两个参数都不传时返回全部文件（兼容旧行为，此时 `limit == total`）。
- 只传一个时另一个取默认值：`offset=0`、`limit=50`。
- `limit` 超过 `200` 或小于 `1`、`offset` 为负数时返回 `422`。

## 音乐

`GET /api/v1/music/platforms`：

```json
{
  "platforms": [
    {"key": "netease", "label": "国内 · 网易云音乐", "note": ""}
  ]
}
```

`POST /api/v1/music/search`：

```json
{"platform": "netease", "keyword": "晴天", "limit": 20}
```

响应中的 `tracks` 使用统一的 Track 结构，可直接原样传给下载端点。

`POST /api/v1/music/download`：

```json
{
  "platform": "netease",
  "tracks": [{"id": "186016", "name": "晴天", "artists": "周杰伦"}],
  "level": "exhigh",
  "out_dir": "D:/Music"
}
```

`level` 可选 `hires`、`lossless`、`exhigh`、`higher`、`standard`；
`out_dir` 省略时使用音乐模块的默认下载目录。单次请求最多提交 `200` 首曲目
（`tracks` 超限返回 `422`）。

响应：

```json
{
  "ok": 1,
  "skip": 0,
  "fail": 0,
  "results": [
    {"status": "ok", "reason": "", "path": "D:/Music/...", "bytes": 8345678}
  ]
}
```

!!! warning "阻塞式请求"
    音乐引擎是同步实现，`/music/download` 会等待所有曲目下载完成才返回。
    批量下载时请控制单次请求的曲目数量，或拆成多个请求。

### 异步下载任务

批量较大时建议使用异步任务端点。`POST /api/v1/music/jobs` 立即返回
`202 Accepted` 和任务 ID，下载在后台线程池中执行：

```bash
# 创建任务（请求体与 /music/download 相同）
curl -s -X POST "$BASE/api/v1/music/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "platform": "netease",
        "tracks": [{"id": "186016", "name": "晴天", "artists": "周杰伦"}],
        "level": "exhigh",
        "out_dir": "D:/Music"
      }'
```

响应：

```json
{"job_id": "3f2c1b0a9d8e4f7c8b6a5d4e3c2b1a09"}
```

轮询任务状态：

```bash
curl -s "$BASE/api/v1/music/jobs/<job_id>" -H "Authorization: Bearer $TOKEN"
```

```json
{
  "job_id": "3f2c1b0a9d8e4f7c8b6a5d4e3c2b1a09",
  "status": "running",
  "progress": {"done": 1, "total": 3},
  "results": [
    {"status": "ok", "reason": "", "path": "D:/Music/晴天.mp3", "bytes": 8345678}
  ],
  "error": null,
  "created_at": 1710000000.0,
  "finished_at": null
}
```

- `status` 取值：`queued`、`running`、`done`、`failed`。
- `progress.done` 随已完成曲目递增，`progress.total` 为提交的曲目数。
- `results` 与同步端点相同，按提交顺序排列；任务失败时包含已完成的部分结果。
- `error` 仅在 `status=failed` 时非空。
- 任务表保存在进程内存中，最多保留最近 `100` 个任务（优先清理已结束的任务）；
  进程重启后任务记录丢失。
- 任务不存在返回 `404`。

!!! note "单进程内存状态"
    异步任务不跨进程共享。多 worker 部署 uvicorn 时，请将 `POST /music/jobs`
    与后续 `GET /music/jobs/{job_id}` 请求路由到同一进程，或改用
    `POST /music/download` 同步端点。

若未安装音乐可选依赖（`mutagen`、`qrcode`、`websocket-client` 等），
音乐端点返回 `503` 并附安装提示，而不是 `500`：

```json
{"detail": "Music support is not available: ... Install it with: pip install \"novel-downloader[music]\""}
```

## 请求限制与超时

为避免单个请求占用过多资源，API 对携带 JSON 请求体的端点设置了大小上限，
并对阻塞型端点设置了整体墙钟预算。

请求体上限（默认 `1 MiB`，超出返回 `413`）：

| 端点 | 请求体 | 列表长度 |
| --- | --- | --- |
| `POST /api/v1/search` | ≤ 1 MiB | — |
| `POST /api/v1/music/search` | ≤ 1 MiB | — |
| `POST /api/v1/music/download` | ≤ 1 MiB | `tracks` ≤ 200 |
| `POST /api/v1/music/jobs` | ≤ 1 MiB | `tracks` ≤ 200 |

`tracks` 超过 `200` 首返回 `422`，`detail` 会指明数量上限。

阻塞型端点的默认超时预算（超出返回 `504`）：

| 端点 | 默认预算 | 说明 |
| --- | --- | --- |
| `POST /api/v1/search` | `120` 秒 | 在每个站点的 `timeout` 之上再加的总预算 |
| `POST /api/v1/music/search` | `120` 秒 | 音乐平台搜索 |
| `POST /api/v1/music/download` | `1800` 秒 | 同步下载全部曲目 |
| `POST /api/v1/music/jobs` | `1800` 秒 | 后台任务超过预算会被标记为 `failed` |

超时只结束**请求的等待**：底层阻塞线程无法被中断，会在后台继续运行到结束。
如需调整默认值，可用 `create_app(timeouts={...})` 覆盖（主要供测试使用）：

```python
app = create_app(timeouts={"search": 30.0, "music_download": 600.0})
```

## 错误码

错误响应统一为 `{"detail": "..."}`（`detail` 为英文，便于客户端判断）：

| 状态码 | 场景 |
| --- | --- |
| `400` | 未知音乐平台；音乐平台初始化失败；同步音乐下载失败。 |
| `401` | 已配置 token，但请求缺少或携带了错误的 token。 |
| `404` | 任务 ID 不存在；音乐任务 ID 不存在；静态文件不存在。 |
| `413` | 请求体超过该端点的大小上限。 |
| `422` | 请求参数校验失败（缺少必填字段、数值越界、`tracks` 过多等）。 |
| `500` | 服务端内部错误，固定返回 `{"detail": "Internal server error"}`，不泄漏堆栈。 |
| `503` | 未安装音乐可选依赖，音乐相关端点不可用。 |
| `504` | 阻塞型端点超过超时预算。 |

校验失败（`422`）时 `detail` 为一句话描述，另有 `errors` 提供原始校验条目：

```json
{
  "detail": "Request validation failed: keyword: Field required",
  "errors": [
    {"type": "missing", "loc": ["body", "keyword"], "msg": "Field required"}
  ]
}
```

## 完整调用示例

以下示例假设已设置 `TOKEN=my-secret`。

```bash
BASE=http://127.0.0.1:8081
TOKEN=my-secret

# 1. 搜索
curl -s -X POST "$BASE/api/v1/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keyword":"斗破苍穹","sites":["qidian"],"per_site_limit":5}'

# 1b. 流式搜索（实时输出 SSE 事件）
curl -N "$BASE/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian,biquge5" \
  -H "Authorization: Bearer $TOKEN"

# 2. 新增下载任务
curl -s -X POST "$BASE/api/v1/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"斗破苍穹","site":"qidian","book_id":"123456"}'

# 3. 查询进度（列表或单个任务）
curl -s "$BASE/api/v1/tasks" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/api/v1/tasks/<task_id>" -H "Authorization: Bearer $TOKEN"

# 4. 查看并下载导出文件
curl -s "$BASE/api/v1/history" -H "Authorization: Bearer $TOKEN"
curl -s -OJ "$BASE/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt"

# 5. 异步音乐下载与轮询
JOB=$(curl -s -X POST "$BASE/api/v1/music/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"platform":"netease","tracks":[{"id":"186016"}]}' \
  | sed -E 's/.*"job_id":"([^"]+)".*/\1/')
curl -s "$BASE/api/v1/music/jobs/$JOB" -H "Authorization: Bearer $TOKEN"
```

`/downloads/*` 不受 token 保护（它是静态文件挂载），因此在公网暴露 API 时，
请自行限制对导出目录的访问。

## 在程序内直接使用

```python
from novel_downloader.apps.api import create_app

app = create_app(token="my-secret", cors=False)

# 交给任意 ASGI 服务器，例如：
# uvicorn my_module:app --host 127.0.0.1 --port 8081
```

也可以只导入工厂函数做集成测试：

```python
import asyncio
import httpx

from novel_downloader.apps.api import create_app

async def main() -> None:
    app = create_app(output_dir="./downloads")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print((await client.get("/api/v1/health")).json())

asyncio.run(main())
```

## Python SDK

`novel_downloader.apps.api.client.NovelApiClient` 是同步客户端，覆盖全部端点。
分页与任务流相关的方法：

```python
from novel_downloader.apps.api.client import NovelApiClient

with NovelApiClient("http://127.0.0.1:8081", token="my-secret") as api:
    # 分页（默认 offset=0、limit=50，服务端上限 200）
    first = api.tasks_page(offset=0, limit=20)
    print(first["total"], len(first["completed"]))

    files = api.history_page(offset=0, limit=20)
    print(files["total"], [item["name"] for item in files["files"]])

    # 逐事件消费任务流：每个快照都是 /tasks 的结构
    for snapshot in api.iter_tasks():
        print(len(snapshot["pending"]), len(snapshot["running"]))
        if not snapshot["pending"] and not snapshot["running"]:
            break          # 退出循环即断开连接
```

- 既有的 `tasks()` / `history()` 保持不变，仍然返回完整列表。
- `tasks_page()` / `history_page()` 直接把 `offset` / `limit` 作为查询参数发送，
  越界（负数、`limit=0`、`limit>200`）由服务端返回 `422`，客户端抛出
  `ApiError`。
- `iter_tasks()` 使用 `httpx` 的流式请求解析 SSE；只产出 `snapshot` 事件
  的 `data`（已从 JSON 解码）。生成器被关闭或循环 `break` 时连接随即关闭。
  传输错误与非 2xx 响应抛出 `ApiError`。

## 安全提示

- **默认只绑定 `127.0.0.1`**，不要随意改成 `0.0.0.0`。
- 通过非回环地址提供服务时，**务必设置 token**，并建议放在反向代理/防火墙之后。
- `--cors` 会允许任意网页跨域调用本 API。仅在本机使用时开启；切勿与公网监听、
  无 token 的组合一起使用。
- `/downloads` 是静态目录挂载，不带鉴权，暴露的只是导出文件本身；目录穿越
  路径（如 `..`、`%2e%2e`）会被拒绝并返回 `404`。
- API 具备添加下载任务的能力，等同于本机上的下载器权限，请像保护本地账号一样
  保护 token。
- 请求体大小、曲目数量与阻塞端点耗时都有上限（见「请求限制与超时」），
  超限分别返回 `413`、`422`、`504`。
