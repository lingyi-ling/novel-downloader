# Local REST API

Besides the CLI, the desktop app and the Android client, the project also ships a
local REST API for other software to call. It reuses the exact same service layer
as the desktop app (task manager, search plugins, music download engine), so the
task queue, export directory and site coverage stay consistent.

The API is built with FastAPI, listens on `127.0.0.1:8081` by default, and is
meant for local use only.

## Install and start

API support is the optional `api` extra:

```bash
pip install "novel-downloader[api]"
```

After installation you can use the entry point directly:

```bash
novel-api
# equivalent to uvicorn novel_downloader.apps.api.app:fastapi_app --host 127.0.0.1 --port 8081
```

Common options:

| Option | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Listen address. Do not change it to `0.0.0.0` unless a token is configured. |
| `--port` | `8081` | Listen port. |
| `--token` | env `NOVEL_API_TOKEN` | When set, every `/api/v1/*` and `/downloads/*` request requires authentication. |
| `--cors` | off | Allow cross-origin calls from any origin (`Access-Control-Allow-Origin: *`). |
| `--rate-limit` | `120` | Maximum requests per client IP inside the sliding window; `0` disables rate limiting. |
| `--rate-limit-window` | `60` | Sliding-window length in seconds. |

```bash
# start with a token
novel-api --host 127.0.0.1 --port 8081 --token my-secret

# or use an environment variable
NOVEL_API_TOKEN=my-secret novel-api
```

If `--host` is not a loopback address (`127.0.0.1`/`localhost`/`::1`) and no
token is set, start-up prints a prominent warning to stderr: anyone who can
reach that port can control downloads and read exported files under
`/downloads`. Using `--cors` together with a non-local listen address produces
an additional warning.

Once started:

- Swagger UI: <http://127.0.0.1:8081/docs>
- OpenAPI JSON: <http://127.0.0.1:8081/openapi.json>
- Static export directory: `/downloads/<filename>` (maps to `general.output_dir`)

## Authentication

When no token is configured, requests are not checked; this suits
localhost-only deployments.

When a token is configured, every `/api/v1/*` request and every `/downloads/*`
static-file request must carry one of these headers:

```http
Authorization: Bearer my-secret
```

```http
X-API-Key: my-secret
```

A missing or wrong token returns `401`:

```json
{"detail": "Invalid or missing API token"}
```

## Endpoint overview

All business endpoints are prefixed with `/api/v1`; requests and responses are
normally JSON (except the three SSE endpoints `search/stream`, `tasks/stream`
and `login/requests/stream`). In the examples below `$BASE` is the API root
(e.g. `http://127.0.0.1:8081`) and `$TOKEN` is the token configured at start-up.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Health check, returns `{status, version}`. |
| `GET` | `/api/v1/sites` | Sites supported for search/download, returns `{search: {...}, download: {...}}`. |
| `POST` | `/api/v1/search` | Aggregate search across multiple sites. |
| `GET` | `/api/v1/search/stream` | Stream per-site search results with SSE. |
| `GET` | `/api/v1/browse/categories` | Category tree published by a site (grouped by channel). |
| `POST` | `/api/v1/browse` | Site ranking (omit `category_id`) or books in one category. |
| `GET` | `/api/v1/tasks` | Task snapshot: `{pending, running, completed}`, with pagination. |
| `GET` | `/api/v1/tasks/stream` | Push a task snapshot every second via SSE. |
| `GET` | `/api/v1/tasks/{task_id}` | Fetch a single task; returns `404` if it does not exist. |
| `POST` | `/api/v1/tasks` | Add a download task. |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | Cancel a pending or running task. |
| `GET` | `/api/v1/login/requests` | List pending login-credential requests. |
| `GET` | `/api/v1/login/requests/stream` | Push pending login-credential requests via SSE. |
| `POST` | `/api/v1/login/requests/{req_id}` | Submit credentials and complete a login request. |
| `GET` | `/api/v1/watchlist` | Follow list. |
| `POST` | `/api/v1/watchlist` | Follow a book (adding an existing book only refreshes the entry). |
| `DELETE` | `/api/v1/watchlist/{site}/{book_id}` | Stop following; returns `404` if absent. |
| `POST` | `/api/v1/watchlist/check` | Check followed books for new chapters without starting downloads. |
| `GET` | `/api/v1/history` | List `txt`/`epub` files in the export directory. |
| `GET` | `/api/v1/music/platforms` | Music platform list and usage notes. |
| `POST` | `/api/v1/music/search` | Search songs on one platform or every platform (`platform` defaults to `all`). |
| `POST` | `/api/v1/music/download` | Download one or more songs synchronously. |
| `POST` | `/api/v1/music/jobs` | Create an asynchronous music download job, returns `202 {job_id}`. |
| `GET` | `/api/v1/music/jobs/{job_id}` | Query asynchronous music job status and progress. |
| `POST` | `/api/v1/music/export` | Export downloaded tracks as an M3U8 playlist. |
| `GET` | `/api/v1/music/health` | Platform connectivity self-check (one platform or all). |

Swagger UI (`/docs`) groups the endpoints above under `system`, `search`,
`browse`, `tasks`, `login`, `watchlist`, `history` and `music`.

Task object fields:

`task_id`, `title`, `site`, `book_id`, `status`, `chapters_done`,
`chapters_total`, `progress`, `error`, `exported_paths`, `created_at`,
`finished_at`.

`status` values: `queued`, `running`, `processing`, `exporting`, `completed`,
`cancelled`, `failed`.

## Search

Request body:

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

- `keyword` is required.
- If `sites` is omitted, every enabled site is searched; an empty array searches
  no site.
- `per_site_limit` caps results per site and defaults to `5`.
- `timeout` is the **wall-clock budget per site** in seconds: a site that does
  not answer within this window is treated as timed out, so the whole aggregate
  search takes roughly this long instead of waiting for the slowest site.
- `sort` accepts `quality`, `priority`, `site`, `title`, `author`, `words`,
  `update`.
- `limit` is the total result cap after merging.

Response:

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

## Rankings and category browsing

Sites such as Fanqie publish rankings and categories publicly, so no keyword is
needed:

```bash
# category tree: {site, groups: {male: [{id, name}], female: [...]}}
curl -s "$BASE/api/v1/browse/categories?site=fanqienovel" \
  -H "Authorization: Bearer $TOKEN"

# ranking (omit category_id)
curl -s -X POST "$BASE/api/v1/browse" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"site":"fanqienovel","limit":20}'

# one category: gender only matters for category browsing
curl -s -X POST "$BASE/api/v1/browse" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"site":"fanqienovel","category_id":"1141","gender":"male","limit":20}'
```

The `POST /browse` response has the same shape as `/search`:
`{site, total, results}`. When a site does not support browsing (its searcher
has no `rank`/`categories` implementation), the API returns `400` with
`Site '<site>' does not support browsing.` in `detail`.

## Streaming search (SSE)

`GET /api/v1/search/stream` pushes results as each site finishes, which suits
incremental display. Query parameters mirror the `POST /search` body:

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `keyword` | string | required | Search keyword. |
| `sites` | string | all enabled sites | Comma-separated site list, e.g. `qidian,biquge5`. |
| `per_site_limit` | int | `5` | Result cap per site. |
| `timeout` | float | `5` | Wall-clock budget per site (seconds); slow sites are dropped without blocking others. |

The response is `text/event-stream` with this event format:

```
event: results
data: [{"site":"qidian","book_id":"123456","title":"斗破苍穹", ...}]

event: results
data: [{"site":"biquge5","book_id":"...","title":"...", ...}]

event: done
data: {"total":2}
```

- One `results` event is pushed when each site finishes; `data` is that site's
  result array.
- One `done` event is pushed after all sites finish; `data.total` is the total
  number of items pushed in this request.
- When the client disconnects, the server closes the underlying search generator
  and cancels site requests still in flight.
- Response headers include `Cache-Control: no-cache` and
  `X-Accel-Buffering: no`, so reverse proxies do not buffer the stream.

Use `curl -N` to observe the live event stream:

```bash
curl -N "http://127.0.0.1:8081/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian,biquge5&per_site_limit=5&timeout=5" \
  -H "Authorization: Bearer $TOKEN"
```

!!! note "Authentication and client choice"
    SSE endpoints are protected by the token too and accept it only through the
    `Authorization` or `X-API-Key` header. Browser `EventSource` cannot set
    custom headers, so read the response stream with `fetch` or use a client
    such as `curl` that can set headers.

## Download tasks

Add a task:

```json
{
  "title": "斗破苍穹",
  "site": "qidian",
  "book_id": "123456"
}
```

`title` may be omitted; the API then generates `<site> (id = <book_id>)`.

### Pagination

`GET /api/v1/tasks` accepts `offset` / `limit` query parameters:

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `offset` | int | `0` | Number of tasks to skip; must be `>= 0`. |
| `limit` | int | `50` | Number of tasks to return, `1..200`; values above `200` return `422`. |

Pagination applies to the concatenation of `pending + running + completed`
(order inside each group is unchanged), and the response still contains three
arrays grouped the same way:

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

- `total` is the task count before pagination; `offset` / `limit` echo the
  values actually applied.
- **Backward compatible with older clients**: when neither `offset` nor `limit`
  is passed, no pagination is applied and all tasks are returned (in that case
  `limit` equals `total`); the three array fields are identical to older
  versions.
- When only one of them is passed, the other takes its default (`offset=0`,
  `limit=50`).
- An `offset` beyond the total returns three empty arrays while `total` still
  reports the real count.

```bash
# page 2 (20 items per page)
curl -s "$BASE/api/v1/tasks?offset=20&limit=20" -H "Authorization: Bearer $TOKEN"
```

### Task event stream (SSE)

`GET /api/v1/tasks/stream` pushes one task snapshot immediately, then one every
second:

```bash
curl -N "$BASE/api/v1/tasks/stream" -H "Authorization: Bearer $TOKEN"
```

```
event: snapshot
data: {"pending":[],"running":[{"task_id":"a","status":"running", ...}],"completed":[]}

event: snapshot
data: {"pending":[],"running":[],"completed":[{"task_id":"a","status":"completed", ...}]}
```

- Every event's `data` has the same structure as `GET /api/v1/tasks`
  (`pending` / `running` / `completed`) and is a **full snapshot**, not an
  incremental patch, so clients can replace local state directly.
- This stream is not paginated; poll `GET /api/v1/tasks` when you need paging.
- The stream lives exactly as long as the request: when the client disconnects,
  the server stops polling and releases resources immediately, leaving no
  background task. There is therefore no `Last-Event-ID` replay and no state
  shared across requests.
- Response headers include `Cache-Control: no-cache` and
  `X-Accel-Buffering: no`.
- Authentication matches the search stream: pass the token via
  `Authorization: Bearer` or `X-API-Key`, so browser clients must read the
  stream with `fetch` instead of `EventSource`.

Fetch a single task:

```bash
curl -s "$BASE/api/v1/tasks/<task_id>" -H "Authorization: Bearer $TOKEN"
```

It returns the same task object as the task list; a missing task returns `404`.

A successful cancel returns `{"ok": true}`; a missing task returns `404`.

## Login credential requests

Some sites require login (username/password or cookies) before downloading.
When a task reaches a site login step without a usable saved cookie, the API
creates a "login credential request" and waits for manual input. These requests
are exposed through this group of endpoints — **when a task is stuck in
`running` with no progress for a long time, poll this endpoint and submit
credentials**:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/login/requests` | Current pending credential requests. |
| `GET` | `/api/v1/login/requests/stream` | SSE: pushed when pending requests exist. |
| `POST` | `/api/v1/login/requests/{req_id}` | Submit credentials and complete the request. |

### When a request appears

Once a task queued by `POST /tasks` starts (status `running`), a site that
requires login without a valid cookie creates a credential request. **The task
waits at most 120 seconds**:

- If `GET /login/requests` finds the request and
  `POST /login/requests/{req_id}` submits the credentials within that window,
  the task continues immediately;
- If nobody submits within 120 seconds, the task falls back to the `prefill`
  values (login then usually fails and the task becomes `failed`).

### Request shape

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

- `fields[].type` is `text` / `password` / `cookie`. A `cookie` field takes the
  raw cookie string (e.g. `"key=value; other=value"`, also accepts newlines or
  commas); the server parses it into key/value pairs.
- `label` / `placeholder` / `description` come from the site plugin; the server
  **does not translate or render them** and returns them verbatim (they may be
  translation keys). Presentation is up to the client.
- `prefill` holds values from configuration or a previous login; submitted
  `values` override it by field name.

### Poll-and-submit flow

```bash
# while the task is stuck, poll pending requests
curl -s "$BASE/api/v1/login/requests" -H "Authorization: Bearer $TOKEN"

# submit credentials (req_id from the previous step; omitted fields keep prefill)
curl -s -X POST "$BASE/api/v1/login/requests/<req_id>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"values": {"username": "me", "password": "secret", "cookies": "token=abc; uid=42"}}'
# → {"ok": true}
```

After a successful submit the request disappears from the list immediately and
the waiting task resumes at once. Error codes:

| Status | Scenario |
| --- | --- |
| `404` | `req_id` does not exist (after a successful submit the task takes the result and removes the request, so a repeat submit also gets `404`). |
| `409` | The request is already completed but the task has not taken the result yet, and it is submitted again. |
| `422` | `values` is not an object of the form `{"field name": "string"}`. |

!!! note "Only pending requests are listed"
    `GET /login/requests` returns only requests that are not completed yet. If
    you prefer not to poll, subscribe to the SSE stream:

    ```bash
    curl -N "$BASE/api/v1/login/requests/stream" -H "Authorization: Bearer $TOKEN"
    ```

    The server polls the credential queue once per second: while pending
    requests exist it pushes `requests` events (`data` has the same shape as
    the `requests` array from `GET /login/requests`); when the queue empties it
    pushes one final empty-array event and then stays silent until a new request
    appears. Disconnecting stops it. Authentication matches the other SSE
    endpoints (`Authorization` / `X-API-Key` headers).

## Following books

The follow list shares the same `watchlist.json` as `novel-cli follow`
(in the application data directory, overridable with `NOVEL_HOME`).

`GET /api/v1/watchlist`:

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

`POST /api/v1/watchlist`:

```json
{"site": "b520", "book_id": "8_8187", "title": "示例小说"}
```

The response is `{"created": true, "entry": {...}}`. Adding an existing book
returns `created: false`; when `title` is omitted, the existing title and latest
chapter are preserved.

`DELETE /api/v1/watchlist/{site}/{book_id}` returns `{"ok": true}` on success
and `404` when the book is not followed:

```json
{"detail": "Book not in watchlist: b520/8_8187"}
```

`POST /api/v1/watchlist/check`:

```json
{"download": false}
```

The check runs one single-site search per book on its source site (the same as
`novel-cli follow check`) and writes the latest chapter back to `last_chapter`.
**It does not start any download.** Response:

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
  "items": ["...the result of every book..."]
}
```

- A failed search for one site only affects that entry (`error` is non-empty and
  it counts toward `failed`); the rest are checked normally.
- `needs_download` holds entries that need a download; `items` holds the full
  result for every entry.
- `download=true` only attaches a `task` object to those entries that can be
  submitted directly to `POST /api/v1/tasks`; the server still does not queue a
  download.

## Exported files

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

`download_url` is a path relative to the API root; append it to
`http://127.0.0.1:8081` to download.

`GET /api/v1/history` also accepts `offset` / `limit` (default `0` / `50`, max
`200`); sorting (by modification time, newest first) happens before pagination:

```json
{
  "total": 120,
  "offset": 20,
  "limit": 20,
  "files": [{"name": "...", "ext": "txt", "size": 1, "mtime": 1.0, "download_url": "/downloads/..."}]
}
```

- `total` is always the file count before pagination.
- When neither parameter is passed, all files are returned (backward
  compatible; `limit == total` in that case).
- When only one is passed, the other takes its default: `offset=0`, `limit=50`.
- `limit` above `200` or below `1`, and a negative `offset`, return `422`.

## Music

`GET /api/v1/music/platforms`:

```json
{
  "platforms": [
    {"key": "netease", "label": "国内 · 网易云音乐", "note": ""}
  ]
}
```

`POST /api/v1/music/search` searches every registered platform by default;
`platform` may be omitted or set to `all`:

```json
{"platform": "all", "keyword": "晴天", "limit": 20}
```

An all-platform search queries concurrently, merges the same song into one row,
and `tracks[].sources` lists the platform keys that matched (best source first).
The response additionally contains `search_id` and per-platform `platforms`
statistics:

```json
{
  "platform": "all",
  "total": 2,
  "search_id": "9f2c1b0a9d8e4f7c8b6a5d4e",
  "tracks": [
    {"id": "186016", "name": "晴天", "artists": "周杰伦", "provider": "netease", "sources": ["netease", "qq"]}
  ],
  "platforms": [
    {"platform": "netease", "ok": true, "count": 1, "error": ""},
    {"platform": "qq", "ok": true, "count": 1, "error": ""}
  ]
}
```

A single-platform request (e.g. `"platform": "netease"`) keeps the
`{"total", "tracks", "search_id", "platform"}` shape. `tracks` uses the unified
Track structure and can be passed to the download endpoint unchanged;
`search_id` should be sent back as well (see below).

`POST /api/v1/music/download`:

```json
{
  "platform": "all",
  "search_id": "9f2c1b0a9d8e4f7c8b6a5d4e",
  "tracks": [{"id": "186016", "name": "晴天", "artists": "周杰伦", "sources": ["netease", "qq"]}],
  "level": "exhigh",
  "out_dir": "D:/Music"
}
```

- `platform` defaults to `all`: each song is downloaded from its own `provider`;
  passing an explicit platform key overrides that behavior.
- `search_id` comes from the search response. The direct-link resolution data in
  search results cannot survive a JSON round trip, so sending it back lets the
  server restore the original Track objects; the cache keeps up to 32 entries
  for 30 minutes, after which (or after a process restart) you must search again.
- `level` accepts `hires`, `lossless`, `exhigh`, `higher`, `standard`;
  when `out_dir` is omitted, the music module's default download directory is
  used. A single request accepts at most `200` tracks
  (more returns `422`).

Response:

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

!!! warning "Blocking request"
    The music engine is synchronous, so `/music/download` waits for every track
    to finish before returning. Keep the number of tracks per request small for
    batch downloads, or split the work into multiple requests.

### Asynchronous download jobs

Use the asynchronous job endpoint for larger batches. `POST /api/v1/music/jobs`
returns `202 Accepted` and a job ID immediately, and downloads run in a
background thread pool:

```bash
# create a job (same body as /music/download)
curl -s -X POST "$BASE/api/v1/music/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "platform": "all",
        "search_id": "9f2c1b0a9d8e4f7c8b6a5d4e",
        "tracks": [{"id": "186016", "name": "晴天", "artists": "周杰伦", "sources": ["netease", "qq"]}],
        "level": "exhigh",
        "out_dir": "D:/Music"
      }'
```

Response:

```json
{"job_id": "3f2c1b0a9d8e4f7c8b6a5d4e3c2b1a09"}
```

Poll job status:

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

- `status` values: `queued`, `running`, `done`, `failed`.
- `progress.done` increases as tracks finish; `progress.total` is the number of
  submitted tracks.
- `results` matches the synchronous endpoint and follows submission order; a
  failed job includes the partial results that did finish.
- `error` is non-empty only when `status=failed`.
- The job table lives in process memory and keeps the latest `100` jobs
  (finished jobs are evicted first); records are lost on restart.
- A missing job returns `404`.

!!! note "Single-process in-memory state"
    Asynchronous jobs are not shared across processes. When deploying uvicorn
    with multiple workers, route `POST /music/jobs` and the subsequent
    `GET /music/jobs/{job_id}` requests to the same process, or use the
    synchronous `POST /music/download` endpoint instead.

### Exporting playlists

`POST /api/v1/music/export` writes tracks already downloaded locally to an M3U8
playlist and makes no network requests. Every track must carry a local `path`
(usually taken from the download result or the `music/download` response);
tracks without `path` count toward `skipped`:

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

- When `out_path` ends with `.m3u` / `.m3u8` it is treated as a file path;
  any other value is treated as a directory and a
  `playlist-<UTC timestamp>.m3u8` file is created inside it.
- When `out_path` is omitted, the playlist is written to the music setting
  `out_dir` (`music settings out_dir`).
- `platform` may be `all` (when results from an aggregate search are mixed);
  passing a concrete platform key validates it and an unknown key returns
  `400`. The playlist only records file locations, so this field does not
  affect the written content.

Response:

```json
{"path": "D:/Music/playlists/favorites.m3u8", "tracks_written": 1, "skipped": 0}
```

### Platform health check

`GET /api/v1/music/health` runs one bounded search per platform (no direct-link
resolution and no download). When `platform` is omitted it checks every
registered platform concurrently; `timeout` is the per-platform budget
(default `5` seconds, range `1..60`):

```json
[
  {"platform": "netease", "ok": true, "elapsed_ms": 812, "error": ""},
  {"platform": "qq", "ok": false, "elapsed_ms": 5003, "error": "搜索超时（>5s）"}
]
```

An unknown platform returns `400`.

When the optional music dependencies (`mutagen`, `qrcode`,
`websocket-client`, etc.) are not installed, music endpoints return `503` with
install hints instead of `500`:

```json
{"detail": "Music support is not available: ... Install it with: pip install \"novel-downloader[music]\""}
```

## Task completion webhook

After setting the `NOVEL_API_WEBHOOK` environment variable, any download task
that reaches a terminal state (`completed`, `cancelled`, `failed`) makes the API
send this payload to that URL:

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

- Sending happens on a background daemon thread with a `5`-second timeout per
  attempt; failures (connection errors, timeouts, non-2xx) are only logged and
  never affect task execution or API responses.
- When unset (or when `create_app(webhook_url="")` is passed explicitly), no
  watcher is installed and no polling happens.
- Tasks that already finished when the API starts do not trigger a callback;
  each task is notified once.
- It can also be configured in code: `create_app(webhook_url="https://...")`.

## Request limits and timeouts

To keep a single request from consuming too many resources, the API caps the
request body size for endpoints that accept JSON and applies an overall
wall-clock budget to blocking endpoints.

Request body cap (default `1 MiB`; above it returns `413`):

| Endpoint | Body | List length |
| --- | --- | --- |
| `POST /api/v1/search` | ≤ 1 MiB | — |
| `POST /api/v1/login/requests/{req_id}` | ≤ 1 MiB | — |
| `POST /api/v1/watchlist` | ≤ 1 MiB | — |
| `POST /api/v1/watchlist/check` | ≤ 1 MiB | — |
| `POST /api/v1/music/search` | ≤ 1 MiB | — |
| `POST /api/v1/music/download` | ≤ 1 MiB | `tracks` ≤ 200 |
| `POST /api/v1/music/export` | ≤ 1 MiB | `tracks` ≤ 200 |
| `POST /api/v1/music/jobs` | ≤ 1 MiB | `tracks` ≤ 200 |

More than `200` tracks returns `422` and `detail` names the limit.

Default timeout budgets for blocking endpoints (above them returns `504`):

| Endpoint | Default budget | Description |
| --- | --- | --- |
| `POST /api/v1/search` | `120` seconds | Overall budget on top of each site's `timeout`. |
| `POST /api/v1/music/search` | `120` seconds | Music platform search. |
| `POST /api/v1/music/download` | `1800` seconds | Synchronous download of all tracks. |
| `POST /api/v1/music/jobs` | `1800` seconds | Background jobs exceeding the budget are marked `failed`. |

A timeout only ends **the request's wait**: the underlying blocking thread
cannot be interrupted and keeps running in the background until it finishes.
To change the defaults, override them with `create_app(timeouts={...})`
(mainly intended for tests):

```python
app = create_app(timeouts={"search": 30.0, "music_download": 600.0})
```

### Request rate limiting

The API enables a per-client-IP sliding-window rate limit by default: each IP
may send at most `--rate-limit` (default `120`) requests within
`--rate-limit-window` (default `60`) seconds; excess requests return `429` with
a `Retry-After` header (seconds until a slot frees up). `--rate-limit 0` disables
rate limiting entirely; `GET /api/v1/health` is exempt so availability
monitoring keeps working. Counting happens before authentication, so brute-force
token guessing is throttled too; rejected requests are not counted, and the
window drains automatically once the client stops. Rate-limit state lives in
process memory, so multiple workers count independently.

## Error codes

Error responses are uniformly `{"detail": "..."}` (`detail` is English so
clients can branch on it):

| Status | Scenario |
| --- | --- |
| `400` | Unknown music platform; music platform initialization failure; synchronous music download failure; playlist write failure. |
| `401` | A token is configured but the request is missing it or carries a wrong one. |
| `404` | Task ID not found; login credential request ID not found; music job ID not found; watchlist entry not found; static file not found. |
| `409` | A completed login credential request is submitted again before the task takes its result. |
| `413` | Request body exceeds the endpoint's size cap. |
| `422` | Request parameter validation failed (missing required field, out-of-range value, too many `tracks`, etc.). |
| `429` | Per-client-IP rate limit exceeded; the response carries a `Retry-After` header. |
| `500` | Internal server error; always returns `{"detail": "Internal server error"}` without leaking a stack trace. |
| `503` | Optional music dependencies are missing; music endpoints are unavailable. |
| `504` | A blocking endpoint exceeded its timeout budget. |

On validation failure (`422`), `detail` is a one-sentence description and
`errors` holds the raw validation entries:

```json
{
  "detail": "Request validation failed: keyword: Field required",
  "errors": [
    {"type": "missing", "loc": ["body", "keyword"], "msg": "Field required"}
  ]
}
```

## Complete example

The examples below assume `TOKEN=my-secret`.

```bash
BASE=http://127.0.0.1:8081
TOKEN=my-secret

# 1. search
curl -s -X POST "$BASE/api/v1/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keyword":"斗破苍穹","sites":["qidian"],"per_site_limit":5}'

# 1b. streaming search (real-time SSE events)
curl -N "$BASE/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian,biquge5" \
  -H "Authorization: Bearer $TOKEN"

# 2. add a download task
curl -s -X POST "$BASE/api/v1/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"斗破苍穹","site":"qidian","book_id":"123456"}'

# 3. check progress (list or single task)
curl -s "$BASE/api/v1/tasks" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/api/v1/tasks/<task_id>" -H "Authorization: Bearer $TOKEN"

# 3b. when the task needs a login: poll credential requests and submit (see "Login credential requests")
curl -s "$BASE/api/v1/login/requests" -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$BASE/api/v1/login/requests/<req_id>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"values": {"username": "me", "password": "secret"}}'

# 4. list and download exported files (/downloads is token-protected too)
curl -s "$BASE/api/v1/history" -H "Authorization: Bearer $TOKEN"
curl -s -OJ "$BASE/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt" \
  -H "Authorization: Bearer $TOKEN"
# X-API-Key works as well
curl -s -OJ "$BASE/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt" \
  -H "X-API-Key: $TOKEN"

# 5. asynchronous music download and polling
JOB=$(curl -s -X POST "$BASE/api/v1/music/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"platform":"netease","tracks":[{"id":"186016"}]}' \
  | sed -E 's/.*"job_id":"([^"]+)".*/\1/')
curl -s "$BASE/api/v1/music/jobs/$JOB" -H "Authorization: Bearer $TOKEN"
```

When a token is configured, `/downloads/*` requires authentication just like
`/api/v1/*`: a missing or wrong token returns `401` and does not reveal whether
an exported file exists. Without a token (recommended for localhost only) the
static directory stays unauthenticated. The Android client downloads history
files through Android `DownloadManager` with an `X-API-Key` header, so no extra
configuration is needed there.

## Using the app in code

```python
from novel_downloader.apps.api import create_app

app = create_app(token="my-secret", cors=False)

# hand it to any ASGI server, for example:
# uvicorn my_module:app --host 127.0.0.1 --port 8081
```

You can also import only the factory for integration tests:

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

`novel_downloader.apps.api.client.NovelApiClient` is a synchronous client that
covers every endpoint. Methods related to pagination and task streaming:

```python
from novel_downloader.apps.api.client import NovelApiClient

with NovelApiClient("http://127.0.0.1:8081", token="my-secret") as api:
    # pagination (defaults offset=0, limit=50; server max 200)
    first = api.tasks_page(offset=0, limit=20)
    print(first["total"], len(first["completed"]))

    files = api.history_page(offset=0, limit=20)
    print(files["total"], [item["name"] for item in files["files"]])

    # consume the task stream event by event: every snapshot has the /tasks shape
    for snapshot in api.iter_tasks():
        print(len(snapshot["pending"]), len(snapshot["running"]))
        if not snapshot["pending"] and not snapshot["running"]:
            break          # leaving the loop closes the connection
```

- The existing `tasks()` / `history()` remain unchanged and still return the
  complete list.
- `tasks_page()` / `history_page()` send `offset` / `limit` directly as query
  parameters; out-of-range values (negative, `limit=0`, `limit>200`) make the
  server return `422` and the client raise `ApiError`.
- `iter_tasks()` uses `httpx` streaming to parse SSE and yields only the `data`
  of `snapshot` events (already JSON-decoded). Closing the generator or breaking
  out of the loop closes the connection immediately. Transport errors and
  non-2xx responses raise `ApiError`.

Methods for login credential requests (use alongside `tasks()` polling when a
task needs login):

```python
import time

with NovelApiClient("http://127.0.0.1:8081", token="my-secret") as api:
    # while the task is stuck, poll pending credential requests (submit within 120 seconds)
    pending = api.login_requests()
    while not pending["total"]:
        time.sleep(1)
        pending = api.login_requests()

    req = pending["requests"][0]
    values = {f["name"]: input(f"{f['label']}: ") for f in req["fields"]}
    api.submit_login(req["req_id"], values)   # → {"ok": True}
```

- `login_requests()` returns the full `GET /login/requests` structure (`total`
  and the `requests` list).
- `submit_login(req_id, values)` submits credentials and wakes the waiting task;
  `values` may be omitted (equivalent to an empty object, so every field keeps
  its `prefill`). An unknown `req_id` raises `ApiError` (HTTP 404), and an
  already-completed request raises 409.

Methods for following books and music:

```python
with NovelApiClient("http://127.0.0.1:8081", token="my-secret") as api:
    api.add_watch("b520", "8_8187", title="示例小说")
    print(api.watchlist()["books"])

    result = api.check_watchlist(download=True)
    for item in result["needs_download"]:
        # queue downloads yourself when needed (check itself never downloads)
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

- `watchlist()` / `add_watch()` / `remove_watch()` cover follow-list CRUD.
- `check_watchlist(download=False)` returns the same structure as
  `POST /watchlist/check`.
- `music_export()` and `music_health()` cover playlist export and platform
  self-check; `music_health()` returns a list directly.

## Security notes

- **Bind to `127.0.0.1` by default**; do not casually change it to `0.0.0.0`.
- When serving on a non-loopback address, **always set a token**, and preferably
  put the API behind a reverse proxy or firewall.
- `--cors` allows any web page to call this API cross-origin. Enable it only for
  local use; never combine it with a public listen address and no token.
- `/downloads` is a mounted static directory; with a token configured it also
  requires `Authorization: Bearer` or `X-API-Key`, and without a token it is
  unauthenticated (recommended for localhost only). Traversal paths (such as
  `..`, `%2e%2e`) are rejected with `4xx`.
- The API can add download tasks, which is equivalent to the downloader's
  permissions on this machine; protect the token like a local account.
- Request body size, track count and blocking-endpoint duration all have caps
  (see "Request limits and timeouts"); exceeding them returns `413`, `422` or
  `504` respectively.
