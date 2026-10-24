# NovelDownloader Documentation

> An asynchronous, extensible novel downloader and processing toolkit with built-in multi-platform music downloading

---

## Project Overview

**NovelDownloader** provides a complete workflow from fetching web content and parsing chapters to text processing and exporting files.

Core features include:

* Multi-site search and download with resumable transfers
* Download queue and progress display
* Multi-format export (TXT / EPUB / HTML)
* Text cleaning and enhancement pipeline (processors)
* Pluggable site parsers and processors
* Multi-platform music downloading
* CLI (`novel-cli`), local REST API (`novel-api`), desktop client (`novel-desktop`), and Android client
* Programmable API

> Requirements: **Python 3.11+** (development environment: Python 3.13)

---

## Features

### Multi-site Search

* Covers 57 search sites and 83 download sites; aggregate searches or target one or more sites
* Results can be sorted by quality, priority, site, title, author, word count, or update time
* Extensible through rule-driven book sources, adding a site without writing Python code

---

### Download Capabilities

* Download queue with live progress
* Resumable crawling: automatically detects already-downloaded chapters
* Pluggable HTTP backends:
    * `aiohttp` (default)
    * `httpx`
    * `curl_cffi` (suitable for anti-scraping sites)

---

### Multi-format Export

* TXT
* EPUB
* HTML

---

### Content Cleaning and Enhancement

* Ad filtering
    * Title filtering
    * Body-text filtering
* Text processing pipeline (processors), including but not limited to:
    * Regex cleanup
    * Conversion between Simplified and Traditional Chinese
    * Machine translation
* Image chapters and obfuscated chapters handling (optional components):
    * Original image download
    * Watermark removal
    * Image preprocessing
    * OCR text extraction
    * Font obfuscation restoration

---

### Multi-platform Music Download

* Supports 25 music platforms (NetEase, QQ Music, Kugou, Kuwo, Migu, Spotify, TIDAL, and more)
* Keyword search per platform, then download by result index or song ID
* Optional audio quality level; output directory and tag embedding follow the music settings
* `--json` output for scripts and third-party integrations

---

### Extensibility

* Plugin system: extendable site parsing, text processing, export formats, and more
* Pluggable HTTP client
* Customizable processing pipeline and export logic

---

### Usage

* Command line (`novel-cli`)
* Desktop client (`novel-desktop`)
* Local REST API (`novel-api`)
* Android client (`android/`)
* Python programming interface

---

## Installation

Install with pip:

```bash
pip install -U novel-downloader
```

Install the aggregated runtime extras:

```bash
pip install novel-downloader[all]
```

Install optional groups as needed:

```bash
pip install "novel-downloader[api]"      # local REST API
pip install "novel-downloader[music]"    # music downloads
```

For more installation options, see: [Installation Guide](guide/installation.md). For standalone Windows builds, see [Windows Install Scripts](guide/install-scripts.md).

---

## Quick Start

### Set Language (Optional)

```bash
# Set to Chinese
novel-cli config set-lang zh_CN

# Set to English
novel-cli config set-lang en_US
```

### Initialize the Configuration File

```bash
novel-cli config init
```

After `settings.toml` is generated, you can adjust it as needed:

* Fetch interval
* Default site configuration
* Text processing pipeline

For details, see: [Configuration Structure](guide/settings-reference.md).

### Command Line (CLI)

```bash
# Automatically parse the URL and download
novel-cli download https://example.com/book/1234/

# Use the site name and book ID
novel-cli download --site qidian 12345
```

More parameters:

```bash
novel-cli --help
novel-cli download --help
```

* For supported sites, see: [Supported Sites List](supported-sites/index.md)
* For more examples, see: [CLI Usage Guide](guide/cli-examples.md)
* You can use `CTRL+C` during a run to cancel the task
* For music downloads, see: [CLI Usage Guide - music subcommand](guide/cli-examples.md#7-music-subcommand)

### Local REST API

The local REST API depends on the `api` extra and listens on `127.0.0.1:8081` by default; it should only be used on the local machine.

```bash
pip install "novel-downloader[api]"
novel-api
```

The API reuses the same service layer as the CLI and exposes endpoints for site listing, aggregate search, the download task queue, history export, and music downloads, making third-party integrations straightforward.

* See [Local REST API](guide/api.md) for endpoints and authentication
* Swagger UI: `http://127.0.0.1:8081/docs`

### Desktop Client

`novel-desktop` provides a Tkinter graphical interface with search, the download queue, export history, and music downloads:

```bash
novel-desktop
```

### Music Download (CLI)

Music support depends on the optional `music` group:

```bash
pip install "novel-downloader[music]"
```

```bash
# List available platforms
novel-cli music platforms

# Search for songs
novel-cli music search "keyword" --platform netease

# Download a chosen search result
novel-cli music download "keyword" --pick 1
```

For option details, see: [CLI Usage Guide - music subcommand](guide/cli-examples.md#7-music-subcommand).

---

## Programming Interface Example

```python
import asyncio
from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig, ClientConfig

async def main() -> None:
    site = "n23qb"

    # Specify the book ID
    book = BookConfig(book_id="12282")

    # Create the client
    cfg = ClientConfig(request_interval=0.5)
    client = registrar.get_client(site, cfg)

    # Perform the download in an async context
    async with client:
        await client.download_book(book)

    # Run the export operation after the download completes
    client.export_book(book, formats=["txt", "epub"])

if __name__ == "__main__":
    asyncio.run(main())
```

For the full API documentation, see: [API Reference](reference/api-examples.md).

---

## Text Processing (`processors`)

NovelDownloader supports a multi-stage text processing pipeline, including:

* Regex cleanup (custom ad removal / watermark removal)
* Conversion between Simplified and Traditional Chinese (based on [opencc-python](https://github.com/yichen0831/opencc-python))
* Automatic translation (supports translators such as `google` / `edge` / `youdao`)
* Text correction (based on [pycorrector](https://github.com/shibing624/pycorrector))
* Custom processors

For details, see: [Processors Documentation](guide/processors-reference.md).

---

## Plugin System

NovelDownloader extends functionality through a registration system, including:

* New site parsers
* New text processors
* New exporters

For documentation, see: [Plugin Development Guide](reference/plugins.md).

---

## Install from Source

```bash
git clone https://github.com/lingyi-ling/novel-downloader.git
cd novel-downloader

# Optional: compile translation files for multilingual support
# pip install -e ".[dev]"
# python scripts/compile_mo.py

pip install .
# Or install with optional features:
# pip install .[all]
```

---

## FAQ

* **Sites that require login**: refer to [Copying Cookies](guide/copy-cookies.md).
* **Location of exported files**: see [File Saving](guide/file-saving.md).
* **Environment self-check**: run `novel-cli doctor`.
* **Errors or connection problems**: see [Troubleshooting](guide/troubleshooting.md).
* **Understanding the code structure**: see [Architecture](guide/architecture.md).
* **Security hardening and vulnerability reporting**: see [Security](guide/security.md).

---

## Notes

* **Site structure changes**: if a target site's page structure is updated or chapter fetching fails, feel free to open an Issue or submit a PR
* **Scope of login support**: login functionality is limited by site policies and APIs; some scenarios require manually configuring Cookies or binding an account
* **Request frequency**: please set the fetch interval reasonably to avoid triggering anti-bot measures or IP restrictions

---

## Project Notice

* This project is for learning and research purposes only and **must not** be used for any commercial or illegal purposes; please comply with the target website's `robots.txt` and relevant laws and regulations
* Any legal liability arising from the use of this project is borne by the user; the author assumes no related responsibility
