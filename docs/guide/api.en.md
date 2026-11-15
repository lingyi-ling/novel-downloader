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
| `--rate-limit` | `120` | Maximum requests per client IP inside the sliding window (must be between `1` and `1000000`; anything else is a startup error). |
| `--no-rate-limit` | off | Disable rate limiting explicitly; only for a trusted local environment. |
| `--rate-limit-window` | `60` | Sliding-window length in seconds; must be greater than `0` and at most `3600`. |

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

> When bound to a non-loopback address (such as `0.0.0.0`), `/docs`, `/redoc`
> and `/openapi.json` are disabled automatically so the full route and
> parameter schema is not published to the LAN; `novel-api` exposes them only
> on a loopback listen.

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
{"detail": "Invalid or missing API token. Send 'Authorization: Bearer <token>' or 'X-API-Key: <token>'."}
```

## Endpoint overview

All business endpoints are prefixed with `/api/v1`; requests and responses are
normally JSON (except the three SSE endpoints `search/stream`, `tasks/stream`
and `login/requests/stream`). In the examples below `$BASE` is the API root
(e.g. `http://127.0.0.1:8081`) and `$TOKEN` is the token configured at start-up.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Liveness check and read-only diagnostics, returns `{status, version, uptime_s, ...}` (see "Health check"). |
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
`finished_at`, `export_stage`.

`status` values: `queued`, `running`, `processing`, `exporting`, `completed`,
`cancelled`, `failed`.

## Health check

`GET /api/v1/health` is a read-only diagnostics endpoint that keeps the
historical fields for old clients and monitoring:

```json
{
  "status": "ok",
  "version": "1.34.0",
  "uptime_s": 1234.567,
  "output_writable": true,
  "cache_writable": true,
  "rate_limit": {"enabled": true, "limit": 120, "window": 60.0},
  "tasks": {"pending": 1, "running": 1, "completed": 8},
  "music_platforms_available": 28
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | string | Always `"ok"`; a response means the process can serve requests (liveness). |
| `version` | string | Server version. |
| `uptime_s` | number | Seconds this process has been running, rounded to 3 decimals; based on `time.monotonic`, so wall-clock changes neither rewind nor jump it. |
| `output_writable` | bool | Whether the export directory (`general.output_dir`) is writable. |
| `cache_writable` | bool | Whether the cache directory (`general.cache_dir`) is writable. |
| `rate_limit` | object | Rate-limiter configuration for this process: `{enabled, limit, window}`. |
| `tasks` | object | Task counts per in-memory queue group: `{pending, running, completed}`. |
| `music_platforms_available` | int | Number of registered music platforms; `0` when the optional music extras are not installed. |

- **Backward compatible**: `status` and `version` are always present and the new fields are additive, so clients that parse only those two are unaffected.
- **Read-only and side-effect-free**: the probe makes no network request, runs no search or download, imports no platform implementation, and creates no directory or file. Writability is a best-effort `os.access` check on the nearest existing ancestor, so a directory that does not exist yet counts as writable when that ancestor is; the response **never contains a path**, a client IP or a rate-limit counter.
- **`rate_limit`**: `enabled=true` means the process installed the sliding-window limiter, `limit` is `--rate-limit` and `window` is `--rate-limit-window` in seconds. With `--no-rate-limit` all three keys are still present: `{"enabled": false, "limit": null, "window": null}`. `enabled` reflects configuration only; no remaining quota or rejection count is reported.
- **`tasks`**: counts only, never task objects; the three groups match `GET /api/v1/tasks` and ignore pagination. `completed` reflects what the current process keeps in memory.
- **`music_platforms_available`**: counted from the lazy registry and cached for the process lifetime; it is `0` when music dependencies are missing or the registry is unavailable. The probe imports no concrete platform and makes no network call.
- `GET /api/v1/health` is exempt from request rate limiting (see "Request rate limiting"), which makes it suitable for availability monitoring.
- **The embedded engine stays a liveness probe**: `/api/v1/health` on the desktop app and Android standalone embedded transport still returns only `{"status", "version"}`. The diagnostic fields above are REST-server only; clients must not assume the embedded transport carries them.

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
  "limit": 20,
  "group": false,
  "min_words": 300000,
  "max_words": 1000000,
  "updated_after": "2024-01-01"
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
  `update`. The default `quality` is the only key that uses the query-intent
  verdict; with any other explicit key the response's `query_intent` is `null`
  (that ordering does not use the verdict).
- `order` accepts `asc` / `desc`; when omitted, the key's natural direction is used.
- `limit` is the total result cap after merging.
- `group` defaults to `false`; when `true`, same-book results from different
  sites are folded into one row. That row is the book's best source and carries
  an extra `sources` array listing every site that has it (best first). The
  envelope stays `{total, results, query_intent}` (see below), so clients that
  ignore grouping and intent keep the default and see no change.
- `min_words` / `max_words` are optional inclusive word-count bounds in the
  range `0`–`1000000000`. The server parses `word_count` with the same rules
  the ranking layer uses (`"30万字"`, `"1,234"`); as soon as either field is
  set, rows **without a parseable word count** are dropped. Omit both for no
  filtering.
- `updated_after` is an optional `YYYY-MM-DD` inclusive lower bound. When set,
  rows **without a parseable update date** are dropped. A malformed date is a
  `422`.
- All three filters run **after ranking and before `group` merging and the
  `limit` cut**. They only narrow the result set; they do not change ordering
  or the response fields. Filters are part of the request identity: different
  filter combinations are cached separately and never reuse an unfiltered
  snapshot. With all three omitted, behaviour is exactly what it was before
  these fields existed.
- `keyword` supports a two-to-four-token "title + author" style query: spaces
  (including the full-width space), `·`, `,`, `，` and `-` split it into
  tokens, which are compared against the title and author separately (for
  example `"斗破苍穹 天蚕土豆"`); the whole keyword still participates in the
  score. Multi-token matching only affects ranking — it does **not** change
  the cache key, which is still the normalised full keyword.

Response:

```json
{
  "total": 2,
  "query_intent": "title",
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
      "priority": 1,
      "rank_score": 0.912345,
      "match_tier": 4,
      "query_intent": "title"
    }
  ]
}
```

The top-level `query_intent` is a verdict on the **whole query** and may be
`null`:

| Value | Meaning |
| --- | --- |
| `"title"` | The result set as a whole looks like a title lookup; the `quality` order is driven by title matches |
| `"author"` | The result set as a whole looks like an author lookup; the `quality` order is driven by author matches |
| `null` | No clear verdict: the query is ambiguous (for example the multi-token "title + author" form, or too little evidence), no field matched, or this call used a non-`quality` sort (an empty `sites` selection also returns `null`) |

It explains why the `quality` order is what it is; it never changes the order
and is never written back to the cache. `query_intent` is a **query-level**
verdict — "does this query look like a title or an author?" — while
`match_tier` is a **row-level** verdict: "how strongly does this one row match
the keyword in one field?" They are different questions: a row can have a high
`match_tier` while the whole query's `query_intent` is still `null`. When
`query_intent` is `"author"`, `match_tier` uses the author field as its primary
field; `"title"` uses the title; `null` keeps the historical rule (the better of
the two fields). Only `"title"` / `"author"` / `null` are accepted; any other
value, or conflicting values within one list, is treated as `null`. The CLI,
desktop app and Android app never guess a label for it.

Every final `results[]` row also carries the same `query_intent` key (`null`
when there is no verdict); the top-level value is aggregated from those rows,
and scripts that only need row-level data may read it there. Streaming
previews, browse results and older cache snapshots have no such key.

In addition to the top-level verdict, each row carries three **read-only
derived fields** (merged rows from `group: true` carry them too):

| Field | Type | Meaning |
| --- | --- | --- |
| `rank_score` | float | A `0..1` match-quality score computed from this keyword and row; it includes a small recency signal, so it is comparable within one call but may drift slightly between calls. |
| `match_tier` | int | The `0..4` match tier: `4` exact equality, `3` prefix, `2` containment, `1` partial character coverage, `0` no match. Deterministic for a given keyword and row. |
| `query_intent` | string \| null | The row-level copy of the top-level verdict: `"title"` / `"author"` / `null`. Every row of one response has the same value; `null` when there is no verdict. |

These fields are response annotations: they are never written back to the cache
and never modify the row a site returned, so clients that only persist books
can ignore them. The streaming endpoint `/search/stream` emits each site's raw
preview chunk and **does not** include `rank_score` / `match_tier` /
`query_intent`; only the final `POST /search` results carry these fields (the
SSE `done` frame has `total` only).

The clients surface the same values like this:

| Client | Presentation |
| --- | --- |
| CLI | The `Title` cell appends `· Exact 0.91` (tier label plus the two-decimal `rank_score`); `--json` is not trimmed and still prints whatever raw fields the backend supplied. |
| Desktop app | A colored badge beside the title shows only the tier (`Exact` / `Prefix` / `Contains` / `Partial` / `No match`); the full explanation lives in the tooltip and accessible name: `Match: Exact · score 0.91`. A cross-site merged row also says `(best source)`. |
| Android app | The badge beside the title reads `Exact · 0.91`; a screen reader announces the full "Match: Exact, relevance score 0.91". With a score but no tier it shows `Relevance 0.80`. |

`query_intent` is a **list-level** explanation, separate from the row badges
above:

| Client | Presentation |
| --- | --- |
| CLI | The results title gains `· Title match` / `· Author match`, e.g. `Search Results · Page 1/2 · Title match`; `--json` keeps `query_intent` on each row. |
| Desktop app | A small list-level chip beside the result count (`Title match` / `Author match`), hidden on a new search, a result-list reset or a failed search; it is not the per-row badge next to the title. |
| Android app | The same list-level chip beside the result count (`Title match` / `Author match`); it appears only when `query_intent` is `"title"` / `"author"` and there are results. Rankings/category browsing clear it, and `null` or unknown values show nothing rather than a guessed label. |

All three use only usable values: `match_tier` must be an integer in `0..4` and
`rank_score` a finite number in `0..1`. Unusable values are ignored: the CLI
and desktop app require both fields before rendering an annotation, while
Android shows the tier alone when the score is missing and the relevance alone
when the tier is missing. When both fields are unavailable (old cache entries,
browse results, an older embedded engine), the row shows no badge/suffix rather
than a misleading `0.00` or `No match`. The annotation explains the ranking; it
is never sent back in a later request.

A filtered request (300k–1M words, updated since 2024-01-01, cross-site grouping):

```bash
curl -s -X POST "$BASE/api/v1/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keyword":"斗破苍穹","sites":["qidian","biquge5"],"per_site_limit":10,"group":true,"min_words":300000,"max_words":1000000,"updated_after":"2024-01-01"}'
```

The filter fields are optional: omitting them is exactly the request this
feature shipped with. If a site returns no word count or update date, those rows
are silently dropped while the corresponding filter is active (that is not a
request error), so "fewer results after filtering" usually means the site is
missing those fields rather than a failed search.

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

The `POST /browse` response uses the same outer shape as `/search`
(`{site, total, results}`), but its rows are the site's entries as returned and
do not carry the search-only `rank_score` / `match_tier` / `query_intent`
annotations. When a
site does not support browsing (its searcher
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
| `min_words` / `max_words` | int | no limit | Inclusive word-count bounds (`0`–`1000000000`); when set, rows without a parseable word count are dropped. |
| `updated_after` | date | no limit | `YYYY-MM-DD` inclusive lower bound; when set, rows without a parseable update date are dropped. |

Filters apply in place to each site's chunk as it arrives, without reordering rows or changing the event shape; the final `done` event still reports the post-filter total. A malformed date returns `422`.

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
- A `results` payload is a site's raw preview rows and does **not** include
  `rank_score` / `match_tier` / `query_intent`; `done` carries `total` only.
  These derived fields appear only in the final `POST /search` response.
- One `done` event is pushed after all sites finish; `data.total` is the total
  number of items pushed in this request.
- When the client disconnects, the server closes the underlying search generator
  and cancels site requests still in flight.
- Response headers include `Cache-Control: no-cache` and
  `X-Accel-Buffering: no`, so reverse proxies do not buffer the stream.
  `X-Request-ID` is returned as well; see [Request tracing](#request-id).
- A `: keepalive` comment frame is written after 15 seconds of silence; it
  carries no event. See [SSE keepalive](#sse-keepalive).

Use `curl -N` to observe the live event stream:

```bash
curl -N "http://127.0.0.1:8081/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian,biquge5&per_site_limit=5&timeout=5" \
  -H "Authorization: Bearer $TOKEN"
```

The stream accepts the same filters, for example to keep only 300k+ word books updated in 2024 or later:

```bash
curl -N "http://127.0.0.1:8081/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian&min_words=300000&updated_after=2024-01-01" \
  -H "Authorization: Bearer $TOKEN"
```

!!! note "Authentication and client choice"
    SSE endpoints are protected by the token too and accept it only through the
    `Authorization` or `X-API-Key` header. Browser `EventSource` cannot set
    custom headers, so read the response stream with `fetch` or use a client
    such as `curl` that can set headers.

## SSE keepalive {#sse-keepalive}

All three SSE endpoints (`search/stream`, `tasks/stream`,
`login/requests/stream`) share one keepalive: after **15 seconds** of silence on
any of them, the server writes a comment frame that starts with a colon:

```
: keepalive
```

- **Ignore it, as the SSE spec requires.** A line starting with a colon is a
  comment, not an event. The keepalive carries no `event` or `data`, never
  triggers `EventSource`'s `message`, and never produces a `results` /
  `snapshot` / `requests` / `done` event. Hand-rolled `fetch` parsers must skip
  lines starting with `:` and comment-only event blocks, and must not feed them
  to a JSON parser.
- **The event contract is unchanged.** Keepalives do not alter event order,
  count, or `done.total`; a client can only treat one as proof that the
  connection is still alive.
- **Timing.** The idle timer starts when the stream opens and resets after every
  frame, data or comment, so nothing is emitted at `t=0`. No keepalive is sent
  immediately before the first frame; one only appears if that first frame takes
  longer than 15 seconds (for example while every site is still answering),
  which is exactly the initial wait it protects. The stream ends immediately
  after its last frame, and **no keepalive is sent after it ends**.
  `tasks/stream` emits a `snapshot` every second, so its keepalive only shows up
  during abnormal silence; `login/requests/stream` goes silent once the queue
  empties and therefore keeps receiving keepalives.
- **Why it exists.** Reverse proxies, load balancers, CDNs and NAT gateways
  reclaim connections that stay byte-silent for too long. The keepalive resets
  their idle timers so a search is not cut off with `502` / `504` or a reset
  connection before its results arrive.
- **Where to tune it.** The interval and frame are defined in
  `src/novel_downloader/apps/api/routes.py` as `SSE_KEEPALIVE_INTERVAL`
  (default `15.0` seconds) and `SSE_KEEPALIVE_FRAME` (`: keepalive\n\n`).
  `_stream_with_keepalive()` also takes an `interval` argument, which tests use
  to shorten it. Changing the interval affects only the heartbeat, never the
  event format.

### Reconnecting and X-Request-ID {#sse-reconnect}

Every SSE connection is an ordinary HTTP request: each (re)connection goes
through `X-Request-ID` resolution on its own and gets its own access-log line.
So:

- **Every reconnect is a new request.** Without an `X-Request-ID`, each
  reconnect receives a fresh UUID4 and appears as a separate log line. To group
  several reconnects into one troubleshooting session, send the same
  `X-Request-ID` on every reconnect (the server reuses it verbatim); to tell
  attempts apart, change it each time (for example `search-retry-01`,
  `search-retry-02`). The server does not de-duplicate: multiple log lines with
  the same id mean multiple connections.
- **Keepalives carry no id and do not resume the stream.** `: keepalive` is only
  a comment frame: no request id, event id, or sequence number. Frames from this
  API also never include an `id:` field, so there is no `Last-Event-ID` and no
  replay. After a reconnect the client starts over with new `results` /
  `snapshot` / `requests`; a `done` event is not replayed. A keepalive only
  proves that the connection is still alive.
- **The logged path has no query string.** The keyword and filters live in the
  query string, while the access log records only
  `/api/v1/search/stream`; use the `X-Request-ID` to correlate reconnects.
- **Reconnect must resend the headers.** The token and `X-Request-ID` are
  request headers, so every reconnect has to carry them again; `EventSource`
  cannot set custom headers, so use `fetch` when a token is configured.

The example below opens the stream with a fixed id and reuses it after a drop;
`grep` removes comment lines, leaving the event frames (`event:` / `data:`
lines):

```bash
BASE=http://127.0.0.1:8081
RID=search-retry-01

# first connection (-N disables curl buffering; -sS keeps only errors)
curl -N -sS -H "Authorization: Bearer $TOKEN" -H "X-Request-ID: $RID" \
  "$BASE/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian"

# reconnect after a drop: same id; --line-buffered streams events immediately
curl -N -sS -H "Authorization: Bearer $TOKEN" -H "X-Request-ID: $RID" \
  "$BASE/api/v1/search/stream?keyword=%E6%96%97%E7%A0%B4&sites=qidian" \
  | grep --line-buffered -v '^:'
```

Use `curl -N -v` to see the `X-Request-ID` in the response headers on stderr: a
valid value is echoed verbatim, while an invalid one shows the UUID4 the server
generated instead.

!!! tip "Troubleshooting behind a proxy or NAT"
    - **Idle read timeouts on the client and every hop must exceed 15 seconds.**
      An HTTP client with a 10-second read timeout disconnects before the first
      keepalive can arrive.
    - On nginx, keep `proxy_read_timeout` above 15 seconds (the default 60 is
      fine) and disable buffering for the location: `proxy_buffering off;`.
      The backend does send `X-Accel-Buffering: no`, but only nginx honours it,
      and an explicit setting is more reliable.
    - CDNs and cloud load balancers usually have an idle limit (Cloudflare's is
      100 seconds); make sure it exceeds the keepalive interval and that
      `text/event-stream` is not cached.
    - NAT gateways commonly reclaim idle connections after 30–120 seconds. If a
      stream always dies at a fixed number of seconds while a local
      `curl -N -v` shows a `: keepalive` every 15 seconds, the heartbeats are
      not crossing the middle layer (buffered or not flushed). Fix the proxy
      configuration rather than the client's reconnect logic.
    - Watch the raw lines with `curl -N -v`: normally a `: keepalive` appears
      roughly every 15 seconds, with `event:` / `data:` frames in between.

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
- After 15 seconds of silence a `: keepalive` comment frame is written as well
  (it carries no event). Normally a `snapshot` arrives every second, so the
  keepalive only appears during abnormal silence. See
  [SSE keepalive](#sse-keepalive).
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
    appears. During that silence a `: keepalive` comment frame is written every
    15 seconds; ignore it (see [SSE keepalive](#sse-keepalive)). Disconnecting
    stops it. Authentication matches the other SSE endpoints (`Authorization` /
    `X-API-Key` headers).

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
The response additionally contains `search_id`, per-platform `platforms`
statistics, and the paging echo:

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
  ],
  "offset": 0,
  "supports_offset": false
}
```

The request body also accepts `offset` (`>= 0`, default `0`), the number of
hits to skip, and `limit` is limited to `1..100`. A single-platform request
(e.g. `"platform": "netease"`) returns
`{"total", "tracks", "search_id", "platform", "offset", "supports_offset"}`.
`tracks` uses the unified Track structure and can be passed to the download
endpoint unchanged; `search_id` should be sent back as well (see below).

- `offset` is honoured only by an **explicit single-platform** search, and the
  response echoes it. Aggregate searches (`platform="all"`) always start at the
  first hit and return `"offset": 0`.
- `supports_offset` says whether the platform actually implements paging.
  musicdl-backed platforms have no paging concept: a non-zero `offset` yields
  an empty page, so they return `false`. A client seeing `false` should stop
  its "load more" control instead of requesting empty pages forever. Aggregate
  searches also return `false`.

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
- `level` accepts `hires`, `lossless`, `exhigh`, `higher`, `standard`.
- `out_dir` must stay inside the music output directories: the music setting
  `out_dir` and the platform default music directory (usually
  `~/Music/novel_downloader`). A relative path is resolved against the default
  directory; when omitted, the default directory is used.
- An absolute path outside those roots, a `..` component, or a symlink escape is
  rejected with `400` and the fixed detail
  `"Music out_dir must be inside the music output directory"` (the submitted
  path is never echoed).
- A single request accepts at most `200` tracks (more returns `422`).
- `results[].reason` is sanitised before it is returned: the query and fragment
  are stripped from any embedded URL (signed tokens never leave the process),
  every whitespace run is collapsed to one space, and the text is capped at
  `200` characters; the full exception is only written to the server log.

When the engine as a whole raises (other than a timeout, which returns `504`),
the response is `502` with a detail such as
`"Music download failed: <exception class>"`: the class name only, never the
exception message.

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
- At most `4` jobs may be `queued` / `running` at once; creating a fifth
  returns `429` with the detail
  `"Too many music jobs are already running (limit 4); retry later"`. This
  `429` carries `Retry-After: 5` (wait roughly one download slot; it is a
  **load** signal, not the per-IP request rate limit). The per-IP rate-limit
  `429` also has `Retry-After`, but its value depends on when the window
  drains; the two are distinguishable by `detail`.
- `progress.done` increases as tracks finish; `progress.total` is the number of
  submitted tracks.
- `results` matches the synchronous endpoint and follows submission order; a
  failed job includes the partial results that did finish.
- `error` is non-empty only when `status=failed`: a timeout uses the fixed
  message `"Music download timed out after ..."`; other engine errors contain
  only the exception class name (`Music download failed: <class>`), not the
  exception message. `results[].reason` goes through the same sanitiser as the
  synchronous endpoint (URL query / fragment stripped, whitespace collapsed,
  capped at `200` characters).
- The job table lives in process memory and keeps the latest `100` jobs
  (finished jobs are evicted first); records are lost on restart.
- A missing job returns `404`.

!!! note "Single-process in-memory state"
    Asynchronous jobs are not shared across processes. When deploying uvicorn
    with multiple workers, route `POST /music/jobs` and the subsequent
    `GET /music/jobs/{job_id}` requests to the same process, or use the
    synchronous `POST /music/download` endpoint instead.

!!! note "Desktop / Android embedded engine"
    The embedded engine used by the desktop app and Android standalone mode
    exposes the same music endpoints. Its `out_dir` / `out_path` confinement and
    active-job cap match the REST API (`400` / `429`; the music-job cap `429`
    also carries `Retry-After: 5`).

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

- When `out_path` ends with `.m3u` / `.m3u8` it is treated as a file path; any
  other value **with a suffix** is rejected. A value without a suffix is treated
  as a directory and a `playlist-<UTC timestamp>.m3u8` file is created inside
  it.
- The resolved target must stay inside the music output directories (the music
  setting `out_dir` or the platform default music directory); an absolute path
  outside them, a `..` component, or a symlink escape returns `400` with the
  fixed detail
  `"Playlist out_path must be a .m3u/.m3u8 file inside the music output directory"`
  (the submitted path is never echoed).
- When `out_path` is omitted, the playlist is written to the music setting
  `out_dir` (`music settings out_dir`).
- `platform` may be `all` (when results from an aggregate search are mixed);
  passing a concrete platform key validates it and an unknown key returns
  `400`. The playlist only records file locations, so this field does not
  affect the written content.
- If the write itself fails (`OSError`), the response is `500` and the detail
  contains only the exception class name
  (`"Could not write playlist: <class>"`), never the target path or OS error
  text.

Response:

```json
{"path": "D:/Music/playlists/favorites.m3u8", "tracks_written": 1, "skipped": 0, "written": 1}
```

`written` equals `tracks_written`; it is a compatibility alias read by the embedded engine and the Android client.

### Platform health check

`GET /api/v1/music/health` runs one bounded search per platform (no direct-link
resolution and no download). When `platform` is omitted it checks every
registered platform concurrently; `timeout` is the per-platform budget
(default `5` seconds; must be greater than `0` and at most `60`):

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
    "finished_at": 1710000100.0,
    "export_stage": "raw"
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
a `Retry-After` header. The header is a **whole number of seconds, rounded up**:
the remaining time until the oldest admitted request slides out of the window
(minimum `1`, never more than the window length), so waiting for it always
finds a free slot. `--no-rate-limit` disables rate limiting entirely
(`--rate-limit 0` now fails with a usage error instead of silently disabling
it); with limiting off, `/health` reports `rate_limit.enabled` as `false` and
`limit` / `window` as `null`. `GET /api/v1/health` is exempt so availability
monitoring keeps working. Counting happens before authentication, so
brute-force token guessing is throttled too; rejected requests are not counted,
and the window drains automatically once the client stops.

Rate-limit state lives only in **the current process's memory**; it is neither
shared across processes nor persisted:

- `novel-api` always runs a single uvicorn process (the application object is
  handed to `uvicorn.run`, which refuses `--workers > 1`), so `--rate-limit`
  is the effective global limit.
- If you deploy manually with
  `uvicorn novel_downloader.apps.api.app:fastapi_app --workers N`, every worker
  counts independently: one client spread over several workers can reach up to
  `N ×` the limit in aggregate (a single keep-alive connection stays with one
  worker and remains bound by one budget). Divide the limit by the worker
  count when you need a global cap — e.g. for 4 workers and a global
  120 requests/minute, use your own module with `create_app(rate_limit=30)` as
  the import target; the module-level `fastapi_app` always uses the default
  120/60.
- A **restart** starts with an empty table, so every client immediately
  regains a full budget. Persisting counters would let a stale lockout survive
  a restart and add a shared-storage failure mode for no benefit on a
  localhost service, so it is deliberately not done.
- The window uses `time.monotonic`, so system clock adjustments (NTP steps,
  manual changes) can neither unlock a client nor extend a lockout.
- The number of tracked IPs has a hard ceiling (default `10000`). At the cap,
  stale entries are swept first and then the least recently active keys are
  evicted. Eviction is fail-open per key (the evicted client starts a fresh
  window), but only a request from a *new* key triggers it, so an
  already-tracked client — including one currently being rejected — cannot
  reset its own counter by hammering. Counting uses the TCP peer address and
  does not trust `X-Forwarded-For`, so behind a reverse proxy all clients
  share the proxy peer's single bucket; limit at the proxy if you need
  per-client accounting, or let the API listen directly together with a
  token.

## Request tracing (X-Request-ID) {#request-id}

The API assigns a correlation id to **every HTTP request** and echoes it in the
`X-Request-ID` header of **every response**: successful responses, `401` /
`404` / `413` / `422` / `429` errors, and the `500` produced by an unhandled
exception. The header is present even when the response is generated directly
by an inner middleware (rate limiter, body-size cap) or by the outermost error
handler. With `--cors`, browser clients can read it too (the header is listed
in `Access-Control-Expose-Headers`).

The error body keeps the uniform `{"detail": "..."}` shape and does **not**
include the id; the correlation is carried by the response header and by the
`[<id>]` prefix of the access-log line — always the same string.

**Accepted and generated values**

- A client may send its own `X-Request-ID` to line up retries, scripts, or a
  bug report with the server log.
- Only the **first** occurrence of the header is considered. The value must be
  `1`–`64` characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, and `-`.
- A value that does not match (empty, non-ASCII, whitespace, control
  characters, brackets, longer than 64 characters) is **silently replaced**
  with a server-generated UUID4. It is not a `400`, and it does not change the
  request's business result. Input containing newlines or escape sequences
  therefore cannot forge a log field or a response header.
- When the client sends nothing usable, the server generates a UUID4. The
  client should still read the response header and use that value when
  reporting.

!!! note "Embedded engine (desktop / Android standalone mode)"
    The embedded HTTP engine (`apps/embedded/server.py`) implements the same
    request-id contract as the REST server: same header name; only the first
    occurrence is considered; only `1`–`64` characters from
    `A-Z a-z 0-9 . _ : -` are accepted; a missing or invalid value is replaced
    with a generated UUID4; and every response (JSON, downloaded file,
    `404` / `405`, the `503 Embedded engine busy`) echoes it in
    `X-Request-ID`. A client can therefore quote one id regardless of which
    transport answered. The difference is logging: the REST access-log line
    starts with `[<id>]`, while the embedded engine logs at `DEBUG` without the
    id, so trust the value echoed in the response header.

**Logging**

Every request produces exactly one access-log line with a fixed shape:

```text
[3f2a1c8e-...] GET /api/v1/sites 200 1.8ms
```

i.e. `[request id] method path status duration`. The query string is never
logged (it may carry a token), and response bodies are never read or logged.
Requests slower than one second are logged at `WARNING` with the same shape.
Given a failed response's id, search the terminal or service log that runs
`novel-api` for `[<request id>]` to find that exact line.

An SSE connection also produces exactly one line: the log is written when the
response head is sent (the stream opens), and its duration is the time until
that head is sent, not the lifetime of the stream; every reconnect adds another
line (see [SSE keepalive and reconnecting](#sse-reconnect)).

**Attaching and reporting from a client**

```bash
BASE=http://127.0.0.1:8081

curl -sS -D headers.txt -o body.json \
  -H "X-Request-ID: import-2026-11-15-001" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keyword": "Three Body"}' \
  "$BASE/api/v1/search"

# On success or failure, read back the id that was actually used
grep -i '^x-request-id:' headers.txt
```

An invalid value (with a space here) is not an error, but it is replaced by a
server-generated UUID4:

```bash
BASE=http://127.0.0.1:8081

curl -sS -D - -o /dev/null \
  -H "X-Request-ID: bad id" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/health" | grep -i '^x-request-id:'
```

- A programmatic client may generate an id per request and send it; the simpler
  option is to omit the header and read the server-generated value from the
  failed response.
- When reporting a problem, include the `X-Request-ID`, the approximate time,
  the request method and path, and the status code. **Do not** include
  `Authorization`, tokens, cookies, or request bodies.
- The id only correlates and aids debugging: it is not used for
  authentication, request de-duplication, rate-limit accounting, or
  persistence. After a server restart, old ids no longer map to any log
  context.

## Response headers and CORS contract

### Security headers

Every `/api/v1/*` and `/downloads/*` response — success or failure — carries:

| Header | Value | Purpose |
| --- | --- | --- |
| `X-Content-Type-Options` | `nosniff` | Stops a browser from re-interpreting the response as another type. |
| `Referrer-Policy` | `no-referrer` | Keeps the (possibly token-bearing) API URL out of `Referer` headers sent to third-party links. |
| `X-Request-ID` | correlation id | See "Request tracing". |

`/downloads/*` additionally carries
`Content-Security-Policy: sandbox; default-src 'none'`: the export directory
shares the API's origin, so a malicious exported HTML/SVG file cannot run
script to drive the local API. JSON responses carry **no** CSP.

`Cache-Control` is assigned per path:

| Path | Value |
| --- | --- |
| `/api/v1/sites`, `/api/v1/history`, `/api/v1/music/platforms` | `private, max-age=60` |
| `/api/v1/tasks*`, `/api/v1/login/requests`, `/api/v1/music/jobs*` and other live data | `no-store` |
| Any `4xx` / `5xx` error | `no-store` |
| `/downloads/*` | `public, max-age=3600` |

A `Cache-Control` set by an SSE route (`no-cache`) is never overridden.
`/docs`, `/redoc` and `/openapi.json` are outside those prefixes and receive
none of these headers.

### CORS

Off by default: no `Access-Control-*` header is sent, and a browser request
carrying a non-loopback `Origin` is rejected with `403` before it reaches the
router (with `no-store` and the security headers). CLI, SDK and curl send no
`Origin` and are unaffected.

`--cors` (or `create_app(cors=True)`) is the explicit opt-in: any origin is
allowed (`Access-Control-Allow-Origin: *`) and the `Origin` gate above is
removed.

- Credentials are **never** combined with the wildcard: responses carry no
  `Access-Control-Allow-Credentials`, so a browser sends no cookies.
  Authentication uses explicit headers.
- The preflight admits only the verbs the API uses (`GET`, `POST`, `DELETE`,
  `HEAD`, `OPTIONS`) and the headers it reads (`Accept`, `Authorization`,
  `Content-Type`, `X-API-Key`, `X-Request-ID`); anything else is a `400`.
- Browser JavaScript may read `Retry-After` and `X-Request-ID`
  (`Access-Control-Expose-Headers`).
- Preflight responses carry the security headers but no `Cache-Control`:
  browsers cache preflights via `Access-Control-Max-Age`, which `no-store`
  would defeat.

!!! note "Embedded engine (desktop / Android standalone mode)"
    The embedded engine has **no CORS layer**: it binds `127.0.0.1` only and
    serves native clients, so it sends no `Access-Control-*` header and a
    browser cannot read its responses cross-origin. It does carry
    `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
    `X-Request-ID`, and the `/downloads` sandbox CSP; it does **not** implement
    the REST `Cache-Control` policy (native clients do not go through an HTTP
    cache). Pointing a browser at the embedded port would require adding both.

## Error codes

Error responses are uniformly `{"detail": "..."}` (`detail` is English so
clients can branch on it):

| Status | Scenario |
| --- | --- |
| `400` | Unknown music platform; `out_dir` / `out_path` outside the music output directories, or an `out_path` suffix other than `.m3u` / `.m3u8`. |
| `401` | A token is configured but the request is missing it or carries a wrong one. |
| `404` | Task ID not found; login credential request ID not found; music job ID not found; watchlist entry not found; static file not found. |
| `409` | A completed login credential request is submitted again before the task takes its result. |
| `413` | Request body exceeds the endpoint's size cap. |
| `422` | Request parameter validation failed (missing required field, out-of-range value, too many `tracks`, etc.). |
| `429` | Per-client-IP rate limit exceeded, with a `Retry-After` header; or `4` music jobs are already active (this `429` also carries `Retry-After: 5`, with detail `Too many music jobs are already running (limit 4); retry later`). |
| `500` | Internal server error; the generic handler always returns `{"detail": "Internal server error"}` without leaking a stack trace. Music platform initialization and playlist write failures return their own fixed messages, containing only the exception class name. |
| `502` | The music download engine failed as a whole; the detail contains only the exception class name (`"Music download failed: <class>"`). |
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
