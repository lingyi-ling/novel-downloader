## Plugin System (Plugins)

Extensions (or overrides) for new **sites**, **text processors**, and **clients** are supported through plugins.

Plugins can be enabled from a local directory, and you can choose whether they override the built-in implementations.

### Enabling in `settings.toml`

```toml
[plugins]
# Whether to enable the local plugin directory
enable_local_plugins = true
# Whether local plugins are allowed to override built-in implementations
override_builtins = false
# Local plugin path (optional)
local_plugins_path = "./novel_plugins"
```

### Directory Structure

```text
.
├─ settings.toml
└─ novel_plugins
   ├─ sites
   │  └─ ciweimao            # Example: site key (site_key), i.e. the value of --site
   │     ├─ fetcher.py       # Required: implements the session class (e.g. CiweimaoFetcher)
   │     ├─ parser.py        # Required: implements the parser class (e.g. CiweimaoParser)
   │     ├─ searcher.py      # Optional: used for in-site search
   │     └─ client.py        # Optional: uses the generic CommonClient if not provided
   └─ processors
      └─ processor_a.py      # Custom text processor; its name is referenced in processors.name in settings.toml
```

> `<site_key>` in `sites/<site_key>/` is the value of the `--site` command-line option, for example:
>
> `novel-cli download --site ciweimao 123456`

---

### Extending a Site

A site usually consists of two required components: **Fetcher** (fetches pages) and **Parser** (parses pages)

Optional components: **Searcher** (in-site search), **Client** (overrides the default download/export flow)

#### 1. Fetcher (Fetching)

* Responsibility: login, fetching the **book info page** and **chapter pages** (which may span multiple pages)
* Recommended base class: `BaseFetcher` or the higher-level `GenericFetcher`
* Registration: use the decorator `@registrar.register_fetcher()`

##### Fetcher Protocol (abridged)

```python
class FetcherProtocol(Protocol):
    site_name: str

    async def login(
        self,
        username: str = "",
        password: str = "",
        cookies: dict[str, str] | None = None,
        attempt: int = 1,
        **kwargs: Any,
    ) -> bool: ...
    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]: ...
    async def fetch_chapter_content(self, book_id: str, chapter_id: str, **kwargs: Any) -> list[str]: ...

    @property
    def is_logged_in(self) -> bool: ...
    @property
    def login_fields(self) -> list[LoginField]: ...
```

Sites that require login can provide interactive fields:

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
```

##### Subclassing `BaseFetcher` (simple sites with custom URLs)

```python
from typing import Any
from novel_downloader.plugins.base.fetcher import BaseFetcher
from novel_downloader.plugins.registry import registrar

@registrar.register_fetcher()
class B520Fetcher(BaseFetcher):
    site_name: str = "b520"

    BOOK_INFO_URL = "http://www.b520.cc/{book_id}/"
    CHAPTER_URL   = "http://www.b520.cc/{book_id}/{chapter_id}.html"

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        url = self.BOOK_INFO_URL.format(book_id=book_id)
        return [await self.fetch(url, headers={"Referer": "http://www.b520.cc/"})]

    async def fetch_chapter_content(self, book_id: str, chapter_id: str, **kwargs: Any) -> list[str]:
        url = self.CHAPTER_URL.format(book_id=book_id, chapter_id=chapter_id)
        return [await self.fetch(url, headers={"Referer": "http://www.b520.cc/"}, encoding="gbk")]
```

##### Subclassing `GenericFetcher` (templated URLs / built-in pagination support)

**Single-page info + single-page chapters**

```python
from novel_downloader.plugins.base.fetcher import GenericFetcher
from novel_downloader.plugins.registry import registrar

@registrar.register_fetcher()
class BiquyueduFetcher(GenericFetcher):
    site_name: str = "biquyuedu"
    BOOK_INFO_URL = "https://biquyuedu.com/novel/{book_id}.html"
    CHAPTER_URL   = "https://biquyuedu.com/novel/{book_id}/{chapter_id}.html"
```

**Info/chapters spanning multiple pages**

```python
from novel_downloader.plugins.base.fetcher import GenericFetcher
from novel_downloader.plugins.registry import registrar

@registrar.register_fetcher()
class Biquge5Fetcher(GenericFetcher):
    site_name: str = "biquge5"
    BASE_URL = "https://www.biquge5.com"

    USE_PAGINATED_INFO = True
    USE_PAGINATED_CHAPTER = True

    @classmethod
    def relative_info_url(cls, book_id: str, idx: int) -> str:
        return f"/{book_id}/index_{idx}.html" if idx > 1 else f"/{book_id}/"

    @classmethod
    def relative_chapter_url(cls, book_id: str, chapter_id: str, idx: int) -> str:
        return f"/{book_id}/{chapter_id}_{idx}.html" if idx > 1 else f"/{book_id}/{chapter_id}.html"
```

**Separate info page and catalog page**

```python
from novel_downloader.plugins.base.fetcher import GenericFetcher
from novel_downloader.plugins.registry import registrar

@registrar.register_fetcher()
class I25zwFetcher(GenericFetcher):
    site_name: str = "i25zw"

    HAS_SEPARATE_CATALOG = True
    BOOK_INFO_URL    = "https://www.i25zw.com/book/{book_id}.html"
    BOOK_CATALOG_URL = "https://www.i25zw.com/{book_id}/"
    CHAPTER_URL      = "https://www.i25zw.com/{book_id}/{chapter_id}.html"
```

> `GenericFetcher` also provides the pagination hook `should_continue_pagination(...)`, relative path joining, and other common logic for quick adaptation.

---

#### 2. Parser (Parsing)

* Responsibility: parse the raw HTML list into **book metadata** (including volume/chapter lists) and **chapter content**
* Recommended base class: `BaseParser`
* Registration: `@registrar.register_parser()`

##### Parser Protocol (abridged)

```python
class ParserProtocol(Protocol):
    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None: ...
    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None: ...
```

**Data structures:**

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
```

##### Parsing Example

```python
from typing import Any
from lxml import html
from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import BookInfoDict, ChapterDict, ChapterInfoDict, VolumeInfoDict

@registrar.register_parser()
class AaatxtParser(BaseParser):
    site_name: str = "aaatxt"

    def parse_book_info(self, raw_pages: list[str], **kwargs: Any) -> BookInfoDict | None:
        if not raw_pages:
            return None

        tree = html.fromstring(raw_pages[0])
        book_name = self._first_str(tree.xpath("//div[@class='xiazai']/h1/text()"))
        author    = self._first_str(tree.xpath("//span[@id='author']/a/text()"))
        cover_url = self._first_str(tree.xpath("//div[@id='txtbook']//div[@class='fm']//img/@src"))
        update_time = self._first_str(tree.xpath("//div[@id='txtbook']//li[contains(text(), '上传日期')]/text()"),
                                      replaces=[("上传日期:", "")])
        summary  = self._first_str(tree.xpath("//div[@id='jj']//p/text()"))
        download_url = self._first_str(tree.xpath("//div[@id='down']//li[@class='bd']//a/@href"))

        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath("//div[@id='ml']//ol/li/a"):
            url = a.get("href", "").strip()
            chapter_id = url.split("/")[-1].replace(".html", "")
            title = a.text_content().strip()
            chapters.append({"title": title, "url": url, "chapterId": chapter_id})

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]
        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
            "summary": summary,
            "volumes": volumes,
            "extra": {"download_url": download_url},
        }

    def parse_chapter_content(self, raw_pages: list[str], chapter_id: str, **kwargs: Any) -> ChapterDict | None:
        if not raw_pages:
            return None
        tree = html.fromstring(raw_pages[0])
        raw_title = self._first_str(tree.xpath("//div[@id='content']//h1/text()"))
        title = raw_title.split("-", 1)[-1].strip()

        paragraphs = []
        for txt in tree.xpath("//div[@class='chapter']//text()"):
            line = txt.strip()
            if not line or self._is_ad_line(txt):
                continue
            paragraphs.append(line)

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {"id": chapter_id, "title": title, "content": content, "extra": {"site": self.site_name}}
```

---

#### 3. Searcher (Optional)

* Responsibility: in-site search, returning a list of `SearchResult`
* Base class: `BaseSearcher`
* Registration: `@registrar.register_searcher()`

##### Searcher Example

```python
import logging
from lxml import html
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

@registrar.register_searcher()
class B520Searcher(BaseSearcher):
    site_name = "b520"
    priority  = 30
    BASE_URL  = "http://www.b520.cc/"
    SEARCH_URL = "http://www.b520.cc/modules/article/search.php"

    async def _fetch_html(self, keyword: str) -> str:
        try:
            async with self.session.get(self.SEARCH_URL, params={"searchkey": keyword},
                                      headers={"Referer": "http://www.b520.cc/"}) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception:
            logger.error("Failed to fetch HTML for keyword '%s' from '%s'", keyword, self.SEARCH_URL)
            return ""

    def _parse_html(self, html_str: str, limit: int | None = None) -> list[SearchResult]:
        doc = html.fromstring(html_str)
        rows = doc.xpath('//table[@class="grid"]//tr[position()>1]')
        results: list[SearchResult] = []
        for idx, row in enumerate(rows):
            href = self._first_str(row.xpath(".//td[1]/a[1]/@href"))
            if not href:
                continue
            if limit is not None and idx >= limit:
                break

            book_id = href.strip("/").split("/")[-1]
            book_url = self._abs_url(href)
            title = self._first_str(row.xpath(".//td[1]/a[1]/text()"))
            latest_chapter = self._first_str(row.xpath(".//td[2]/a[1]/text()")) or "-"
            author = self._first_str(row.xpath(".//td[3]//text()"))
            word_count = self._first_str(row.xpath(".//td[4]//text()"))
            update_date = self._first_str(row.xpath(".//td[5]//text()"))

            results.append(SearchResult(
                site=self.site_name,
                book_id=book_id,
                book_url=book_url,
                cover_url="",
                title=title,
                author=author,
                latest_chapter=latest_chapter,
                update_date=update_date,
                word_count=word_count,
                priority=self.priority + idx,
            ))
        return results
```

---

### Extending Text Processors (Processors)

* Responsibility: transform **book metadata** and **chapters** before export
* Protocol: `ProcessorProtocol`
* Registration: `@registrar.register_processor()`

```python
class ProcessorProtocol(Protocol):
    def __init__(self, config: dict[str, Any]) -> None: ...
    def process_book_info(self, book_info: BookInfoDict) -> BookInfoDict: ...
    def process_chapter(self, chapter: ChapterDict) -> ChapterDict: ...
```

**How configuration is passed in**

In `settings.toml`:

```toml
[[general.processors]]
name = "cleaner"
overwrite = false
config_1 = true
config_2 = 2
config_3 = "three"
```

Then the processor constructor `__init__(config)` receives:

```json
{
  "config_1": true,
  "config_2": 2,
  "config_3": "three"
}
```

> Documentation for the existing built-in processors: see `docs/3-settings-schema.md#processors-配置`
> This includes `cleaner` (regex/replace), `zh_convert` (OpenCC simplified/traditional conversion), and `corrector` (pycorrector correction; results vary by model).

---

### Registration Mechanism

Plugins are **registered via decorators**; registration happens automatically when the module is imported, with no manual table/manifest needed:

* Site session: `@registrar.register_fetcher()`
* Site parser: `@registrar.register_parser()`
* Site searcher: `@registrar.register_searcher()`
* Site client: `@registrar.get_client()`
* Text processor: `@registrar.register_processor()`

#### 1. How Key Names Are Determined

The registrar automatically derives a unique key name (key) from the plugin module path, which is used to locate the plugin at runtime.

**Site-related plugins (Fetcher / Parser / Client / Searcher)**

The key name comes from the following parts of the module path:

```
sites.<site_key>.<kind>
```

For example, given the following structure:

```
novel_plugins/
└── sites/
    └── ciweimao/
        ├── fetcher.py
        ├── parser.py
        └── searcher.py
```

The corresponding site key name is:

```
ciweimao
```

That key can be used directly from the command line:

```bash
novel-cli download --site ciweimao 123456
```

> Site key names are always converted to lowercase; if they start with a digit, add the prefix `n` (e.g. `123abc` -> `n123abc`)

**Text Processor (Processor)**

The key name comes from the part of the module path after `processors.`.

For example, the module:

```
novel_downloader.plugins.processors.cleaner
```

Automatically derives the key name:

```
cleaner
```

It can be referenced directly in the configuration file:

```toml
[[general.processors]]
name = "cleaner"
```

If the processor uses a subpackage, for example:

```
novel_plugins/processors/text/zh_convert.py
```

Then the key name is:

```
text.zh_convert
```

#### 2. Search/Load Order and Override Relationship

The registry tries to import modules in "namespace" order (lazy loading):

1. Built-in namespace: `novel_downloader.plugins`
2. (Optional) Local namespace: `novel_plugins` by default, controlled by `[plugins]` in `settings.toml`

In the configuration file:

* `enable_local_plugins = true` enables local plugins
* `local_plugins_path = "./novel_plugins"` specifies the directory
* When `override_builtins = true`, the local namespace is placed **before the built-in one**, allowing overrides of implementations with the same name

> Dynamic import path for site modules: `{namespace}.sites.<site_key>.<kind>`
>
> Dynamic import path for processor modules: `{namespace}.processors.<name>`

---

#### 3. Retrieval and Fallback Strategy

* `get_client(site)`: when no site-specific implementation is found, automatically falls back to the **generic implementation** (`CommonClient`)
* `get_fetcher(site)` / `get_parser(site)`: raises `ValueError("Unsupported site")` if not found
* `get_processor(name)`: raises `ValueError("Unsupported processor")` if not found
* `get_searcher_class(site)`: raises `ValueError("Unsupported site")` if not found
* `get_searcher_classes()`: when no site is specified, attempts to load the `searcher` module for all known sites and returns the available list

> All registrations take effect **when the module is imported**; make sure the plugin files can be imported by Python (correct paths, no syntax errors)
