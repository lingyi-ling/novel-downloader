# 本地 REST API

除了命令行、桌面端和 Android 客户端，本项目还提供一个供其他软件调用的本地
REST API。它复用与桌面端完全相同的服务层（任务管理器、搜索插件、音乐下载
引擎），因此任务队列、导出目录和站点支持范围都保持一致。

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
| `--token` | 环境变量 `NOVEL_API_TOKEN` | 设置后所有 `/api/v1/*` 与 `/downloads/*` 请求都要鉴权。 |
| `--cors` | 关闭 | 允许任意来源跨域调用（`Access-Control-Allow-Origin: *`）。 |
| `--rate-limit` | `120` | 每个客户端 IP 在滑动窗口内允许的最大请求数；`0` 关闭限流。 |
| `--rate-limit-window` | `60` | 限流滑动窗口长度（秒）。 |

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

设置 token 后，每个 `/api/v1/*` 请求以及 `/downloads/*` 静态文件请求都必须
携带以下任意一种请求头：

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

所有业务端点前缀为 `/api/v1`，请求与响应通常为 JSON（`search/stream`、
`tasks/stream`、`login/requests/stream` 三个 SSE 端点除外）。
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
| `GET` | `/api/v1/login/requests` | 待处理的登录凭据请求列表。 |
| `GET` | `/api/v1/login/requests/stream` | 以 SSE 推送待处理的登录凭据请求。 |
| `POST` | `/api/v1/login/requests/{req_id}` | 提交凭据，完成一个登录请求。 |
| `GET` | `/api/v1/watchlist` | 追更列表。 |
| `POST` | `/api/v1/watchlist` | 追更一本书（重复添加只刷新条目）。 |
| `DELETE` | `/api/v1/watchlist/{site}/{book_id}` | 取消追更；不存在返回 `404`。 |
| `POST` | `/api/v1/watchlist/check` | 检查追更书籍的新章节，不启动下载。 |
| `GET` | `/api/v1/history` | 列出导出目录中的 `txt`/`epub` 文件。 |
| `GET` | `/api/v1/music/platforms` | 音乐平台列表与使用提示。 |
| `POST` | `/api/v1/music/search` | 在指定音乐平台搜索歌曲。 |
| `POST` | `/api/v1/music/download` | 同步下载一首或多首歌曲。 |
| `POST` | `/api/v1/music/jobs` | 创建异步音乐下载任务，返回 `202 {job_id}`。 |
| `GET` | `/api/v1/music/jobs/{job_id}` | 查询异步音乐任务状态与进度。 |
| `POST` | `/api/v1/music/export` | 把已下载曲目导出为 M3U8 歌单。 |
| `GET` | `/api/v1/music/health` | 平台连通性自检（单个或全部平台）。 |

Swagger UI（`/docs`）按 `system`、`search`、`tasks`、`login`、`watchlist`、
`history`、`music` 分组展示以上端点。

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

## 登录凭据请求

部分站点需要登录（账号密码或 Cookie）才能下载。当任务运行到站点登录步骤、
又没有可用的已保存 Cookie 时，会创建一条「登录凭据请求」并等待人工输入。
这些请求通过本组端点暴露给外部客户端——**任务卡在
`running`、长时间无进度时，轮询此端点提交凭据**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/login/requests` | 当前待处理的凭据请求列表。 |
| `GET` | `/api/v1/login/requests/stream` | SSE：有待处理请求时推送。 |
| `POST` | `/api/v1/login/requests/{req_id}` | 提交凭据，完成该请求。 |

### 何时产生

`POST /tasks` 排队的任务启动后（状态 `running`），如果站点要求登录且没有
有效 Cookie，就会创建凭据请求。**任务最多等待 120 秒**：

- 期间通过 `GET /login/requests` 找到对应请求、用
  `POST /login/requests/{req_id}` 提交凭据，任务立刻继续；
- 超过 120 秒无人提交，任务回退使用 `prefill` 预填值继续登录
  （通常随后登录失败，任务转为 `failed`）。

### 请求结构

```json
{
  "total": 1,
  "requests": [
    {
      "req_id": "b1e04f2c9d8e4f7c8b6a5d4e3c2b1a09",
      "task_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
      "title": "斗破苍穹",
      "fields": [
        {
          "name": "username",
          "label": "login.username.label",
          "type": "text",
          "required": true,
          "default": "",
          "placeholder": "",
          "description": ""
        },
        {
          "name": "password",
          "label": "login.password.label",
          "type": "password",
          "required": true,
          "default": "",
          "placeholder": "",
          "description": ""
        }
      ],
      "prefill": {"username": "someone"},
      "done": false
    }
  ]
}
```

- `fields[].type` 取值 `text` / `password` / `cookie`。`cookie` 字段提交
  原始 Cookie 字符串（如 `"key=value; other=value"`，也支持换行、逗号分隔），
  服务端会解析成键值对。
- `label` / `placeholder` / `description` 由站点插件提供，服务端**不做翻译
  也不做渲染**，按原样返回（可能是翻译键）；如何展示由客户端决定。
- `prefill` 是来自配置或上一次登录的预填值；提交的 `values` 按字段名覆盖它。

### 轮询-提交流程

```bash
# 任务卡住时，轮询待处理请求
curl -s "$BASE/api/v1/login/requests" -H "Authorization: Bearer $TOKEN"

# 提交凭据（req_id 取自上一步；未提交的字段沿用 prefill）
curl -s -X POST "$BASE/api/v1/login/requests/<req_id>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"values": {"username": "me", "password": "secret", "cookies": "token=abc; uid=42"}}'
# → {"ok": true}
```

提交成功后请求立即从列表消失，等待中的任务马上恢复执行。错误码：

| 状态码 | 场景 |
| --- | --- |
| `404` | `req_id` 不存在（提交成功后任务会取走结果并移除请求，重复提交也是 `404`）。 |
| `409` | 请求已完成但任务尚未取走结果时再次提交。 |
| `422` | `values` 不是 `{"字段名": "字符串"}` 形式的对象。 |

!!! note "只列待处理请求"
    `GET /login/requests` 只返回尚未完成的请求。不想轮询时可以订阅 SSE 流：

    ```bash
    curl -N "$BASE/api/v1/login/requests/stream" -H "Authorization: Bearer $TOKEN"
    ```

    服务端每秒轮询一次凭据队列：有待处理请求时推送 `requests` 事件
    （`data` 与 `GET /login/requests` 的 `requests` 数组同构）；队列清空时
    推送最后一个空数组事件，之后静默，直到出现新请求。客户端断开即停止。
    鉴权方式与其他 SSE 端点相同（`Authorization` / `X-API-Key` 请求头）。

## 追更

追更列表与 `novel-cli follow` 共用同一份 `watchlist.json`
（位于应用数据目录，可用 `NOVEL_HOME` 覆盖）。

`GET /api/v1/watchlist`：

```json
{
  "total": 1,
  "books": [
    {
      "site": "b520",
      "book_id": "8_8187",
      "title": "示例小说",
      "last_chapter": "第 120 章",
      "updated_at": "2026-01-01T00:00:00+00:00"
    }
  ]
}
```

`POST /api/v1/watchlist`：

```json
{"site": "b520", "book_id": "8_8187", "title": "示例小说"}
```

响应 `{"created": true, "entry": {...}}`。重复添加已存在的书籍返回
`created: false`；未提供 `title` 时保留原有标题和最新章节。

`DELETE /api/v1/watchlist/{site}/{book_id}` 成功返回 `{"ok": true}`，
该书未被追更时返回 `404`：

```json
{"detail": "Book not in watchlist: b520/8_8187"}
```

`POST /api/v1/watchlist/check`：

```json
{"download": false}
```

检查会针对每本书在其来源站点做一次单站搜索（与 `novel-cli follow check`
相同），把最新章节写回 `last_chapter`。**不会启动任何下载。** 响应：

```json
{
  "checked": 2,
  "updated": 1,
  "failed": 0,
  "download": false,
  "needs_download": [
    {
      "site": "b520",
      "book_id": "8_8187",
      "title": "示例小说",
      "previous_chapter": "第 119 章",
      "last_chapter": "第 120 章",
      "found": true,
      "changed": true,
      "needs_download": true,
      "error": null
    }
  ],
  "items": ["...每本书的结果..."]
}
```

- 单个站点搜索失败只影响该条目（`error` 非空，计入 `failed`），其余照常检查。
- `needs_download` 是需要下载的条目；`items` 包含所有条目的完整结果。
- `download=true` 只是在这些条目上附加可直接提交给 `POST /api/v1/tasks`
  的 `task` 对象，仍然不会在服务端排队下载。

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

### 导出歌单

`POST /api/v1/music/export` 把已经下载到本地的曲目写成 M3U8 歌单，不做任何
网络请求。每首曲目必须带本地 `path`（通常取自下载结果或 `music/download`
的返回值），没有 `path` 的曲目计入 `skipped`：

```json
{
  "platform": "netease",
  "tracks": [
    {
      "id": "186016",
      "name": "晴天",
      "artists": "周杰伦",
      "duration_s": 269,
      "path": "D:/Music/周杰伦/叶惠美/晴天 - 周杰伦.mp3"
    }
  ],
  "out_path": "D:/Music/playlists/favorites.m3u8"
}
```

- `out_path` 以 `.m3u` / `.m3u8` 结尾时视为文件路径；其他值视为目录，
  并在其中生成 `playlist-<UTC 时间戳>.m3u8`。
- 省略 `out_path` 时写入音乐设置的 `out_dir`（`music settings out_dir`）。
- `platform` 必须是 `/music/platforms` 返回的 key，否则返回 `400`。

响应：

```json
{"path": "D:/Music/playlists/favorites.m3u8", "tracks_written": 1, "skipped": 0}
```

### 平台健康检查

`GET /api/v1/music/health` 对每个平台做一次有界搜索（不解析直链、不下载）。
省略 `platform` 时并发检查全部已注册平台；`timeout` 为单个平台的预算
（默认 `5` 秒，范围 `1..60`）：

```json
[
  {"platform": "netease", "ok": true, "elapsed_ms": 812, "error": ""},
  {"platform": "qq", "ok": false, "elapsed_ms": 5003, "error": "搜索超时（>5s）"}
]
```

指定未知平台返回 `400`。

若未安装音乐可选依赖（`mutagen`、`qrcode`、`websocket-client` 等），
音乐端点返回 `503` 并附安装提示，而不是 `500`：

```json
{"detail": "Music support is not available: ... Install it with: pip install \"novel-downloader[music]\""}
```

## 任务完成回调（Webhook）

设置环境变量 `NOVEL_API_WEBHOOK` 后，任何下载任务进入终态
（`completed`、`cancelled`、`failed`）时，API 会向该 URL 发送：

```json
{
  "event": "task_finished",
  "task": {
    "task_id": "a1b2c3",
    "title": "示例小说",
    "site": "b520",
    "book_id": "8_8187",
    "status": "completed",
    "chapters_done": 120,
    "chapters_total": 120,
    "progress": 1.0,
    "error": null,
    "exported_paths": {"txt": "downloads/示例小说.txt"},
    "created_at": 1710000000.0,
    "finished_at": 1710000100.0
  }
}
```

```bash
NOVEL_API_WEBHOOK=https://example.invalid/hooks/novel novel-api
```

- 发送在后台守护线程中进行，单次超时 `5` 秒；失败（连接错误、超时、非 2xx）
  只写日志，不影响任务执行或 API 响应。
- 未配置（或显式传入 `create_app(webhook_url="")`）时不安装 watcher，也不做
  任何轮询。
- API 启动时已经结束的任务不会触发回调；每个任务只通知一次。
- 也可在代码中配置：`create_app(webhook_url="https://...")`。

## 请求限制与超时

为避免单个请求占用过多资源，API 对携带 JSON 请求体的端点设置了大小上限，
并对阻塞型端点设置了整体墙钟预算。

请求体上限（默认 `1 MiB`，超出返回 `413`）：

| 端点 | 请求体 | 列表长度 |
| --- | --- | --- |
| `POST /api/v1/search` | ≤ 1 MiB | — |
| `POST /api/v1/login/requests/{req_id}` | ≤ 1 MiB | — |
| `POST /api/v1/watchlist` | ≤ 1 MiB | — |
| `POST /api/v1/watchlist/check` | ≤ 1 MiB | — |
| `POST /api/v1/music/search` | ≤ 1 MiB | — |
| `POST /api/v1/music/download` | ≤ 1 MiB | `tracks` ≤ 200 |
| `POST /api/v1/music/export` | ≤ 1 MiB | `tracks` ≤ 200 |
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

### 请求频率限制

API 默认启用按客户端 IP 的滑动窗口限流：每个 IP 在 `--rate-limit-window`
（默认 `60` 秒）内最多 `--rate-limit`（默认 `120`）个请求，超出的请求返回
`429` 并附带 `Retry-After` 头（等待多少秒后可有空位）。`--rate-limit 0`
可完全关闭限流；`GET /api/v1/health` 不受限流影响，便于可用性监控。
计数发生在鉴权之前，token 暴力枚举同样会被节流；被拒绝的请求不计数，
客户端停止请求后窗口排空即自动恢复。限流状态保存在进程内存中，多 worker
部署时各进程独立计数。

## 错误码

错误响应统一为 `{"detail": "..."}`（`detail` 为英文，便于客户端判断）：

| 状态码 | 场景 |
| --- | --- |
| `400` | 未知音乐平台；音乐平台初始化失败；同步音乐下载失败；歌单写入失败。 |
| `401` | 已配置 token，但请求缺少或携带了错误的 token。 |
| `404` | 任务 ID 不存在；登录凭据请求 ID 不存在；音乐任务 ID 不存在；追更条目不存在；静态文件不存在。 |
| `409` | 登录凭据请求已完成、尚未被任务取走时再次提交。 |
| `413` | 请求体超过该端点的大小上限。 |
| `422` | 请求参数校验失败（缺少必填字段、数值越界、`tracks` 过多等）。 |
| `429` | 超出按客户端 IP 的请求频率限制，响应带 `Retry-After` 头。 |
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

# 3b. 任务需要登录时：轮询凭据请求并提交（见「登录凭据请求」章节）
curl -s "$BASE/api/v1/login/requests" -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$BASE/api/v1/login/requests/<req_id>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"values": {"username": "me", "password": "secret"}}'

# 4. 查看并下载导出文件（/downloads 同样受 token 保护）
curl -s "$BASE/api/v1/history" -H "Authorization: Bearer $TOKEN"
curl -s -OJ "$BASE/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt" \
  -H "Authorization: Bearer $TOKEN"
# 也可以使用 X-API-Key
curl -s -OJ "$BASE/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt" \
  -H "X-API-Key: $TOKEN"

# 5. 异步音乐下载与轮询
JOB=$(curl -s -X POST "$BASE/api/v1/music/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"platform":"netease","tracks":[{"id":"186016"}]}' \
  | sed -E 's/.*"job_id":"([^"]+)".*/\1/')
curl -s "$BASE/api/v1/music/jobs/$JOB" -H "Authorization: Bearer $TOKEN"
```

配置 token 后，`/downloads/*` 与 `/api/v1/*` 一样要求鉴权：缺少或错误的 token
返回 `401`，不会泄露导出文件是否存在。未配置 token 时（仅建议用于 localhost）
静态文件保持免鉴权。Android 客户端的历史文件下载通过 Android `DownloadManager`
发送 `X-API-Key` 请求头，因此无需额外配置。

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

登录凭据请求相关方法（任务需要登录时配合 `tasks()` 轮询使用）：

```python
import time

with NovelApiClient("http://127.0.0.1:8081", token="my-secret") as api:
    # 任务卡住时轮询待处理的凭据请求（120 秒内提交）
    pending = api.login_requests()
    while not pending["total"]:
        time.sleep(1)
        pending = api.login_requests()

    req = pending["requests"][0]
    values = {f["name"]: input(f"{f['label']}: ") for f in req["fields"]}
    api.submit_login(req["req_id"], values)   # → {"ok": True}
```

- `login_requests()` 返回 `GET /login/requests` 的完整结构（`total` 与
  `requests` 列表）。
- `submit_login(req_id, values)` 提交凭据并唤醒等待中的任务；`values`
  可省略（等价于空对象，所有字段沿用 `prefill`）。未知 `req_id` 抛出
  `ApiError`（HTTP 404），请求已完成时抛出 409。

追更与音乐相关方法：

```python
with NovelApiClient("http://127.0.0.1:8081", token="my-secret") as api:
    api.add_watch("b520", "8_8187", title="示例小说")
    print(api.watchlist()["books"])

    result = api.check_watchlist(download=True)
    for item in result["needs_download"]:
        # 需要时由调用方自己排队下载（check 本身不会下载）
        api.add_task(item["site"], item["book_id"], item.get("title"))

    playlist = api.music_export(
        "netease",
        [{"id": "186016", "name": "晴天", "path": "D:/Music/晴天.mp3"}],
        out_path="D:/Music/favorites.m3u8",
    )
    print(playlist["path"], playlist["tracks_written"], playlist["skipped"])

    for probe in api.music_health("netease"):
        print(probe["platform"], probe["ok"], probe["elapsed_ms"])
```

- `watchlist()` / `add_watch()` / `remove_watch()` 对应追更 CRUD。
- `check_watchlist(download=False)` 返回与 `POST /watchlist/check` 相同的结构。
- `music_export()` 与 `music_health()` 分别覆盖歌单导出和平台自检；
  `music_health()` 直接返回列表。

## 安全提示

- **默认只绑定 `127.0.0.1`**，不要随意改成 `0.0.0.0`。
- 通过非回环地址提供服务时，**务必设置 token**，并建议放在反向代理/防火墙之后。
- `--cors` 会允许任意网页跨域调用本 API。仅在本机使用时开启；切勿与公网监听、
  无 token 的组合一起使用。
- `/downloads` 是静态目录挂载，配置 token 后同样要求 `Authorization: Bearer`
  或 `X-API-Key`；未配置 token 时免鉴权（仅建议用于 localhost）。目录穿越
  路径（如 `..`、`%2e%2e`）会被拒绝并返回 `4xx`。
- API 具备添加下载任务的能力，等同于本机上的下载器权限，请像保护本地账号一样
  保护 token。
- 请求体大小、曲目数量与阻塞端点耗时都有上限（见「请求限制与超时」），
  超限分别返回 `413`、`422`、`504`。
