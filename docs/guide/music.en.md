## Music Downloads

The music engine is optional and shares the same service layer as novel downloads. The CLI, desktop client, Android standalone mode and local API all use it, so the platform list, merge rules and on-disk behaviour are the same everywhere.

This page describes the engine's behaviour and limits. For parameters see [CLI Usage - music subcommand](./cli-examples.en.md#7-music-subcommand) and [Local API Service - Music](./api.en.md#music); for symptoms see [Troubleshooting](./troubleshooting.en.md).

> **The platform count is not fixed**: the registry is evaluated at import time against the optional dependencies that are actually installed, so a platform whose dependency is missing does not appear. Treat the output of `novel-cli music platforms` or `GET /api/v1/music/platforms` as authoritative.

---

### Installation and dependencies

The music engine is not part of the default install:

```bash
pip install "novel-downloader[music]"
```

Before every run the CLI probes for `mutagen`, `qrcode`, `websocket-client` and friends; if any is missing it prints the install hint above and exits non-zero instead of raising a traceback. The desktop installer and Android standalone build bundle these dependencies.

The `music` extra also carries the optional dependencies of the platform adapters:

| Dependency | Platforms affected |
| --- | --- |
| `aigpy` | TIDAL (the whole platform is not registered without it) |
| `ytmusicapi` | YouTube Music search |
| `pywidevine` | DRM modules for Apple Music / SoundCloud |
| `m3u8`, `emoji`, `bleach`, `filetype`, ... | Adapters for 5sing / Migu / Kuwo / Qianqian / Bilibili / JOOX import these at module load |

DRM decryption and HLS downloads additionally need **external** conditions (your own CDM, `N_m3u8DL-RE`) that are not pip dependencies; see "DRM and regional limits" below.

---

### Platform model

Every platform implements a common interface: `search` returns platform-neutral `Track` objects, `resolve` turns a `Track` into a downloadable direct link, and `lyric` returns original and translated lyrics when available. The registry stores only a display name and a constructor; platform implementations are imported when selected.

Platforms are grouped as `国内` (China), `国际` (International) and `曲库` (Catalog), sorted by display name inside each group. Some platforms have a usage note (`platform_note`), printed by the CLI after a platform is selected and shown under the search box in the desktop app:

| Platform | Note |
| --- | --- |
| TIDAL | Requires a token JSON in the platform credentials, otherwise search is empty |
| Soda Music | Requires a device Cookie (`{"cookies": {...}}`), otherwise it may hang for a long time |
| Apple Music | Requires an account; DRM decryption requires your own CDM (`.wvd`) |
| SoundCloud | Search works; DRM/HLS downloads require a CDM and external `N_m3u8DL-RE` |
| Spotify / YouTube | Downloads depend on third-party services, which may be unreachable |
| Qobuz / Deezer / Suno / MOOV / JOOX | Require direct access to the corresponding site; otherwise search is empty or times out |

Credential file locations are covered in "Data directory and security". Credentials are re-read on the next search; no process restart is needed.

---

### Unified search and source merging

`--platform` defaults to `all` for `search` and `download`: one query covers every registered platform instead of forcing a per-platform choice first. The desktop platform picker likewise puts "All platforms" first and selects it by default.

Aggregate search rules (implemented in `apps/music/aggregate.py`):

* **Concurrency**: a bounded thread pool queries the platforms and takes results as they arrive; a failure in one platform is recorded against that platform only.
* **Budget**: the whole search has a wall-clock limit (5 seconds by default for CLI/API, 6 seconds for the progressive desktop search). Once the first platform returns, the engine waits about 2 more seconds and then stops; platforms that have not answered are skipped for this run.
* **Per-platform count**: aggregate search takes a small number of raw results per platform (8 by default) and truncates the merged list to `--limit`.
* **Merging**: tracks with the same normalized "title + artist" are treated as one song (when the artist is unknown the album is used instead, so covers are not merged by accident). The merged row lists the matching platforms in `sources`, best source first.
* **Primary source**: non-VIP first, then a cover, a duration, an ID, and finally platform order. VIP tracks sort later so a merged row does not keep only an undownloadable source.
* **Ordering**: keyword relevance first, then non-VIP, number of sources, and platform order.

`sources` is not just display data: during a batch download, if the primary source fails the engine tries the other platforms for the same song in `sources` order (only for in-process original objects; a single-platform search has no alternatives).

Failures and circuit breaking:

* A platform that raises or exceeds the budget is recorded as failed and skipped by aggregate search for 10 minutes.
* A platform that twice takes over 2 seconds and returns nothing is treated as "slow and empty" and skipped for 5 minutes.
* This state is written to `search_stats.json` in the music data directory and survives a restart.
* **Explicitly selecting a platform bypasses the breaker**, which makes manual retries possible.
* If every platform fails, the CLI exits non-zero; zero results from platforms that answered normally is a successful empty search. For diagnosis see [Troubleshooting - aggregate music search](./troubleshooting.en.md#8-some-music-platforms-do-not-respond-are-very-slow-in-aggregate-search).

---

### `search_id`: giving downloads back the original search objects

Direct-link resolution data exists only inside the process (musicdl's `SongInfo`, NetEase raw fields). Once a search result has been through JSON — the pywebview bridge on desktop, HTTP for REST or the embedded engine — those objects degrade to plain dicts and `resolve` fails.

How the API handles it:

1. `POST /api/v1/music/search` returns a `search_id` alongside the tracks.
2. Send `search_id` back when downloading (`POST /api/v1/music/download` or `POST /api/v1/music/jobs`).
3. The server restores the original `Track` objects from the in-process cache by `search_id`; the submitted JSON copies are only a fallback when the cache misses.

Cache limits: at most 32 searches, 30 minutes each, LRU eviction, and the TTL starts when the search happens (a hit does not extend it). After a restart or expiry you must search again; the provider then reports a clear reason such as 「搜索结果已失效，请重新搜索后再下载」 (search results expired, search again) instead of raising `AttributeError`.

The CLI never goes through JSON, so it uses the original objects in the same process and does not need `search_id`. Playlist export does not need it either — export only cares about local file paths.

---

### Downloading and writing files

Quality levels are `hires` / `lossless` / `exhigh` / `higher` / `standard`; the default comes from the music settings, usually `exhigh`.

* NetEase requests the target level first, then falls back to `standard` once if no direct link is available. The level that actually worked is recorded in the result and may be lower than requested.
* Restricted tracks (VIP-only, album purchase required, no copyright / delisted, preview-only) produce a readable reason and count as failed or skipped; they do not abort the batch.
* Transport failures are retried automatically (2 times by default, not counting the first attempt). The `.part` file is kept during retries, which also provides resumption.
* A download is written to `<target>.part`, size-checked, then atomically renamed. An HTML/JSON error page returned by the server (expired direct link) is rejected, so a web page is never saved as audio.

File naming:

* With `split_folder` enabled: `<out_dir>/<artist>/<album>/<title> - <artist>.<ext>`; disabled: directly under `<out_dir>`.
* Same-title tracks with different IDs use `<title> - <artist> [<id>].<ext>` to avoid overwriting each other.
* `skip_existing` only skips an existing file larger than 10 KiB.
* Tags, cover and lyrics are best-effort: a failure in any of them skips that step only, and the downloaded audio still counts as successful.
* Lyrics are written next to the audio as `<stem>.lrc`; a translation is written as `<stem>.zh.lrc`.

Batch downloads:

* Aggregate results are grouped by `provider`; each platform is downloaded concurrently and progress is mapped back to the whole batch.
* When the primary source fails and alternatives exist, other platforms are tried in order. A successful fallback reports success once, not one failure plus one success.
* The CLI exits non-zero if any track failed, so scripts cannot mistake "a few files missing" for success.
* The API `out_dir` must stay inside the music output directories; absolute paths outside them, `..` components and symlink escapes are rejected, and the error never echoes the submitted path.

---

### Playlist export

`export-m3u` writes **already downloaded** tracks to an M3U8 playlist; it does not download audio or fetch direct links:

1. It searches again with `--keyword` (platform defaults to `netease`).
2. For each result it looks for `<title> - <artist>.<ext>` under `<out_dir>/<artist>/<album>/` using the downloader's naming convention; if that is missing it also tries the ` [<id>]` variant.
3. Only tracks with a local path are written; the rest are counted as skipped. If nothing matches, the command fails instead of writing an empty playlist.
4. The default output is `<out_dir>/<keyword>.m3u8`. The playlist is UTF-8 with `#EXTM3U` and `#EXTINF` entries, and stores paths relative to the playlist directory by default so the whole folder can be moved.

The API `POST /api/v1/music/export` does not search and requires every track to carry a local `path`. `out_path` must be a `.m3u` / `.m3u8` file (or a directory, in which case a timestamped `playlist-*.m3u8` is created) and must stay inside the allowed music directories; otherwise it returns `400` with the fixed detail `"Playlist out_path must be a .m3u/.m3u8 file inside the music output directory"`, without echoing the submitted path.

When exporting a playlist from the desktop Music tab, an existing target is never silently overwritten: the app first reports "The playlist already exists: <path>" and only replaces the file after you click that message.

For 400/429 export failures see [Troubleshooting - playlist export](./troubleshooting.en.md#12-music-playlist-export-returns-400-music-jobs-return-429).

---

### Platform health

A health check performs one **bounded search** only; it does not resolve direct links or download anything. It distinguishes "the platform is down" from "this song is not found".

* CLI: `novel-cli music health` checks every platform concurrently, or use `--platform KEY` for one. The probe keyword defaults to `test` and each platform returns at most one result. A successful search with zero results still counts as reachable (that is what an auth-required platform without credentials looks like). The command exits non-zero if any platform fails.
* API: `GET /api/v1/music/health` returns `platform`, `ok`, `elapsed_ms` and `error` per platform. Omitting `platform` checks all; `timeout` defaults to 5 seconds (allowed 0-60). An unknown platform returns `400`.
* A passing single-platform check only means "it can complete a search"; it does not mean member-only or DRM tracks are downloadable.

---

### DRM and regional limits

**Searchable does not mean downloadable.** These platforms try to resolve a downloadable direct link during search; when none is available the list may be empty or contain only previews.

| Platform | Limit |
| --- | --- |
| TIDAL | A token JSON must be configured in the platform credentials, otherwise search is empty; downloads work only when the platform returns an HTTP direct link |
| Apple Music | An account is required for full tracks; DRM decryption requires your own CDM (`.wvd`) |
| SoundCloud | Search works; DRM/HLS tracks require a CDM and external `N_m3u8DL-RE` |
| YouTube Music | Search uses `ytmusicapi`; downloads depend on a third-party resolution service |
| Spotify | Downloads depend on a third-party service, usually unreachable from this environment |

The built-in downloader accepts non-HLS direct links only: HLS (m3u8) resolution is rejected immediately with "HLS 流暂不支持直链下载". Even with a CDM and `N_m3u8DL-RE`, DRM/HLS tracks from Apple Music / SoundCloud therefore **cannot** be downloaded in-app; use an external tool that supports Widevine. This is an expected limit of the current version, not a network failure.

Full details and verification commands: [Troubleshooting - TIDAL / YouTube / Apple Music / SoundCloud](./troubleshooting.en.md#10-tidal-youtube-apple-music-soundcloud-searchable-does-not-mean-downloadable).

---

### Data directory and security

Music runtime data lives in the `music/` subdirectory of the platform data directory (`%LOCALAPPDATA%\novel_downloader\music\` on Windows) and can be relocated wholesale with `NOVEL_MUSIC_HOME`:

| File | Contents |
| --- | --- |
| `settings.json` | Output directory, quality, tags/cover/lyrics, split folders, skip existing, worker count |
| `session.json` | NetEase login state (browser cookies) |
| `platform_creds.json` | Per-platform credentials such as a TIDAL token or Soda cookie |
| `search_stats.json` | Aggregate-search failure breaker and "slow and empty" statistics |

The default download directory is `novel_downloader` under the user's music directory (for example `~/Music/novel_downloader`).

These files and cookies are stored in **plaintext**, and `session.json` / `platform_creds.json` can take over an account directly. Do not commit them, paste them into issues, or include them in screenshots. Delete the files and sign out on the platform side when switching machines or after a suspected leak. See [Security - where cookies and credentials are stored](./security.en.md#where-cookies-and-credentials-are-stored) for locations and permission advice.

---

### Related documentation

* [CLI Usage - music subcommand](./cli-examples.en.md#7-music-subcommand): full parameters and examples for the five subcommands
* [Local API Service - Music](./api.en.md#music): search, synchronous/asynchronous download, playlist export and health endpoints
* [Troubleshooting](./troubleshooting.en.md): missing dependencies, slow aggregate search, DRM limits, export 400/429
* [Android Client](./android-app.en.md): standalone mode bundles the music engine
