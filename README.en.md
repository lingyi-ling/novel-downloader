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

* Multi-site search: 59 search sites and 84 download sites, with aggregate search, ranking, and filtering
* Download queue and progress: resumable crawling, queue scheduling, and live progress
* Export to TXT, EPUB, and HTML
* History export: re-export previously downloaded books at any time
* Follow updates and self-checks: a built-in follow list that checks for new chapters (optional automatic download), plus `doctor` environment diagnostics and `storage` disk-usage reporting
* Asynchronous and high-performance crawling
* Pluggable HTTP backends: `aiohttp`, `httpx`, `curl_cffi`
* Text processing pipeline: ad removal, zh conversion, translation, etc.
* Optional support for image chapters and obfuscated content
* Multi-platform music download: search and download across 25 music platforms
* Plugin system for site parsers, exporters, and processing pipelines
* Rule-driven book sources: add a site with a JSON rule, no Python code required
* CLI + local REST API + desktop client: `novel-cli`, `novel-api`, and `novel-desktop` share the same service layer
* Android client: native Kotlin/Compose app (`android/`) that talks to the local API

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

For other optional features (local API, music, OCR, image-to-text, extra backends, etc.), refer to the [Full Installation Guide](./docs/guide/installation.md).

---

## Quick Start (CLI)

```bash
# Set preferred interface language
novel-cli config set-lang en_US

# Download a novel
novel-cli download https://www.example.com/book/123/

# Using site + book ID
novel-cli download --site n23qb 12282

# Search and download music
novel-cli music search "keyword" --platform netease
novel-cli music download "keyword" --pick 1
```

More examples: [CLI Examples](./docs/guide/cli-examples.md)

---

## Local REST API

After installing the `api` extra, start the local-only REST service with `novel-api` (defaults to `127.0.0.1:8081`) for third-party integrations covering search, task queues, history export, and music downloads:

```bash
novel-api
```

See [Local REST API](docs/guide/api.md) for endpoints, authentication, and request examples.

---

## Desktop Client

```bash
novel-desktop
```

A graphical interface for search, the download queue, export history, and music downloads.

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

More examples: [API Examples](./docs/reference/api-examples.md)

---

## Development

```bash
git clone https://github.com/lingyi-ling/novel-downloader.git
cd novel-downloader

pip install .
# Optional:
# pip install .[all]
# pip install -e .[dev,all]
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
* For errors, see [Troubleshooting](docs/guide/troubleshooting.md); for the code structure, see [Architecture](docs/guide/architecture.md).

---

## Disclaimer

This project is for learning and research purposes only.

Do not use it for commercial or illegal activities.

Users are responsible for complying with target sites' `robots.txt` and local regulations.
The author assumes no liability for misuse.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=lingyi-ling/novel-downloader&type=Date)](https://star-history.com/#lingyi-ling/novel-downloader&Date)
