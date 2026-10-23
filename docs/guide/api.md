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

所有业务端点前缀为 `/api/v1`，请求与响应均为 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 健康检查，返回 `{status, version}`。 |
| `GET` | `/api/v1/sites` | 搜索/下载支持的站点，返回 `{search: {...}, download: {...}}`。 |
| `POST` | `/api/v1/search` | 聚合搜索多个站点。 |
| `GET` | `/api/v1/tasks` | 任务快照：`{pending, running, completed}`。 |
| `POST` | `/api/v1/tasks` | 新增下载任务。 |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | 取消待执行或运行中的任务。 |
| `GET` | `/api/v1/history` | 列出导出目录中的 `txt`/`epub` 文件。 |
| `GET` | `/api/v1/music/platforms` | 音乐平台列表与使用提示。 |
| `POST` | `/api/v1/music/search` | 在指定音乐平台搜索歌曲。 |
| `POST` | `/api/v1/music/download` | 下载一首或多首歌曲。 |

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
`out_dir` 省略时使用音乐模块的默认下载目录。

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

若未安装音乐可选依赖（`mutagen`、`qrcode`、`websocket-client` 等），
音乐端点返回 `503` 并附安装提示，而不是 `500`：

```json
{"detail": "Music support is not available: ... Install it with: pip install \"novel-downloader[music]\""}
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

# 2. 新增下载任务
curl -s -X POST "$BASE/api/v1/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"斗破苍穹","site":"qidian","book_id":"123456"}'

# 3. 查询进度
curl -s "$BASE/api/v1/tasks" -H "Authorization: Bearer $TOKEN"

# 4. 查看并下载导出文件
curl -s "$BASE/api/v1/history" -H "Authorization: Bearer $TOKEN"
curl -s -OJ "$BASE/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt"
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

## 安全提示

- **默认只绑定 `127.0.0.1`**，不要随意改成 `0.0.0.0`。
- 通过非回环地址提供服务时，**务必设置 token**，并建议放在反向代理/防火墙之后。
- `--cors` 会允许任意网页跨域调用本 API。仅在本机使用时开启；切勿与公网监听、
  无 token 的组合一起使用。
- `/downloads` 是静态目录挂载，不带鉴权，暴露的只是导出文件本身。
- API 具备添加下载任务的能力，等同于本机上的下载器权限，请像保护本地账号一样
  保护 token。
