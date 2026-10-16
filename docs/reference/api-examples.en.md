# API Description Document

---

## 1. Overview and Layering

* **apps/**: application entry points and interfaces
    * **cli/**: command line (`argparse`, `rich`), `ui_adapters` implements the UI protocol
    * **web/**: NiceGUI frontend (pages, components, task management, login dialog)
* **infra/**: infrastructure (configuration, networking, paths/files, logging, i18n, persistence)
* **libs/**: general-purpose libraries (EPUB, file utilities, encryption/decryption, time utilities)
* **plugins/**: site plugin layer (managed uniformly by `registry`)
    * **fetcher**: network session and login, state persistence
    * **parser**: parses HTML into structured data
    * **client**: site client (unified download/process/export/resource management)
    * **searcher**: site search
    * **registry**: registration and retrieval
* **schemas/**: data models and configuration data classes
* **locales/**: multilingual resources

---

## 2. Quick Start

```python
import asyncio
from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig, ClientConfig

async def main() -> None:
    # specify the site identifier
    site = "n23qb"

    # specify the book ID
    book = BookConfig(book_id="12282")

    # create the client configuration
    cfg = ClientConfig(request_interval=0.5)

    # get the site client instance
    client = registrar.get_client(site, cfg)

    # perform the download inside an async context
    async with client:
        await client.download_book(book)

    # run the export operation after the download completes
    client.export_book(book, formats=["txt", "epub"])

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 3. Configuration API (`infra/config`)

**3.1 `load_config(config_path: str|Path|None) -> dict[str, Any]`**

Reads a TOML file and returns a generic dictionary configuration (raises an exception if it cannot be found or parsing fails).

**3.2 `ConfigAdapter`**

Maps the generic configuration into structured data classes, resolving fields by priority:

1. `config["sites"][<site>]`
2. `config["general"]`
3. default values

**Constructor:**

```python
adapter = ConfigAdapter(config)
```

**Methods:**

```python
get_fetcher_config(site: str)   -> FetcherConfig
get_downloader_config(site: str)-> DownloaderConfig
get_parser_config(site: str)    -> ParserConfig
get_exporter_config(site: str)  -> ExporterConfig
get_login_config(site: str)     -> dict[str, str]
get_book_ids(site: str)         -> list[BookConfig]
```

---

## 4. Plugin System (`plugins`)

**Registrar (`plugins/registry.py`)**

```python
from novel_downloader.plugins import registrar

# decorator registration (site_key defaults to the module's parent directory name)
@registrar.register_fetcher("aaatxt")
class AAAFetcher(...): ...

@registrar.register_parser("aaatxt")
class AAAParser(...): ...

@registrar.register_client("aaatxt")
class AAAClient(...):
    ...
```

Getting instances:

```python
client = registrar.get_client("aaatxt")  # -> ClientProtocol

fetcher = registrar.get_fetcher(site, FetcherConfig)  # -> FetcherProtocol
parser = registrar.get_parser(site, ParserConfig)  # -> ParserProtocol
```

> Site package naming requirement: `plugins.sites.<site_key>.<kind>`; if the first character is a digit, normalize it with an `n` prefix (e.g. `3xx` -> `n3xx`).

---

## 5. Data Models (`schemas/*.py`)

> The excerpts below are for understanding I/O shapes; the actual code is authoritative.

### Chapters and Books

```python
class ChapterDict(TypedDict):
    id: str
    title: str
    content: str
    extra: dict[str, Any]

class ChapterInfoDict(TypedDict):
    title: str
    url: str
    chapterId: str
    accessible: NotRequired[bool]

class VolumeInfoDict(TypedDict):
    volume_name: str
    volume_cover: NotRequired[str]
    update_time: NotRequired[str]
    word_count: NotRequired[str]
    volume_intro: NotRequired[str]
    chapters: list[ChapterInfoDict]

class BookInfoDict(TypedDict):
    book_name: str
    author: str
    cover_url: str
    update_time: str
    summary: str
    extra: dict[str, Any]
    volumes: list[VolumeInfoDict]
    tags: NotRequired[list[str]]
    word_count: NotRequired[str]
    serial_status: NotRequired[str]
    summary_brief: NotRequired[str]
    last_checked: NotRequired[float]  # Unix timestamp
```

### Configuration Data Classes

```python
@dataclass
class FetcherConfig:
    request_interval: float = 2.0
    retry_times: int = 3
    backoff_factor: float = 2.0
    timeout: float = 10.0
    max_connections: int = 10
    max_rps: float = 1000.0
    user_agent: str | None = None
    headers: dict[str, str] | None = None
    verify_ssl: bool = True
    locale_style: str = "simplified"

@dataclass
class OCRConfig: ...
@dataclass
class ParserConfig:
    cache_dir: str = "./novel_cache"
    use_truncation: bool = True
    enable_ocr: bool = False
    batch_size: int = 32
    remove_watermark: bool = False
    cut_mode: str = "none"
    ocr_cfg: OCRConfig = field(default_factory=OCRConfig)

@dataclass
class TextCleanerConfig: ...
@dataclass
class ExporterConfig:
    cache_dir: str = "./novel_cache"
    raw_data_dir: str = "./raw_data"
    output_dir: str = "./downloads"
    check_missing: bool = True
    clean_text: bool = True
    make_txt: bool = True
    make_epub: bool = False
    make_md: bool = False
    make_pdf: bool = False
    append_timestamp: bool = True
    filename_template: str = "{title}_{author}"
    include_cover: bool = True
    include_picture: bool = True
    split_mode: str = "book"
    cleaner_cfg: TextCleanerConfig = field(default_factory=TextCleanerConfig)

class BookConfig(TypedDict):
    book_id: str
    start_id: NotRequired[str]
    end_id: NotRequired[str]
    ignore_ids: NotRequired[list[str]]
```

### Login and Search

```python
@dataclass
class LoginField:
    name: str
    label: str
    type: Literal["text", "password", "cookie"]
    required: bool
    default: str = ""
    placeholder: str = ""
    description: str = ""

class SearchResult(TypedDict):
    site: str
    book_id: str
    book_url: str
    cover_url: str
    title: str
    author: str
    latest_chapter: str
    update_date: str
    word_count: str
    priority: int
```

---

## 6. Site Extension Guide (Minimal Implementation)

**1. Create a new directory**:

```
plugins/sites/<site_key>/
```

**2. Required modules**:

* `fetcher.py`: handles network requests, fetching book pages and chapter pages
* `parser.py`: parses into `BookInfoDict` and `ChapterDict`

**3. Optional modules**:

* `client.py`: if missing, falls back to the `common` implementation
* `searcher.py`: used for CLI / Web search

**4. Apply the corresponding registrar decorator to the class**

**5. If the site requires login**:

* Expose the fields in `FetcherProtocol.login_fields` (`text` / `password` / `cookie`)
* After a successful `login()`, set `is_logged_in = True`
* Also implement `load_state()` / `save_state()` to support session reuse

---

## 7. Common Usage Snippets

### A. Export as EPUB only

```python
from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig

site = "n23qb"  # site identifier
book = BookConfig(book_id="12282")
client = registrar.get_client(site)
client.export_book(book, formats=["epub"])
```

### B. Download + Export Immediately After Searching

```python
import asyncio
from novel_downloader.plugins import registrar
from novel_downloader.plugins.search import search
from novel_downloader.schemas import ClientConfig, BookConfig


async def main() -> None:
    cfg = ClientConfig(request_interval=0.5)

    keyword = "三体"
    results = await search(keyword, sites=["n23qb"])
    if not results:
        print(f"No results found matching '{keyword}'")
        return

    print(f"Found {len(results)} results:")
    for idx, item in enumerate(results[:5], start=1):
        print(f"[{idx}] {item['title']} - {item['author']} ({item['site']})")

    # pick the first result to download
    first = results[0]
    site = first["site"]
    book = BookConfig(book_id=first["book_id"])

    print(f"\nStarting download: {first['title']} - {first['author']} (site: {site})")
    client = registrar.get_client(site, cfg)

    async with client:
        await client.download_book(book)

    # export as txt and epub
    export_result = client.export_book(book, formats=["txt", "epub"])

    print("\nExport complete:")
    for fmt, paths in export_result.items():
        for path in paths:
            print(f" - {fmt}: {path}")

if __name__ == "__main__":
    asyncio.run(main())
```

### C. Sites That Require Login

```python
import asyncio
from typing import Any
from getpass import getpass

from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig, ClientConfig, LoginField
from novel_downloader.infra.cookies import parse_cookies

class SimpleLoginUI:
    async def prompt(
        self,
        fields: list[LoginField],
        prefill: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prefill = prefill or {}
        result: dict[str, Any] = {}

        for field in fields:
            print(f"\n[{field.label}] ({field.name})")
            if field.description:
                print(f"Description: {field.description}")
            if field.placeholder:
                print(f"Hint: {field.placeholder}")

            existing_value = prefill.get(field.name, "").strip()
            if existing_value:
                print("Using the value from the configuration.")
                result[field.name] = existing_value
                continue

            value: str | dict[str, str] = ""
            for _ in range(5):
                if field.type == "password":
                    value = getpass("Enter password: ")
                elif field.type == "cookie":
                    raw = input("Enter Cookies: ").strip()
                    value = parse_cookies(raw)
                else:
                    value = input("Enter value: ").strip()

                if not value and field.default:
                    value = field.default

                if not value and field.required:
                    print("This field is required; please enter a valid value.")
                else:
                    break

            result[field.name] = value

        return result

    def on_login_failed(self) -> None:
        print("Login failed: please check the account, password, or Cookies.")

    def on_login_success(self) -> None:
        print("Login successful.")

async def main() -> None:
    cfg = ClientConfig(request_interval=0.5)

    site = "qidian"
    book = BookConfig(book_id="1001535146")

    client = registrar.get_client(site, cfg)

    async with client:
        # login (if the site requires it)
        await client.login(ui=SimpleLoginUI())
        # download
        await client.download_book(book)

    # export as txt and epub
    export_result = client.export_book(book, formats=["txt", "epub"])

    print("\nExport complete:")
    for fmt, paths in export_result.items():
        for path in paths:
            print(f" - {fmt}: {path}")

if __name__ == "__main__":
    asyncio.run(main())
```

### D. Download Progress Interface

```python
import asyncio

from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig, ClientConfig

class SimpleDownloadUI:
    async def on_start(self, book: BookConfig) -> None:
        print(f"\nStarting download: {book.book_id}")

    async def on_progress(self, done: int, total: int) -> None:
        percent = (done / total * 100) if total else 0.0
        print(f"\rProgress: {done}/{total} ({percent:.1f}%)", end="", flush=True)

    async def on_complete(self, book: BookConfig) -> None:
        print(f"\nDownload complete: {book.book_id}")

async def main() -> None:
    cfg = ClientConfig(request_interval=0.5)

    site = "n23qb"
    book = BookConfig(book_id="12282")

    client = registrar.get_client(site, cfg)

    async with client:
        await client.download_book(book, ui=SimpleDownloadUI())

    # export as txt and epub
    export_result = client.export_book(book, formats=["txt", "epub"])

    print("\nExport complete:")
    for fmt, paths in export_result.items():
        for path in paths:
            print(f" - {fmt}: {path}")

if __name__ == "__main__":
    asyncio.run(main())
```
