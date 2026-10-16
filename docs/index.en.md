# NovelDownloader Documentation

> An asynchronous, extensible novel downloader and processing toolkit

---

## Project Overview

**NovelDownloader** provides a complete workflow from fetching web content and parsing chapters to text processing and exporting files.

Core features include:

* Asynchronous downloading with resumable transfers
* Multi-format export (TXT / EPUB / HTML)
* Text cleaning and enhancement pipeline (processors)
* Pluggable site parsers and processors
* Programmable API
* Command line (CLI) and optional Web GUI

> Requirements: **Python 3.11+** (development environment: Python 3.13)

---

## Features

### Download Capabilities

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

### Extensibility

* Plugin system: extendable site parsing, text processing, export formats, and more
* Pluggable HTTP client
* Customizable processing pipeline and export logic

---

### Usage

* Command line (CLI)
* Web graphical interface (optional installation)
* Python programming interface

---

## Installation

Install with pip:

```bash
pip install -U novel-downloader
```

Install all optional features:

```bash
pip install novel-downloader[all]
```

For more installation options, see: [Installation Guide](guide/installation.md).

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

### Graphical Interface (Web GUI)

The Web GUI depends on additional components (such as NiceGUI) and is not installed with the main program by default.

To use the Web graphical interface, install the corresponding optional dependencies first.

**Install Web GUI Dependencies**

```bash
pip install novel-downloader[web-ui]
```

> If you only need the CLI, you can skip this step.

**Start the Web GUI**

```bash
novel-web
```

To provide LAN/WAN access (please mind security and your network environment):

```bash
novel-web --listen public
```

During a run, you can use `CTRL+C` to stop the service.

**More Resources**

* For supported sites, see: [Supported Sites List](supported-sites/index.md)
* For more examples, see: [Web Usage Guide](guide/web-examples.md)

### Desktop App

The desktop version puts the same Web interface into a native window (based on WebView2 on Windows).

```bash
pip install "novel-downloader[desktop]"
novel-desktop
```

* On first run, it generates `settings.toml` in the user data directory (on Windows: `%LOCALAPPDATA%\novel_downloader`) and uses that directory as the working directory
* You can use `--check` to output an environment health report, or specify the data directory with the `NOVEL_HOME` environment variable
* If pywebview is missing or the WebView2 runtime is unavailable, it automatically falls back to browser mode
* Packaging as an exe is not supported, nor is mobile

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

---

## Notes

* **Site structure changes**: if a target site's page structure is updated or chapter fetching fails, feel free to open an Issue or submit a PR
* **Scope of login support**: login functionality is limited by site policies and APIs; some scenarios require manually configuring Cookies or binding an account
* **Request frequency**: please set the fetch interval reasonably to avoid triggering anti-bot measures or IP restrictions

---

## Project Notice

* This project is for learning and research purposes only and **must not** be used for any commercial or illegal purposes; please comply with the target website's `robots.txt` and relevant laws and regulations
* Any legal liability arising from the use of this project is borne by the user; the author assumes no related responsibility
