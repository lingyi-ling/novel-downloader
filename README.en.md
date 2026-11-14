# novel-downloader

[中文](./README.md) | [English](./README.en.md)

[![CI](https://github.com/lingyi-ling/novel-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/lingyi-ling/novel-downloader/actions/workflows/ci.yml)
[![Latest Release](https://img.shields.io/github/v/release/lingyi-ling/novel-downloader)](https://github.com/lingyi-ling/novel-downloader/releases/latest)
[![GitHub Downloads](https://img.shields.io/github/downloads/lingyi-ling/novel-downloader/total)](https://github.com/lingyi-ling/novel-downloader/releases)
[![License](https://img.shields.io/github/license/lingyi-ling/novel-downloader)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](./docs/guide/installation.en.md)
[![GitHub Stars](https://img.shields.io/github/stars/lingyi-ling/novel-downloader?style=flat&color=fbca04)](https://github.com/lingyi-ling/novel-downloader/stargazers)

Asynchronous, modular, and extensible toolkit for downloading and processing online novels, with built-in multi-platform music downloading.

Supports resumable crawling, download queues with progress, multi-format exporting, and a text processing pipeline, exposed through the CLI (`novel-cli`), a local REST API (`novel-api`), a desktop client (`novel-desktop`), and an Android client.

**Requirements**: Python 3.11+ (development tested on Python 3.13)

---

## Features

* Multi-site search: 59 search sites and 84 download sites, with aggregate search, ranking, and word-count / last-updated filters (CLI, REST API, desktop, Android)
* Built-in updates: desktop, CLI and Android check daily; downloads are SHA-256 verified before use. The desktop installs on quit (installer replaces in place, portable replaces its own folder); Android hands the APK to the system installer and never installs silently
* Download queue and progress: resumable crawling, queue scheduling, and live progress
* Export to TXT, EPUB, and HTML
* History export: re-export previously downloaded books at any time
* Follow updates and self-checks: a built-in follow list that checks for new chapters (optional automatic download), plus `doctor` environment diagnostics with opt-in `--fix` repairs, and `storage` disk reporting, read-only `verify`, and `backup --keep N` retention
* Asynchronous and high-performance crawling
* Pluggable HTTP backends: `aiohttp`, `httpx`, `curl_cffi`
* Text processing pipeline: ad removal, zh conversion, translation, etc.
* Optional support for image chapters and obfuscated content
* Multi-platform music download: 28 music platforms, with all-platform aggregate search that merges the same song into one row with its sources, lyrics saved as `.lrc` beside the audio (plus `.zh.lrc` when a translation exists), and M3U8 playlist export
* Plugin system for site parsers, exporters, and processing pipelines
* Rule-driven book sources: add a site with a JSON rule, no Python code required
* CLI + local REST API + desktop client: `novel-cli`, `novel-api`, and `novel-desktop` share the same service layer
* Desktop client: search filters persist across launches, original and translated lyrics open from the result list, and task-manager/update initialization is deferred for a measured cold start of about 0.1 s
* Android client: native Kotlin/Compose app (`android/`) that can talk to a PC-side API or run the full service layer on-device via its optional standalone engine (books and music alike); supports a custom SAF download directory, task notifications that open and highlight the matching task, and opening saved lyrics
* Supply-chain checks: `scripts/scan_secrets.py` can scan the working tree and full history, and is enforced in CI and before release; release assets are additionally audited for archive members and sensitive content

---

## Installation

### 🚀 One-line install (recommended)

**Windows** (PowerShell, no admin needed):

```powershell
irm https://raw.githubusercontent.com/lingyi-ling/novel-downloader/main/bin/install.ps1 | iex
```

**Linux / macOS**:

```bash
bash <(curl -sSL https://raw.githubusercontent.com/lingyi-ling/novel-downloader/main/bin/install.sh)
```

The Windows script downloads the packaged builds from Releases (CLI +
desktop client), adds them to PATH and creates desktop / Start-menu
shortcuts; it falls back to pip when no release is published. The
Linux/macOS script creates an isolated venv and links entry points into
`~/.local/bin`.

### 📦 Manual install

Install the latest stable release:

```bash
pip install -U novel-downloader
```

Install local REST API support:

```bash
pip install "novel-downloader[api]"
```

Install music download support:

```bash
pip install "novel-downloader[music]"
```

### 🔄 Check for updates

```bash
novel-cli update --check   # check only
novel-cli update           # check and ask before downloading (verifies SHA-256, never runs the file)
```

The desktop and Android clients check on their own after launch (at most once per day). See [Updates and auto-update](./docs/guide/updates.en.md).

### 🐳 Docker (REST API)

```bash
docker build -f packaging/Dockerfile -t novel-downloader-api:local .
docker run -d -p 8081:8081 -v noveldl-data:/data novel-downloader-api:local
```

See [packaging/README-docker.md](./packaging/README-docker.md); a Compose example is provided in [packaging/docker-compose.yml](./packaging/docker-compose.yml).

For other optional features (local API, music, OCR, image-to-text, extra backends, etc.), refer to the [Full Installation Guide](./docs/guide/installation.en.md).

---

## Quick Start (CLI)

```bash
# Set preferred interface language
novel-cli config set-lang en_US

# Download a novel
novel-cli download https://www.example.com/book/123/

# Using site + book ID
novel-cli download --site n23qb 12282

# Search and download music (default: every platform, same song merged into one row)
novel-cli music search "keyword"
novel-cli music download "keyword" --pick 1

# Filter by word count / last update and print JSON
novel-cli search "斗破苍穹" --min-words 300000 --updated-after 2024-01-01 --json

# Music download as single-line JSON: status / path / lyric_path
novel-cli music download "keyword" --pick 1 --json

# Read-only verification of every in-use chapter database; back up and keep the newest 7
novel-cli storage verify
novel-cli storage backup --out ~/novel-backups --keep 7

# Preview / apply safe repairs (missing directories, stale temporary files)
novel-cli doctor --fix --dry-run
novel-cli doctor --fix
```

More examples: [CLI Examples](./docs/guide/cli-examples.en.md). For aggregate search, source merging, playlist export, platform health and DRM limits, see the [Music Downloads guide](./docs/guide/music.en.md).

---

## Local REST API

After installing the `api` extra, start the local-only REST service with `novel-api` (defaults to `127.0.0.1:8081`) for third-party integrations covering search, task queues, history export, and music downloads:

```bash
novel-api
```

Search endpoints accept `min_words`, `max_words`, and `updated_after`, filtering results before grouping and limiting; SSE streaming search uses the same semantics as the REST endpoint. See [Local REST API](docs/guide/api.en.md) for endpoints, authentication, and request examples.

---

## Desktop Client

```bash
novel-desktop
```

A graphical interface for search, the download queue, export history, music downloads, and the settings page. The settings page edits the output folder, proxy, automatic updates, and appearance/language, saving them to `settings.toml`. Search results render 60 rows at a time with a "Show more" button, and playlist export asks before overwriting an existing file.

The advanced search panel filters by word count and last-updated date, and the most recent criteria persist and are restored on the next launch (without starting a search automatically). Music results offer original and translated `.lrc` entry points. Task-manager and update initialization are deferred at startup, for a measured cold start of about 0.1 s.

---

## Rule-driven Book Sources (Add a Site Without Code)

Besides the built-in Python site plugins, a book source can be described entirely by a JSON rule:
search, book details, table of contents, body text, pagination, and URL resolution are all rule-driven, so a new site needs no code changes.

See [Rule-driven Book Sources](./docs/guide/book-sources.en.md) for the full field reference, load order, and known limitations.

Rule files are loaded automatically from the following locations (later ones override earlier ones):

| Location | Description |
| --- | --- |
| `novel_downloader/plugins/rule/builtin/*.json` | Sources shipped with the project |
| `./rules/*.json` | Custom sources in the working directory |
| `$NOVEL_RULES_DIR/*.json` | Directory specified by an environment variable |
| `<user config dir>/novel_downloader/rules/*.json` | Per-user private sources |

```json
{
  "site_key": "example",
  "site_name": "Example Source",
  "base_url": "https://www.example.com",
  "encoding": "utf-8",
  "hosts": ["www.example.com"],
  "search": {
    "url": "https://www.example.com/search.php?q={keyword}",
    "item": "div.result-list dl",
    "fields": {
      "title": {"selector": "dd h3 a", "attr": "text"},
      "url":   {"selector": "dt a", "attr": "href"},
      "author":{"regex": "Author:\\s*(\\S+)", "group": 1}
    }
  },
  "book": {
    "url": "{base_url}/book/{book_id}/",
    "pages": {"url": "{base_url}/book/{book_id}/index_{page}.html", "start": 2},
    "fields": {
      "book_name": {"selector": "meta[property='og:novel:book_name']", "attr": "content"},
      "summary":   {"selector": "meta[property='og:description']", "attr": "content"}
    },
    "chapters": {
      "item": "div.listmain a",
      "title": {"attr": "text"},
      "url":   {"attr": "href"},
      "id_regex": "/(\\d+)\\.html"
    }
  },
  "chapter": {
    "url": "{base_url}/book/{book_id}/{chapter_id}.html",
    "content": {"selector": "#content", "attr": "inner_text"},
    "drop": ["^Page \\(\\d+/\\d+\\)$"]
  },
  "resolve": {
    "book": "^/book/(\\d+)/?$",
    "chapter": "^/book/(\\d+)/(\\d+)\\.html$"
  }
}
```

Key points:

* `selector` accepts CSS selectors and XPath (a value starting with `/` or `(` is treated as XPath).
* `attr` values: `text` (concatenated text) / `inner_text` (joined by line) / `html` / any attribute name such as `href`, `src`, `content`.
* `{keyword}` is URL-encoded before substitution; `{page}`, `{book_id}`, `{chapter_id}`, `{base_url}` work the same way.
* `hosts` + `resolve` enable "paste a link to download"; built-in plugins take precedence over rule sources.
* `pages` drives table-of-contents/body pagination: fetching continues while the next-page link is still present in the current page's HTML.

List and manage sources:

```bash
# List every rule source (--check also verifies that each one registers)
novel-cli sources list --check

# Validate a single rule file
novel-cli sources check ./rules/_example-template.json

# Search / download with a rule source
novel-cli search "斗破苍穹" -s biquge5r
novel-cli download --site biquge5r 0_143 --start 155832 --end 155835
```

The built-in `biquge5r.json` is a complete, working example source and a good starting point for new rules.

---

## Programmatic API

```python
import asyncio
from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig, ClientConfig

async def main() -> None:
    site = "n23qb"
    book = BookConfig(book_id="12282")

    cfg = ClientConfig(request_interval=0.5)
    client = registrar.get_client(site, cfg)

    async with client:
        await client.download_book(book)

    client.export_book(book, formats=["txt", "epub"])

if __name__ == "__main__":
    asyncio.run(main())
```

More examples: [API Examples](./docs/reference/api-examples.en.md)

---

## Development

```bash
git clone https://github.com/lingyi-ling/novel-downloader.git
cd novel-downloader

pip install .
# Optional:
# pip install .[all]
# pip install -e .[dev,all]

# Optional: scan the working tree or full history for credentials
# (reports rule names and locations only, never the matched text)
python scripts/scan_secrets.py
python scripts/scan_secrets.py --history
```

Translations (optional):

```bash
pip install babel
pybabel compile -d src/novel_downloader/locales
```

PRs and issues are welcome.

---

## Notes

* Site structures may change. If parsing issues occur, please open an issue or submit a patch.
* Login support depends on site policies. Cookies or manual account setup may be required.
* Configure request intervals responsibly to avoid rate limiting or IP blocking.
* For errors, see [Troubleshooting](docs/guide/troubleshooting.en.md); for the code structure, see [Architecture](docs/guide/architecture.en.md).

---

## Disclaimer

This project is for learning and research purposes only.

Do not use it for commercial or illegal activities.

Users are responsible for complying with target sites' `robots.txt` and local regulations.
The author assumes no liability for misuse.

Thanks to the linuxdo community for its help during this project's development.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=lingyi-ling/novel-downloader&type=Date)](https://star-history.com/#lingyi-ling/novel-downloader&Date)
