## Rule-driven Book Sources

In addition to the built-in Python site plugins, the project supports describing a book source with a JSON rule: search, book details, table of contents, chapter content, pagination, and URL resolution are all rule-driven, so a new site can be added without changing code.

---

### Loading Locations

Rule files are loaded automatically from the following locations. **Later sources override earlier ones** when they use the same `site_key`:

| Location | Description |
| --- | --- |
| `novel_downloader/plugins/rule/builtin/*.json` | Sources shipped with the project |
| `./rules/*.json` | Custom sources in the working directory |
| `$NOVEL_RULES_DIR/*.json` | Directory specified by an environment variable |
| `<user config dir>/novel_downloader/rules/*.json` | Per-user private sources |

Rule files whose names start with `_` are skipped, which makes them suitable for templates and drafts.

The repository's [`rules/_example-template.json`](https://github.com/lingyi-ling/novel-downloader/blob/main/rules/_example-template.json) is an example template covering every field. Because of the `_` prefix it is not loaded by default; copy it and remove the leading underscore to use it.

---

### Validation and Management

```bash
# List every rule source (--check also verifies that each one registers)
novel-cli sources list --check

# Validate a single rule file
novel-cli sources check ./rules/_example-template.json

# Install into the user directory ($NOVEL_RULES_DIR, or the user config dir when unset); --force overwrites an existing file
novel-cli sources add ./my-site.json

# Uninstall a user source (built-in sources are protected and cannot be removed)
novel-cli sources remove my-site

# Search / download with a rule source
novel-cli search "斗破苍穹" -s biquge5r
novel-cli download --site biquge5r 0_143 --start 155832 --end 155835

# Scripted usage: results go to stdout as JSON; --first downloads the top-ranked result
novel-cli search "斗破苍穹" --json
novel-cli search "斗破苍穹" --first
```

`--json` disables interactive prompts and writes machine-readable results to stdout. Diagnostics such as tables and logs always go to stderr, so the output can be piped directly into `jq`. `--json` and `--first` cannot be used together.

If a file fails to parse (JSON syntax error, missing required key, invalid regular expression, and so on), it is skipped during loading and a log entry is written; other sources are unaffected.

---

### Top-level Fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `site_key` | string | Yes | Unique site identifier; a purely numeric key is automatically prefixed with `n` |
| `site_name` | string | No | Display name; falls back to `site_key` when omitted |
| `base_url` | string | No | Site root URL, used for the `{base_url}` template variable |
| `encoding` | string | No | Page encoding, default `utf-8` |
| `format` | string | No | Default response format for the site: `html` (default) or `json`; each section can override it |
| `priority` | int | No | Source priority, default `200` |
| `hosts` | string[] | No | Links whose host matches one of these values are handled by this rule (together with `resolve`) |
| `headers` | object | No | Headers shared by all requests |
| `resolve` | object | No | Link resolution rules, see below |

At least one of `book` and `chapter` must be provided.

If the whole site uses AJAX/JSON endpoints, set `"format": "json"` at the top level instead of repeating it in `search` / `book` / `chapter` (a section-level value overrides the top-level one).

---

### search: Search

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Search page URL, required |
| `method` | string | `GET` | `GET` or `POST` |
| `data` | object | `{}` | POST form body; values support template variables |
| `json_body` | object | empty | POST JSON body (sets `Content-Type: application/json` automatically); string values support template variables and it is mutually exclusive with `data` |
| `format` | string | inherited | Response format for this section, overriding the top-level `format` |
| `headers` | object | `{}` | Headers specific to this section |
| `item` | string | empty | Selector for a result item; leave empty to treat the whole page as one result |
| `fields` | object | `{}` | Field extraction rules; key meanings are listed below |
| `book_id_regex` | string | empty | Regex that extracts the book ID from the book URL, matched with `search` semantics |
| `book_id_group` | int | `1` | Capture group index for `book_id_regex` |
| `priority` | int | `200` | Ranking weight for this result; the N-th result gets N added |

Available keys in `fields`: `title` / `url` / `cover` / `author` / `latest` / `update` / `word`. `url` is required; entries missing it are discarded.

---

### book: Details and Table of Contents

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Book detail page URL template, required |
| `fields` | object | `{}` | Detail fields; supports `book_name` / `author` / `cover_url` / `summary` / `tags`, etc. |
| `chapters` | object | — | Table-of-contents rules, required |
| `pages` | object \| string | empty | Table-of-contents pagination rules |
| `format` | string | inherited | Response format for this section, overriding the top-level `format` |
| `headers` | object | `{}` | Headers specific to this section |

`chapters`:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `item` | string | — | Selector for chapter list items, required |
| `title` | field | `{"attr": "text"}` | Chapter title extraction rule |
| `url` | field | `{"attr": "href"}` | Chapter link extraction rule |
| `id_regex` | string | empty | Regex that extracts the chapter ID from the link; when omitted, the link filename without its extension is used |
| `id_group` | int | `1` | Capture group index for `id_regex` |
| `volume_name` | string | `正文` | Volume name assigned to these chapters |

---

### chapter: Body Text

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Chapter page URL template, required |
| `content` | field | `{"selector": "body"}` | Body extraction rule |
| `title` | field | empty | Chapter title extraction rule |
| `replace` | [string, string][] | `[]` | Replacements applied in order |
| `drop` | string[] | `[]` | Any line matching one of these regexes is removed entirely |
| `pages` | object \| string | empty | Chapter pagination rules |
| `format` | string | inherited | Response format for this section, overriding the top-level `format` |
| `headers` | object | `{}` | Headers specific to this section |

---

### pages: Pagination

```json
"pages": { "url": "{base_url}/book/{book_id}/index_{page}.html", "start": 2 }
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Page URL template, required |
| `start` | int | `2` | First page number |
| `max_pages` | int | empty | When set, fetch pages `start` through `max_pages` unconditionally instead of relying on the "next-page link exists" heuristic |

A string shorthand is also accepted. By default, fetching starts at `start` and continues while **the next-page link is still present in the current page's HTML**, up to 50 extra pages. JSON endpoints usually do not link to the next page in the response body; use `max_pages` for those sites. Table-of-contents pages are deduplicated by chapter link and merged; chapter pages are concatenated in order.

---

### field: Field Extraction

A `field` can be a string (equivalent to `{"selector": "..."}`) or an object:

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `selector` | string | empty | Selector; leave empty to use the current node directly |
| `attr` | string | `text` | Value extraction mode, see below |
| `regex` | string | empty | Applies one more regex extraction to the extracted value (multiline matching) |
| `group` | int | `0` | Regex capture group; falls back to the whole match when the index is out of range |
| `multi` | bool | `false` | Whether to collect every match (returns an array) |
| `replace` | [string, string][] | `[]` | Replacements applied in order |
| `default` | string | empty | Fallback value when no node matches |

The `selector` syntax depends on the section's `format`:

- **HTML (default)** — two syntaxes:
  - **CSS selectors**: `.list`, `div#content`, `meta[property='og:image']`
  - **XPath**: starts with `/`, `./`, or `(`, for example `//div[@class='item']`, `(//div[@class='item'])[2]`
- **JSON** (`"format": "json"`) — dot paths, see the "JSON / AJAX endpoints" section below

`attr` values:

| Value | Meaning |
| --- | --- |
| `text` | Concatenate all text inside the node (no separator); in JSON mode, the value itself |
| `inner_text` | Join the node's text by line (suitable for body text, preserving paragraphs); in JSON mode, arrays are joined with `\n` |
| `html` | The node's HTML source; in JSON mode, equivalent to `text` |
| Any other string | Read as an HTML attribute name, such as `href` / `src` / `content`; in JSON mode, read as an object key |

---

### Template Variables

| Variable | Available in | Description |
| --- | --- | --- |
| `{base_url}` | URLs, `data` / `json_body` values, `headers` values | Site root URL (trailing `/` removed) |
| `{book_id}` | URLs, `data` / `json_body` values, `headers` values | Book ID |
| `{chapter_id}` | URLs, `data` / `json_body` values, `headers` values | Chapter ID |
| `{page}` | Pagination URLs, `data` / `json_body` values | Page number |
| `{keyword}` | Search URLs, `data` / `json_body` values | Search keyword, URL-encoded |
| `{raw_keyword}` | Search URLs, `data` / `json_body` values | Search keyword as-is |

Unrecognized `{...}` placeholders are kept as-is and are not replaced. In `json_body`, templates only apply to string values.

---

### JSON / AJAX Endpoints

After setting `format` to `json`, each section's `selector` becomes a dot path and can parse JSON returned by AJAX endpoints directly.

| Syntax | Meaning |
| --- | --- |
| `data.list` | Read the `list` key of the `data` object; if the result is itself an array, it is expanded into its elements |
| `data.list[0]` | Explicit index access (no automatic expansion with this syntax) |
| `data.list[*]` | Expand every element of the array |
| `data.list.title` | Read `title` from each array element (implicit expansion over arrays) |
| `$.data.total` | A leading `$` and `.` may be omitted |

If a path ends at an array without an explicit index, it is expanded. This is exactly the form an `item` selector should use (`"item": "data.list"` yields each entry in the list, not the list itself).

Example (search via a POST JSON endpoint; details and body remain HTML):

```json
{
  "site_key": "demo-json",
  "base_url": "https://api.demo.example",
  "format": "html",

  "search": {
    "url": "https://api.demo.example/search",
    "method": "POST",
    "format": "json",
    "json_body": {"kw": "{raw_keyword}", "page": "{page}"},
    "item": "data.list",
    "fields": {
      "title": {"selector": "name", "attr": "text"},
      "url": {"selector": "url", "attr": "text"},
      "author": {"selector": "author", "attr": "text"},
      "cover": {"selector": "coverUrl", "attr": "text"},
      "tags": {"selector": "tags[*]", "attr": "text", "multi": true}
    }
  },

  "book": {
    "url": "{base_url}/book/{book_id}.html",
    "fields": {
      "book_name": {"selector": "h1", "attr": "text"},
      "tags": {"selector": "span.tag", "attr": "text", "multi": true}
    },
    "chapters": {
      "item": "div.list a",
      "title": {"attr": "text"},
      "url": {"attr": "href"}
    }
  },

  "chapter": {
    "url": "{base_url}/read/{book_id}/{chapter_id}.html",
    "content": {"selector": "div#content", "attr": "inner_text"}
  }
}
```

For paginated JSON endpoints, the response usually does not link to the next page, so use `max_pages` in `pages`:

```json
"pages": {"url": "https://api.demo.example/book/{book_id}?page={page}", "start": 2, "max_pages": 20}
```

---

### Built-in Examples

`biquge5r.json` is a complete, working built-in source that can serve as a starting point for new rules; `rules/_example-template.json` covers every field (including JSON-mode syntax).

---

### Known Limitations

- JSON mode for rule sources only **extracts values**; it does not include logic or scripts. Endpoints that require signatures, encryption, or complex computations should still be implemented as Python plugins.
- Request rate is controlled centrally by the client configuration (see [Configuration](./configuration.md)); rules do not expose separate throttling parameters.
- Table-of-contents and chapter pagination fetch at most 50 extra pages (the `max_pages` limit is subject to the same cap).
- Mirror-site availability can fluctuate; if a search returns nothing, try another source.