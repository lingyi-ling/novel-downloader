## Rule DSL Reference

This page is the **field-level reference** for rule-based book-source JSON. It complements [Book Sources](./book-sources.en.md), which explains how to add a source; this page explains what each key actually controls.

> **Kept in sync with the code**: the Chinese master page (`rule-dsl.md`) carries `rule-dsl:keys:*` comment markers around its field tables. `tests/plugins/test_rule_docs.py` parses those tables and compares them with the real sets in `novel_downloader.plugins.rule.model` (`_TOP_LEVEL_KEYS` / `_SEARCH_KEYS` / `_FIELD_KEYS` / `_STEP_KEYS` / `_LIMIT_KEYS` and others). When you add or remove a field, update both pages, or the test fails and lists the difference.
>
> This page follows the current code. Example rules live in `rules/_example-advanced-html.json` and `rules/_example-json-api.json`; both start with `_` and are therefore never registered as real sites by the loader.

---

### Loading and scope

| Location | Description |
| --- | --- |
| `novel_downloader/plugins/rule/builtin/*.json` | Sources shipped with the project, loaded automatically |
| `./rules/*.json` | Custom sources in the working directory |
| `$NOVEL_RULES_DIR/*.json` | Directory named by an environment variable |
| `<user config dir>/novel_downloader/rules/*.json` | Per-user private sources |

Later sources override earlier ones when they use the same `site_key`.

**In user directories such as `rules/`, files whose names start with `_` are not loaded.** Keep examples and drafts in `rules/` with a `_` prefix:

- `rules/_example-template.json` — a template with every field;
- `rules/_example-advanced-html.json` — an HTML site: steps / fallback / preprocess / limits / pagination guards;
- `rules/_example-json-api.json` — a JSON API plus single-result fallback.

Do not put examples in `builtin/`: every `*.json` there is registered as a real source, the example sites do not exist, and they pollute search and `sources list`.

Do not confuse the two `_` conventions:

- A **file name** starting with `_`: the whole file is not loaded;
- A **key name** starting with `_`: ignored at the structural levels of a rule (top level and each section) and usable as a comment. Free-form key/value tables such as `data` / `json_body` / `headers` are not affected inside.

### Resource limits and URL safety

A rule file is configuration, not a program. The loader enforces the following hard limits and rejects a rule that exceeds one with a `RuleError` (without taking down the whole load):

| Limit | Value | Scope |
| --- | --- | --- |
| Single rule file size | 1 MiB (`1024 * 1024` bytes) | Disk loading (`load_rules`) and remote install/fetch each use the same limit |
| `fallback` chain nesting depth | 16 levels | Per-field fallback chain |
| `search.json_body` nesting depth | 32 levels | JSON request body |
| URL scheme | Literals may only be `http` / `https` | `search.url`, `book.url`, `chapter.url` and other templates; relative paths are still allowed |

* The depth limits stop a malicious rule from triggering `RecursionError` during **validation** (which would be a crash rather than a readable rule error).
* `{name}` placeholders in a URL template are removed before the scheme check, so common forms such as `{base_url}/book/{book_id}/` are unaffected; `base_url` is validated separately.
* The final rendered URL is checked again for its scheme before fetching: even when the template itself is valid, a `file://` / `javascript:` value substituted from page data is rejected.

---

### Top-level fields

<!-- rule-dsl:keys:top_level -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `site_key` | string | — | Required; unique site identifier. A numeric value automatically gets an `n` prefix |
| `site_name` | string | `""` | Display name; falls back to `site_key` when omitted |
| `base_url` | string | `""` | Site root, used by the `{base_url}` template variable (trailing `/` removed) |
| `encoding` | string | `utf-8` | Page encoding |
| `format` | string | `html` | Default response format; only `html` or `json`; each section may override it |
| `priority` | integer | `200` | Source priority; a larger value sorts earlier |
| `hosts` | string[] | `[]` | Links whose host (lowercase) matches are handled by this rule, together with `resolve` |
| `headers` | object | `{}` | Request headers for book / chapter fetches. **Sent as-is, without template substitution** (put variables in each section's `headers`) |
| `resolve` | object | empty | Link → `book_id` / `chapter_id` resolution rule |
| `search` | object | empty | Search section |
| `book` | object | empty | Details + chapter list section |
| `chapter` | object | empty | Chapter body section |
| `limits` | object | empty | Per-rule concurrency / interval / retry overrides, see below |
<!-- /rule-dsl:keys:top_level -->

At least one of `book` and `chapter` must be present, otherwise the rule is invalid. A request header containing CR / LF / NUL is rejected outright, which blocks header injection.

---

### `search`: search

<!-- rule-dsl:keys:search -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Required; search URL. Supports `{base_url}` / `{keyword}` / `{raw_keyword}` / `{page}` |
| `method` | string | `GET` | `POST` submits a form; any other value (including `GET`) is treated as GET; the method is uppercased without an allow-list check |
| `data` | object | `{}` | POST form body; string values support `{keyword}` / `{raw_keyword}` / `{page}` templates |
| `json_body` | object | empty | POST JSON body; automatically carries `Content-Type: application/json`. When present it is always sent as JSON POST and takes precedence over `data` / `method` |
| `headers` | object | `{}` | Search-specific request headers; **sent as-is, no template substitution** |
| `format` | string | inherited from top level | Response format for this section, overriding the top-level `format` |
| `item` | string | `""` | Result item selector; empty means the whole page is one result |
| `fields` | object | `{}` | Field extraction rules; key names are restricted, see below |
| `book_id_regex` | string | empty | Regex extracting the ID from a book link (`search` semantics) |
| `book_id_group` | integer | `1` | Capture group of the regex above; must be >= 0 |
| `priority` | integer | `200` | Ranking weight for search results; within one search, results add `0`, `1`, `2` ... in order |
| `single_result_fallback` | boolean | `false` | When `item` matches nothing, treat the whole page as one result; see below |
<!-- /rule-dsl:keys:search -->

`fields` accepts only the keys the engine actually consumes:

<!-- rule-dsl:keys:search_fields -->
| Field | Type | Description |
| --- | --- | --- |
| `title` | field | Book title |
| `url` | field | Details-page link, **required**; a result without it is dropped |
| `cover` | field | Cover image; relative addresses are completed against `base_url` |
| `author` | field | Author |
| `latest` | field | Latest chapter title |
| `update` | field | Update time |
| `word` | field | Word count; shown as `-` when absent |
<!-- /rule-dsl:keys:search_fields -->

---

### `book`: details and chapter list

<!-- rule-dsl:keys:book -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Required; details-page template. Supports `{base_url}` / `{book_id}` |
| `fields` | object | `{}` | Details fields; key names are restricted, see below |
| `chapters` | object | — | Required; chapter-list rule |
| `pages` | object \| string | empty | Chapter-list pagination rule; a string is equivalent to `{"url": "..."}` |
| `headers` | object | `{}` | Request headers for this section; values support template variables |
| `format` | string | inherited from top level | Response format for this section |
<!-- /rule-dsl:keys:book -->

Details field names:

<!-- rule-dsl:keys:book_fields -->
| Field | Type | Description |
| --- | --- | --- |
| `book_name` | field | Book title |
| `author` | field | Author |
| `cover_url` | field | Cover; relative addresses are completed |
| `update_time` | field | Update time |
| `summary` | field | Summary (full) |
| `summary_brief` | field | Summary (short); written only when non-empty |
| `serial_status` | field | Serialization status; written only when non-empty |
| `word_count` | field | Word count; written only when non-empty |
| `tags` | field | Tags; **the only field that supports `multi`**, collects every match |
<!-- /rule-dsl:keys:book_fields -->

`chapters`:

<!-- rule-dsl:keys:chapters -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `item` | string | — | Required; chapter-list item selector |
| `title` | field | `{"attr": "text"}` | Chapter title |
| `url` | field | `{"attr": "href"}` | Chapter link; relative addresses completed against `base_url`, duplicate links de-duplicated |
| `id_regex` | string | empty | Regex extracting the chapter ID from the link; defaults to the last path segment without its extension |
| `id_group` | integer | `1` | Capture group; must be >= 0. Falls back to the whole match when out of range |
| `volume_name` | string | `正文` | Volume name to group the chapter under |
| `is_desc` | boolean | `false` | Set to `true` when the site lists newest first: reverses the **whole** merged chapter list once (not each page) |
| `container` | string | empty | Only used with `preprocess`: passes the serialized element children of this node to the rewrite chain (bare text children are excluded; see below) |
| `preprocess` | step[] | `[]` | HTML rewrite chain applied before parsing the chapter list; see below |
<!-- /rule-dsl:keys:chapters -->

---

### `chapter`: chapter body

<!-- rule-dsl:keys:chapter -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Required; chapter-page template. Supports `{base_url}` / `{book_id}` / `{chapter_id}` |
| `content` | field | `{"selector": "body"}` | Body extraction |
| `title` | field | empty | Chapter title (optional) |
| `replace` | `[string, string][]` | `[]` | Literal replacements executed in array order |
| `drop` | string[] | `[]` | Regex array; a line matching any regex is removed **as a whole line** |
| `pages` | object \| string | empty | In-chapter pagination rule |
| `headers` | object | `{}` | Request headers for this section; values support template variables |
| `format` | string | inherited from top level | Response format for this section |
| `container` | string | empty | Only used with `preprocess`: passes the serialized element children of this node to the rewrite chain (bare text children are excluded; see below) |
| `preprocess` | step[] | `[]` | HTML rewrite chain applied before parsing the body; see below |
<!-- /rule-dsl:keys:chapter -->

The body processing order is fixed: extract page by page → join with `"\n"` → `chapter.replace` → line-wise `drop` → `strip` both ends. An empty result returns `None`; no empty chapter is written.

---

### `pages`: pagination

<!-- rule-dsl:keys:pages -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | — | Required; page-number template. Supports `{page}` and the other variables of the enclosing section |
| `start` | integer | `2` | First page number (page 1 is fetched from the main link) |
| `max_pages` | integer | empty | When set, fetch exactly `start` through `max_pages` and stop relying on a next-page link |
| `stop_when` | string | empty | Regex (with `re.S`); stop paging when it matches the current page body (the current page is kept) |
| `loop_guard` | boolean | `true` | Stop when a URL repeats or the response body is unchanged, preventing self-referential loops |
<!-- /rule-dsl:keys:pages -->

Without `max_pages`, the engine checks whether the current page still contains the next-page URL (full suffix or file name) and continues if it does; the hard cap on extra pages is 50. `stop_when` checks the **entry page** first (a match fetches no extra pages), then each fetched page is checked together with `loop_guard`, whichever fires first. `loop_guard` stops on a repeated URL or an identical response-body digest; set it to `false` to restore the unprotected heuristic.

When `max_pages` is smaller than `start`, nothing is reported as an error; no extra page is fetched. JSON APIs usually do not link the next page in the response, so they **must** use `max_pages` (optionally with `stop_when` matching an empty list to stop early); with only `stop_when` and no `max_pages`, the first extra request is never made because the current page has no next-page URL. Chapter-list pages are merged with link de-duplication; body pages are concatenated in order.

---

### `field`: field extraction

`field` may be a string (equivalent to `{"selector": "..."}`) or an object:

<!-- rule-dsl:keys:field -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `selector` | string | empty | Selector; empty means use the current node directly |
| `attr` | string | `text` | Value mode, see below; an empty string is invalid |
| `regex` | string | empty | Performs one regex **search** on the extracted value (`re.S`, `.` matches newlines) |
| `group` | integer | `1` | Regex capture group; must be >= 0. Falls back to the whole match when out of range |
| `multi` | boolean | `false` | Collect every match; currently honoured only for `book.fields.tags`, other positions still take the first non-empty match |
| `replace` | `[string, string][]` | `[]` | Literal replacements applied one by one after the regex |
| `default` | string | `""` | Fallback value when every selector and fallback entry yields nothing |
| `steps` | step[] | `[]` | Post-processing chain executed after `regex` / `replace`; see below |
| `fallback` | (string \| field)[] | `[]` | Selector fallback chain, tried in order when the primary selector is empty; see below |
<!-- /rule-dsl:keys:field -->

Execution order: `selector` picks nodes → `attr` extracts a value → `regex` / `group` → `replace` → `steps` → `strip` both ends. `steps` runs only when the current value is non-empty, so a constant expression cannot invent content when the selector matched nothing. `default` is used only after the main field and every fallback have failed.

`attr` values:

| Value | HTML mode | JSON mode |
| --- | --- | --- |
| `text` | Descendant text, whitespace-collapsed and concatenated | A scalar returns itself (numbers / booleans become strings); an object is serialized as JSON text; an array concatenates each element's text with an empty separator |
| `inner_text` | Descendant text joined by lines, preserving paragraphs | A scalar returns itself; an object is likewise serialized as JSON text; an array is joined with `\n` (only when the whole match is the array, see the note below) |
| `html` | The node's inner HTML | An object is serialized as JSON text; an array concatenates each element's text with an empty separator |
| Any other string | Reads the HTML attribute of that name, such as `href` / `src` / `content` | Reads the object key of that name; an array is serialized as JSON text |

> **Note (JSON arrays)**: when the **last segment** of a path points at an array, the engine first expands the array into multiple nodes (see the `selector` syntax below) and applies `attr` to each node. So if `data.content` is an array of paragraphs, only the first non-empty element is taken rather than all of them being joined. If you need the whole text, have the API return a string (the common case), or point the field at an object containing the array and process it with other fields.

`selector` syntax:

- **HTML**: a selector starting with `/`, `./` or `(` is parsed as XPath; anything else as CSS;
- **JSON**: a dot path. `data.list` takes an array and expands it automatically; `[0]` is an explicit index (no expansion); `[*]` expands every element; `items.title` implicitly takes `title` from each array element; a leading `$` and `.` may be omitted. Array expansion happens at the **last segment**, so when the last segment is an array the field only sees one expanded element (see the note above).

---

### `step`: post-processing chain and HTML rewriting

`steps` (field post-processing) and `preprocess` (HTML rewriting before parsing) use the same kind of entry:

<!-- rule-dsl:keys:step -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `lang` | string | — | Required; `js` or `py` |
| `code` | string | empty | Required when `lang=js`: a mini-js program |
| `name` | string | empty | Required when `lang=py`: a whitelisted function name |
<!-- /rule-dsl:keys:step -->

#### `py` steps

`name` may only be taken from the engine's explicit whitelist (pure `str -> str`, no side effects):

`strip_html` / `normalize_space` / `dedupe_lines` / `base64_decode` / `url_decode` / `url_encode` / `html_unescape` / `strip` / `lower` / `upper` / `reverse`

A rule file **cannot inline Python code**: book sources are external input, and allowing executable Python would be remote code execution. An unknown name fails during `parse_rule` and lists the available names. A `py` step may only reference the names above and **cannot** use the multi-argument helpers reserved for `js` steps below (they belong to a different whitelist).

#### `js` steps

`code` is executed by `novel_downloader.libs.mini_js`, a deliberately small subset:

- **Supported**: `let` declarations, `function` definitions and calls, `return` inside function bodies, expression statements, arithmetic (`+ - * /`), ternary expressions, object / array literals, and member access on dictionaries / lists (`obj.key`, `arr[0]`);
- **Not supported**:
  - `if`, `for` / `while` and other loops, regex literals (`/re/`) — these fail during **parsing**;
  - string prototype methods (`.replace` / `.split` / `.trim` / `.toUpperCase`) — these parse but fail at **runtime** (`Member access on non-object`); strings also have no `.length`;
  - `new` and any global constructor / global function (`parseInt` and friends) — runtime `NameError`;
  - a bare top-level `return` — allowed only inside a function body;
- The current string is bound as `input` (alias `result`);
- The `py` whitelist functions above are injected as callable functions, so `normalize_space(input)` is valid while `input.trim()` is not.

Because mini-js has no string methods, rewriting HTML cannot use `.replace`. Two multi-argument helpers are injected **for `js` steps only** (they are not in the `py` whitelist; `{"lang": "py", "name": "replace_all"}` fails at load time):

| Helper | Description |
| --- | --- |
| `replace_all(text, old, new)` | Literal replacement of every occurrence |
| `regex_replace(text, pattern, repl)` | Replacement with Python `re.sub` semantics; use `\1`, not `$1`, in the replacement string |

```json
"preprocess": [
  {"lang": "js", "code": "regex_replace(input, '<div class=\"ad\">.*?</div>', '')"}
]
```

**Regex backslashes pass through three layers**: the rule JSON string → the mini-js string literal → Python `re`. To express `\d+`, write **four backslashes** in the `.json` file:

```json
{"lang": "js", "code": "regex_replace(input, '\\\\d+', '#')"}
```

After JSON decoding this is `regex_replace(input, '\\d+', '#')`; mini-js turns `\\` into `\`, and the regex finally receives `\d+`. Writing only two backslashes produces `\d` → `d` in the JS string, and the regex silently matches nothing (no error). This is the easiest trap.

Unsupported constructs raise a `RuleError` with the rule location, either at load time or at runtime; they are never returned unchanged.

```json
"title": {
  "selector": "h1",
  "steps": [
    {"lang": "js", "code": "normalize_space(strip_html(input))"},
    {"lang": "py", "name": "strip"}
  ]
}
```

**Real site problems this solves**: titles that mix HTML fragments, full-width spaces and repeated text such as "latest chapter"; duplicated anti-piracy lines in the body; per-site base64 / URL decoding.

#### `preprocess` and `container`

`book.chapters.preprocess` and `chapter.preprocess` rewrite the HTML **before parsing**:

```json
"chapters": {
  "item": "dd a",
  "container": "div#catalog",
  "preprocess": [
    {"lang": "py", "name": "html_unescape"}
  ]
}
```

- When `container` is empty, the rewrite chain receives the whole raw response; when set, it receives that node's **inner HTML**;
- This inner HTML is the serialized **element children**; bare text nodes directly under the container do not enter the rewrite chain. If the whole catalog is escaped plain text (typically `&lt;dd&gt;&lt;a ...&gt;` pasted directly into `div#catalog`), using `container` yields an empty document and an error — leave `container` empty and rewrite the whole page instead;
- If `container` matches no node, this is an error rather than silently producing zero chapters;
- The rewrite result must be non-empty, parseable HTML, otherwise it is an error;
- The chain runs in array order; each step's output is the next step's input;
- Only available with `format: "html"`; using `preprocess` in a JSON section is an error;
- The rewritten document is the **new root**, and every selector runs against the rewritten tree. With `container`, the container itself is no longer in the tree, so `item` / `content` must not use it as a prefix (`container: "div#catalog"` means `item` is `dd a`, not `div#catalog dd a`);
- `book.chapters.preprocess` affects **chapter-list parsing only**; `book.fields` details are still extracted from the original page. `chapter.preprocess` affects **the whole body section**, including `chapter.title` and `chapter.content`, so putting `h1` outside `container` makes the title unreachable.

**Real site problems this solves**: a catalog that is double-escaped (`&lt;a href...&gt;`) and must be unescaped first; a site that hides the chapter list in a `display:none` decoy block; relative links that must be normalized to absolute URLs.

#### `fallback` chains

```json
"book_name": {
  "selector": "meta[property='og:novel:book_name']",
  "attr": "content",
  "fallback": [
    {"selector": "h1.book-name", "attr": "text"},
    {"selector": "//h1[@class='book-name']", "attr": "text"}
  ]
}
```

- A string entry **inherits** the main field's `attr` / `regex` / `group` / `replace` / `steps` and only replaces `selector`;
- An object entry is an independent field (it may bring its own `attr` / `regex` / `steps` and so on);
- Fallbacks are tried only when the whole main chain is empty; the first non-empty result wins;
- A fallback entry's own `default` has no effect; the main field's `default` is the final fallback.

> **The inherited-`attr` trap**: when the main field is a `meta` with `attr: "content"`, a string fallback also uses `attr: "content"` to read a `content` attribute from `<h1>` — which does not exist, so the result is empty and the fallback **appears not to work**. When mixing `meta` and `h1`, write the fallback as an object with an explicit `attr: "text"` (as above).

**Real site problems this solves**: an A/B-tested details page (one template uses `meta`, the other `h1`); different field layouts on the main site and a mirror; old pages without a summary block.

---

### `limits`: per-rule concurrency and retries

```json
"limits": {
  "concurrency": 2,
  "min_interval": 1.0,
  "max_interval": 2.5,
  "max_attempts": 3,
  "retry_min_interval": 5,
  "retry_max_interval": 20
}
```

<!-- rule-dsl:keys:limits -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `concurrency` | integer | empty | Concurrent requests for this source; must be >= 1. `0` is invalid |
| `min_interval` | number | empty | Lower bound for the interval between requests, **in seconds** |
| `max_interval` | number | empty | Upper bound for the interval between requests, **in seconds** |
| `max_attempts` | integer | empty | Maximum attempts per request (including the first); must be >= 1 |
| `retry_min_interval` | number | empty | Lower bound for retry backoff, **in seconds** |
| `retry_max_interval` | number | empty | Upper bound for retry backoff, **in seconds** |
<!-- /rule-dsl:keys:limits -->

Constraints and mapping:

- `min_interval <= max_interval` and `retry_min_interval <= retry_max_interval`; a reversed range fails at parse time;
- Unset fields leave the global configuration unchanged; merging is **immutable** (`dataclasses.replace` produces a new config) and never mutates the shared global object;
- `concurrency` maps to `max_connections`;
- `min_interval` / `max_interval` are averaged and mapped to `request_interval`;
- `max_attempts` maps to `retry_times = max_attempts - 1` (the latter counts only retries after the first failure);
- `retry_min_interval` / `retry_max_interval` are averaged and mapped to `backoff_factor`.

**Real site problems this solves**: a source that answers 429/403 under frequent requests needs 1 concurrency and a 2-second interval without slowing other sources; a small site needs serial fetching to avoid an IP ban; an occasional 502 needs more retries and longer backoff.

---

### `search.single_result_fallback`: single-result fallback

Defaults to `false`. When enabled, if the `item` selector matches **no entries**, the engine treats the whole response as one search result, but only with one of these pieces of evidence:

- the final response URL differs from the request URL (a redirect happened); or
- a non-empty `book.fields.book_name` can be extracted from the page (or `search.fields.title` as a fallback).

The result link uses the **final response URL**; metadata prefers `book.fields` and falls back to `search.fields`:

| Output | Preferred (`book.fields`) | Fallback (`search.fields`) |
| --- | --- | --- |
| Title | `book_name` | `title` |
| Author | `author` | `author` |
| Cover | `cover_url` | `cover` |
| Update time | `update_time` | `update` |
| Word count | `word_count` | `word` |

`latest` is always empty in single-result fallback.

```json
"search": {
  "url": "https://example.test/search?q={keyword}",
  "item": "ul.result-list > li",
  "single_result_fallback": true,
  "fields": {
    "title": {"selector": "h1.book-name", "attr": "text"},
    "url": {"selector": "h1.book-name a", "attr": "href"}
  }
}
```

**Real site problems this solves**: an exact-title search where the site 302s to the details page and the response has no result list; a single match rendered as one card without a list container. It only applies to "zero results" and never appends an extra entry when items matched normally, avoiding duplicates.

---

### `resolve`: link resolution

<!-- rule-dsl:keys:resolve -->
| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `book` | string | empty | Regex extracting `book_id` from the URL path; the first capture group is the ID |
| `chapter` | string | empty | Regex extracting `book_id` / `chapter_id` from the URL path; the two capture groups correspond in order |
<!-- /rule-dsl:keys:resolve -->

`chapter` is matched before `book`; when both are empty, a link matching the rule's `hosts` still does not resolve to an in-site ID.

---

### Template variables

| Variable | Available in | Description |
| --- | --- | --- |
| `{base_url}` | book / chapter URLs (including their `pages.url`), `search.url`, book / chapter `headers` values | Site root (trailing `/` removed) |
| `{book_id}` | book / chapter URLs (including their `pages.url`), book / chapter `headers` values | Book ID |
| `{chapter_id}` | chapter URL (including `pages.url`), chapter `headers` values | Chapter ID |
| `{page}` | `pages.url`, `search.url`, `search` `data` / `json_body` values | Page number; fixed to `1` during search |
| `{keyword}` | `search.url`, `search` `data` / `json_body` values | Keyword, URL-encoded |
| `{raw_keyword}` | `search.url`, `search` `data` / `json_body` values | Keyword, as-is |

Substitution is single-pass: inserted values are not scanned again, so a `{book_id}` appearing in page data is not expanded twice. Unrecognized `{...}` is kept as-is.

Two boundaries worth noting:

- **Top-level `headers` and `search.headers` do not undergo template substitution**; variables only take effect in book / chapter `headers`;
- `search` `data` / `json_body` expand only `{keyword}` / `{raw_keyword}` / `{page}`; a `{base_url}` there is left for the site as-is. If you need the site root, put it directly in `search.url` or a fixed `data` value.

---

### Units and value conventions

- **All interval and backoff fields are in seconds (number)**, never milliseconds. A field name without a unit means seconds (`1.5` is 1.5 seconds). This convention comes from a real incident: a template comment said "seconds" while the code interpreted milliseconds, making the actual interval one thousandth of what was intended.
- **`concurrency` must be an integer >= 1. `0` is neither "unlimited" nor "serial"; it is invalid**, because a zero-permit semaphore blocks forever. Use `1` for serial.
- `max_attempts >= 1` and `concurrency >= 1` are enforced at parse time; all `group` / `id_group` / `book_id_group` values must be >= 0, also enforced at parse time.
- `max_pages` has **no** parse-time validation; when it is smaller than `start`, no extra page is fetched and no error is raised.
- A larger `priority` sorts earlier; within search results, `0`, `1`, `2` ... are added in order for stable ordering of ties.
- `encoding` is not validated at parse time; it must be a Python-recognized codec name. The response is decoded with it during download, and a typo fails at the decoding stage.

---

### Do not do this

#### Do not use a string DSL such as `selector@js:code`

Incorrect (do not copy):

```json
"content": {"selector": "//div[@class='content']@js:code"}
```

Correct:

```json
"content": {
  "selector": "//div[@class='content']",
  "steps": [{"lang": "py", "name": "normalize_space"}]
}
```

Why the `@` approach cannot work:

1. **`@` is part of an attribute predicate in XPath.** `//div[@class='content']` is valid XPath; splitting the selector and post-processing at the first `@` yields `//div[` and `class='content']...`, and XPath parsing fails immediately. `//a[@href]`, `//img[@data-src]` and `(//div[@id='x'])[1]` are all truncated.
2. **`@` can also appear in CSS and URLs.** Attribute selectors may match values containing `@` (`a[href*='@']`), URLs often contain `@` (email addresses, CDN paths), and mini-js code often contains `/@/`. A string delimiter without escaping rules cannot express these safely.
3. **A chained multi-segment syntax has no schema.** `selector@js:a;@py:b` can only be parsed by convention; typos, omissions and extra segments are not caught by `parse_rule` and never enter the consistency checks in this page and the tests.
4. **An explicit array can be validated.** `steps` / `fallback` / `preprocess` are structured data: types, lengths and `lang` values are all validated at load time, consistent with the project's "unknown key is an error" policy.

#### Do not write full JavaScript in `steps`

`js` steps run on mini-js, not a full V8/Node. `if`, loops, regex literals and `.replace` / `.split` / `.trim` are not supported. When you need those, prefer a `py` step from the whitelist; if that is still not enough, write a Python plugin for the site instead of piling unsupported code into a rule.

#### Do not use `preprocess` as body cleaning

`preprocess` receives the whole page HTML (or the `container`'s inner HTML) and runs before parsing. Using it to delete body content deletes every paragraph and cannot be recovered by the export-stage processors. Clean the body with `chapter.replace` (literal replacement), `chapter.drop` (line-wise regex removal) or `chapter.content.steps` (post-processing chain).

#### Do not use `py` steps to execute arbitrary code

`{"lang": "py", "name": "..."}` only references functions in the engine whitelist. Rule files are external input; allowing them to carry executable Python would turn "download a book source" into "remote code execution".

#### Do not confuse units and boundary values

- `"min_interval": 500` means 500 seconds, not 500 milliseconds;
- `"concurrency": 0` is invalid, not "automatic" or "unlimited";
- Do not use `max_pages` as "total page limit"; it means "the last page number to fetch" and should be `>= start` (reversing it raises no error, it just fetches no extra pages).

#### Do not copy third-party source rules

Some similar open-source projects use AGPL-3.0, and their rule JSON is copyrighted content of those projects. You may refer to the rule **structure** and **field design**, but site selectors and rule documentation must be written yourself, using this project's own field names and validation rules.
