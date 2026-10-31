## File Saving

At runtime, the program creates three folders in the current working directory according to the configuration file (for example, `./settings.toml`):

- `downloads`
- `novel_cache`
- `raw_data`

---

### novel_cache

Stores caches and cookie data produced while the program runs.

#### Fixed font cache

Caches obfuscated fonts to avoid downloading and parsing them repeatedly:

```text
novel_cache/fixed_fonts/{font_name}.woff2
novel_cache/fixed_font_map/{font_name}.json
```

---

### raw_data

Stores the raw JSON data and chapter content of each book.

#### Book metadata and table of contents

```text
raw_data/{site_name}/{book_id}/book_info.<stage>.json
```

Example content:

```json
{
  "book_name": "Example Novel",
  "author": "Example Author",
  "cover_url": "//example.com/cover/12345/cover.webp",
  "update_time": "2025-05-09 19:58:13",
  "serial_status": "serializing",
  "word_count": "123.45万字",
  "summary": "This is the summary of an example novel.",
  "volumes": [
    {
      "volume_name": "Example Volume",
      "chapters": [
        {
          "title": "Chapter 1 Example Chapter",
          "url": "//example.com/chapter/1",
          "chapterId": "1"
        }
      ]
    }
  ]
}
```

#### Chapter content database

```text
raw_data/{site_name}/{book_id}/chapter.<stage>.sqlite
```

Database schema:

```sql
CREATE TABLE IF NOT EXISTS chapters (
  id           TEXT    NOT NULL PRIMARY KEY,
  title        TEXT    NOT NULL,
  content      TEXT    NOT NULL,
  need_refetch BOOLEAN NOT NULL DEFAULT 0,
  extra        TEXT
);
```

Field descriptions:

| Field       | Description                                                        |
| ----------- | ------------------------------------------------------------------ |
| **id**      | Chapter ID                                                         |
| **title**   | Chapter title                                                      |
| **content** | Chapter body text                                                  |
| **extra**   | JSON string with additional data, such as resources and update time |

**`extra` field structure**

The `extra` field stores chapter metadata as JSON:

```json
{
  "site": "example_site",
  "wordCount": 2114,
  "resources": [
    {
      "type": "image",
      "paragraph_index": 0,
      "url": "https://example.com/chap_cover.jpg",
      "alt": "Chapter cover image"
    },
    {
      "type": "image",
      "paragraph_index": 3,
      "base64": "iVBORw0KGgo...",
      "mime": "image/jpeg",
      "alt": "Illustration 1"
    },
    {
      "type": "image",
      "paragraph_index": 3,
      "url": "https://example.com/sample2.png",
      "alt": "Illustration 2"
    },
    {
      "type": "font",
      "url": "https://example.com/font.woff2"
    },
    {
      "type": "font",
      "base64": "d09GRgABAAAA...",
      "mime": "font/woff2",
      "range": { "start": 1, "end": 3 }
    },
    {
      "type": "css",
      "text": "body { color: black; }"
    }
  ]
}
```

**`resources` field description**

The resource list contains remote resources, inline resources, font resources, and CSS text referenced by a chapter:

| Field                 | Type              | Description                                                       |
| --------------------- | ----------------- | ----------------------------------------------------------------- |
| **`type`**            | `str`             | Resource type, for example `image` / `font` / `css` / `audio`     |
| **`paragraph_index`** | `int` (optional)  | Paragraph position of the resource (1-based; `0` means before the first paragraph) |
| **`range`**           | `dict` (optional) | Paragraph range to which the resource applies                     |
| **`url`**             | `str` (optional)  | URL of a remote resource                                          |
| **`base64`**          | `str` (optional)  | Base64-encoded resource content                                   |
| **`mime`**            | `str` (optional)  | MIME type of the Base64 content, such as `image/jpeg` or `font/woff2` |
| **`text`**            | `str` (optional)  | Text content                                                      |
| **`alt`**             | `str` (optional)  | Alternative text for the resource                                 |
| **`width`**           | `int` (optional)  | Image width                                                       |
| **`height`**          | `int` (optional)  | Image height                                                      |

Notes:

* A paragraph may contain multiple resources, so `paragraph_index` can repeat.
* `paragraph_index = 0` means the resource appears before the first paragraph.

**`range` field description**

`range` describes the paragraph range to which a resource applies. It is 1-based and inclusive:

```json
"range": { "start": 1, "end": 3 }
```

When a resource has both kinds of positioning information, it is resolved in this order:

1. Use `paragraph_index`.
2. If `paragraph_index` is absent, check `range`.
3. If neither exists, the resource applies to all paragraphs.

#### Chapter media folder

```text
raw_data/{site_name}/{book_id}/media/
```

Stores locally downloaded remote resources (usually the contents of `image` and `font` URLs).

These files are inlined or referenced during EPUB or HTML export.

**Storage strategy**

* URL resources (such as `image` and `font`) are downloaded into `media/`.
* Base64 resources do not create files in this directory; they are normally used directly when packaging.
* URL resources are stored under a hash of the URL with an inferred extension: `{sha1_hash}.{ext}`.

---

### downloads

Stores the assembled and exported book files.

#### Whole-book TXT export

```text
downloads/{book_name}_{author}.txt
```

#### Whole-book EPUB export

```text
downloads/{book_name}_{author}.epub
```

#### Per-volume EPUB export

Each volume produces a separate EPUB, with the volume name in the filename:

```text
downloads/{book_name}_{volume_name}_{author}.epub
```

#### Whole-book HTML export

The HTML export contains the complete book content, an index page, chapter pages, and related static assets.

Output directory structure:

```bash
downloads/{book_name}_{author}/
├── index.html                  # Book home page / table of contents
├── css/
│   ├── index.css               # Index page styles
│   └── chapter.css             # Shared chapter page styles
├── js/
│   └── main.js                 # Navigation / interaction logic
├── chapters/                   # Chapter text
│   ├── c001.html
│   ├── c002.html
│   ├── c003.html
│   └── ...
└── media/
    ├── cover.jpg               # Cover image
    ├── img_1.png               # Images referenced by chapters
    ├── img_2.png
    └── ...                     # Font / resource cache
```

## Recommended readers

The exported EPUB works best in these readers:

* **Desktop**: [Readest](https://readest.com/), [Koodo Reader](https://www.koodoreader.com/en), [Calibre](https://calibre-ebook.com)
* **Mobile**: [Readest](https://readest.com/), Apple Books, [Moon+ Reader](https://moondownload.com/chinese.html), Kindle

To convert to other ebook formats, use [Calibre](https://calibre-ebook.com)
or [FreeConvert](https://www.freeconvert.com/).
