#!/usr/bin/env python3
"""
Executable checks for the JSON examples in ``docs/guide/rule-dsl.md``.

The field tables are covered by ``test_rule_docs.py``; this module makes the
*examples* part of the test suite as well:

* every fenced ``json`` block is extracted from the Chinese master page and
  must be byte-identical in the English translation;
* every block is wrapped into a minimal valid rule where needed and parsed
  with the real :func:`parse_rule` (no "example whitelist" escape hatch);
* the interesting blocks are then driven through ``RuleParser`` /
  ``RuleSearcher`` / ``apply_steps`` / ``apply_limits`` with synthetic
  offline pages, and the result is asserted against what the prose promises
  (steps, regex escaping, ``container``, ``fallback``, pagination limits,
  single-result fallback, ...).

All checks are offline and read-only.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from novel_downloader.plugins.rule.engine import (
    PY_STEP_FUNCTIONS,
    apply_steps,
    extract,
    matches_stop_when,
    parse_page,
)
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.model import (
    MAX_EXTRA_PAGES,
    Field,
    Limits,
    Rule,
    RuleError,
    Step,
    apply_limits,
    parse_rule,
)
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.schemas import FetcherConfig, ParserConfig

ROOT = Path(__file__).resolve().parents[2]
ZH_DOC = ROOT / "docs" / "guide" / "rule-dsl.md"
EN_DOC = ROOT / "docs" / "guide" / "rule-dsl.en.md"

_JSON_FENCE_RE = re.compile(r"```json\n(.*?)```", re.S)
_KEY_BLOCK_RE = re.compile(
    r"<!--\s*rule-dsl:keys:(\w+)\s*-->(.*?)<!--\s*/rule-dsl:keys:\1\s*-->", re.S
)
_KEYS_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.M)
_DOC_STEP_NAMES_RE = re.compile(r"^`([^`]+)`(?:\s*/\s*`([^`]+)`)+$", re.M)

_SITE_KEY = "doc-example"

#: ``site_key`` of every rule in the "complete examples" section.  Keeping the
#: list here means a renamed or deleted example fails the test instead of
#: silently reducing coverage.
_DOC_EXAMPLE_KEYS = (
    "doc-search-paged",
    "doc-catalog-next",
    "doc-content-steps",
    "doc-json-api",
    "doc-auth-headers",
)

_BASE_BOOK: dict[str, Any] = {
    "url": "https://doc.example/book/{book_id}/",
    "fields": {},
    "chapters": {
        "item": "dd a",
        "title": {"attr": "text"},
        "url": {"attr": "href"},
    },
}

_BASE_CHAPTER: dict[str, Any] = {
    "url": "https://doc.example/chapter/{chapter_id}",
    "content": {"selector": "body"},
}


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def _read_doc(path: Path) -> str:
    assert path.is_file(), f"missing rule DSL doc: {path}"
    return path.read_text(encoding="utf-8")


def _json_blocks(path: Path) -> list[str]:
    blocks = [match.group(1) for match in _JSON_FENCE_RE.finditer(_read_doc(path))]
    assert blocks, f"no ```json examples found in {path}"
    return blocks


def _key_tables(path: Path) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for name, body in _KEY_BLOCK_RE.findall(_read_doc(path)):
        tables[str(name)] = {str(row) for row in _KEYS_ROW_RE.findall(str(body))}
    return tables


ZH_BLOCKS = _json_blocks(ZH_DOC)
EN_BLOCKS = _json_blocks(EN_DOC)


def _load_fragment(text: str) -> dict[str, Any]:
    """
    Decode one fenced block.

    Most blocks are object *fragments* without the outer braces
    (``"limits": {...}``); a few are a bare object (the step examples).  Both
    shapes are supported so the test never needs a per-block special case just
    to get valid JSON.
    """
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        value = json.loads("{" + text + "}")
    assert isinstance(value, dict), f"example is not a JSON object: {text!r}"
    return cast(dict[str, Any], value)


def _wrap_fragment(fragment: dict[str, Any]) -> dict[str, Any]:
    """Embed a documentation fragment in the smallest valid rule around it."""
    keys = set(fragment)
    base: dict[str, Any] = {"site_key": _SITE_KEY}

    if "site_key" in keys:
        # A complete rule (the private-source walkthrough and the "complete
        # examples" section) needs no wrapping: it is already a full document.
        return fragment

    if "lang" in keys and keys <= {"lang", "code", "name"}:
        return {
            **base,
            "chapter": {
                **_BASE_CHAPTER,
                "title": {"selector": "h1", "steps": [fragment]},
            },
        }
    if "preprocess" in keys:
        return {**base, "chapter": {**_BASE_CHAPTER, **fragment}}
    if "title" in keys:
        return {
            **base,
            "book": {**_BASE_BOOK, "fields": {"book_name": fragment["title"]}},
        }
    if "chapters" in keys:
        return {**base, "book": {**_BASE_BOOK, "chapters": fragment["chapters"]}}
    if "book_name" in keys:
        return {**base, "book": {**_BASE_BOOK, "fields": fragment}}
    if "limits" in keys:
        return {**base, "chapter": _BASE_CHAPTER, "limits": fragment["limits"]}
    if "search" in keys:
        return {**base, "chapter": _BASE_CHAPTER, "search": fragment["search"]}
    if "content" in keys:
        return {**base, "chapter": {**_BASE_CHAPTER, **fragment}}

    raise AssertionError(
        "unrecognised documentation example; teach _wrap_fragment about it: "
        + ", ".join(sorted(keys))
    )


def _decoded_blocks() -> list[dict[str, Any]]:
    return [_load_fragment(block) for block in ZH_BLOCKS]


def _fragments() -> list[dict[str, Any]]:
    """Blocks that are *not* complete rules (no ``site_key``)."""
    return [obj for obj in _decoded_blocks() if "site_key" not in obj]


def _complete_rules() -> list[dict[str, Any]]:
    """Blocks that are full rule documents (they carry ``site_key``)."""
    return [obj for obj in _decoded_blocks() if "site_key" in obj]


def _complete_rule(site_key: str) -> dict[str, Any]:
    matches = [obj for obj in _complete_rules() if obj["site_key"] == site_key]
    assert len(matches) == 1, f"expected exactly one complete example for {site_key!r}"
    return matches[0]


def _block_with_key(key: str) -> dict[str, Any]:
    # Complete rules contain nested sections too; a key lookup means "the
    # snippet that documents this key", so only fragments are considered.
    matches = [obj for obj in _fragments() if key in obj]
    assert len(matches) == 1, f"expected exactly one documented {key!r} example"
    return matches[0]


def _blocks_with_key(key: str) -> list[dict[str, Any]]:
    return [obj for obj in _fragments() if key in obj]


# --------------------------------------------------------------------------- #
# drivers
# --------------------------------------------------------------------------- #
def _parser(rule: Rule) -> RuleParser:
    cls = type(
        f"RuleParser_{rule.site_key}",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    return cast(RuleParser, cls(ParserConfig()))


def _searcher(rule: Rule) -> RuleSearcher:
    priority = rule.search.priority if rule.search else rule.priority
    cls = type(
        f"RuleSearcher_{rule.site_key}",
        (RuleSearcher,),
        {
            "_rule": rule,
            "site_name": rule.site_key,
            "BASE_URL": rule.base_url,
            "priority": priority,
        },
    )
    return cast(RuleSearcher, cls(session=None))


# --------------------------------------------------------------------------- #
# every documented example loads
# --------------------------------------------------------------------------- #
def test_json_examples_are_identical_across_languages() -> None:
    assert ZH_BLOCKS == EN_BLOCKS, (
        "the fenced json examples in rule-dsl.md and rule-dsl.en.md differ; "
        "keep the examples byte-identical in both pages"
    )


def test_field_tables_cover_the_same_keys_in_both_languages() -> None:
    assert _key_tables(ZH_DOC) == _key_tables(EN_DOC), (
        "the rule-dsl:keys:* field tables list different fields in the "
        "Chinese and English pages"
    )


@pytest.mark.parametrize(
    "index", range(len(ZH_BLOCKS)), ids=[f"block-{i}" for i in range(len(ZH_BLOCKS))]
)
def test_every_documented_json_example_loads(index: int) -> None:
    fragment = _load_fragment(ZH_BLOCKS[index])
    rule = parse_rule(_wrap_fragment(fragment), source=f"rule-dsl.md block {index}")
    if "site_key" not in fragment:
        assert rule.site_key == _SITE_KEY


# --------------------------------------------------------------------------- #
# prose <- -> engine
# --------------------------------------------------------------------------- #
def test_py_step_whitelist_documented_in_both_languages_matches_engine() -> None:
    expected = set(PY_STEP_FUNCTIONS)
    for path in (ZH_DOC, EN_DOC):
        match = _DOC_STEP_NAMES_RE.search(_read_doc(path))
        assert match is not None, f"no py-step whitelist paragraph in {path}"
        documented = set(re.findall(r"`([^`]+)`", match.group(0)))
        assert documented == expected, (
            f"{path.name} documents py steps {sorted(documented)} but the "
            f"engine whitelist is {sorted(expected)}"
        )


# --------------------------------------------------------------------------- #
# steps / escaping examples
# --------------------------------------------------------------------------- #
def test_title_steps_example_normalises_html_and_width_space() -> None:
    title = _block_with_key("title")["title"]
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "book": {**_BASE_BOOK, "fields": {"book_name": title}},
        }
    )
    assert rule.book is not None
    # The escaped tags are literal text after HTML parsing, so strip_html is
    # what removes them; U+3000 and the 4-space run are collapsed by
    # normalize_space, then the py strip step trims the result.
    page = parse_page("<h1>&lt;b&gt;Latest&lt;/b&gt;\u3000Chapter    1</h1>")
    assert extract(page, rule.book.fields["book_name"]) == "Latest Chapter 1"


def test_preprocess_regex_example_removes_the_ad_block() -> None:
    fragment = _block_with_key("preprocess")
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "chapter": {
                **_BASE_CHAPTER,
                "content": {"selector": "div#content", "attr": "inner_text"},
                **fragment,
            },
        }
    )
    chapter = _parser(rule).parse_chapter_content(
        [
            '<html><body><div id="content">'
            '<div class="ad">AD</div>keep</div></body></html>'
        ],
        "1",
    )
    assert chapter is not None
    assert chapter["content"] == "keep"


def test_regex_backslash_example_reaches_python_re() -> None:
    fragment = next(
        block
        for block in ZH_BLOCKS
        if (obj := _load_fragment(block)).get("lang") == "js"
        and "\\\\d+" in str(obj.get("code", ""))
    )
    step = Step.from_dict(_load_fragment(fragment))
    assert apply_steps("abc123", [step]) == "abc#"

    # The documented trap: with two backslashes in the .json file (one after
    # JSON decoding) the JS string loses the backslash and the regex becomes
    # ``d+``, which silently leaves digits untouched.
    weakened_step = Step.from_dict(
        {"lang": step.lang, "code": step.code.replace("\\\\", "\\")}
    )
    assert apply_steps("abc123", [weakened_step]) == "abc123"


# --------------------------------------------------------------------------- #
# preprocess + container
# --------------------------------------------------------------------------- #
def test_container_example_parses_a_double_escaped_catalog() -> None:
    chapters = _block_with_key("chapters")["chapters"]
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "base_url": "https://novel.example.test",
            "book": {
                "url": "{base_url}/book/{book_id}/",
                "fields": {},
                "chapters": chapters,
            },
        }
    )
    page = """<html><body>
    <div id="catalog"><div class="payload">
      &lt;dd&gt;&lt;a href="/book/7/101.html"&gt;  Chapter One  &lt;/a&gt;&lt;/dd&gt;
      &lt;dd&gt;&lt;a href="/book/7/102.html"&gt;Chapter Two&lt;/a&gt;&lt;/dd&gt;
    </div></div></body></html>"""
    info = _parser(rule).parse_book_info([page])
    assert info is not None
    found = info["volumes"][0]["chapters"]
    assert [(item["title"], item["url"], item["chapterId"]) for item in found] == [
        ("Chapter One", "https://novel.example.test/book/7/101.html", "101"),
        ("Chapter Two", "https://novel.example.test/book/7/102.html", "102"),
    ]


def test_container_example_rejects_bare_text_children() -> None:
    chapters = _block_with_key("chapters")["chapters"]
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "book": {**_BASE_BOOK, "chapters": chapters},
        }
    )
    bare_text = (
        '<div id="catalog">'
        '&lt;dd&gt;&lt;a href="/book/7/101.html"&gt;Chapter One&lt;/a&gt;&lt;/dd&gt;'
        "</div>"
    )
    with pytest.raises(RuleError, match="produced an empty document"):
        _parser(rule).parse_book_info([bare_text])


def test_container_example_requires_the_container_to_exist() -> None:
    chapters = _block_with_key("chapters")["chapters"]
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "book": {**_BASE_BOOK, "chapters": chapters},
        }
    )
    with pytest.raises(RuleError, match="matched nothing"):
        _parser(rule).parse_book_info(['<div id="not-the-catalog">x</div>'])


def test_preprocess_example_is_rejected_in_json_mode() -> None:
    fragment = _block_with_key("preprocess")
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "chapter": {
                **_BASE_CHAPTER,
                "format": "json",
                "content": {"selector": "content"},
                **fragment,
            },
        }
    )
    with pytest.raises(RuleError, match="preprocess requires 'format': 'html'"):
        _parser(rule).parse_chapter_content(['{"content": "x"}'], "1")


# --------------------------------------------------------------------------- #
# fallback
# --------------------------------------------------------------------------- #
def test_fallback_example_prefers_meta_then_the_object_selector() -> None:
    fragment = _block_with_key("book_name")
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "book": {**_BASE_BOOK, "fields": fragment},
        }
    )
    assert rule.book is not None
    field = rule.book.fields["book_name"]

    with_meta = parse_page(
        '<meta property="og:novel:book_name" content="Meta Name">'
        '<h1 class="book-name">Fallback Name</h1>'
    )
    assert extract(with_meta, field) == "Meta Name"

    without_meta = parse_page('<h1 class="book-name">Fallback Name</h1>')
    assert extract(without_meta, field) == "Fallback Name"


def test_string_fallback_inherits_the_main_attr_as_documented() -> None:
    fragment = _block_with_key("book_name")
    base = fragment["book_name"]
    inherited = Field.from_dict({**base, "fallback": ["h1.book-name"]})
    # The main field reads attr="content"; <h1> has no content attribute, so
    # the string fallback yields nothing.  This is the trap the doc warns about.
    page = parse_page('<h1 class="book-name">Fallback Name</h1>')
    assert extract(page, inherited) == ""


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #
def test_limits_example_maps_onto_fetcher_config() -> None:
    fragment = _block_with_key("limits")
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "chapter": _BASE_CHAPTER,
            "limits": fragment["limits"],
        }
    )
    assert rule.limits is not None
    original = FetcherConfig()
    merged = apply_limits(original, rule.limits)

    assert merged is not original
    assert merged.max_connections == 2
    assert merged.retry_times == 2
    assert merged.request_interval == pytest.approx(1.75)
    assert merged.backoff_factor == pytest.approx(12.5)
    # Merging is immutable: the shared config object is untouched.
    assert original == FetcherConfig()


def test_limits_interval_is_capped_at_max_over_jitter() -> None:
    limits = Limits.from_dict({"min_interval": 5.0, "max_interval": 5.0})
    merged = apply_limits(FetcherConfig(), limits)
    # The fetcher sleeps interval * uniform(1.0, 1.1); the cap keeps the
    # jittered sleep from exceeding the declared max_interval.
    assert merged.request_interval == pytest.approx(5.0 / 1.1)


# --------------------------------------------------------------------------- #
# single-result fallback
# --------------------------------------------------------------------------- #
def _search_rule(fragment: dict[str, Any]) -> Rule:
    return parse_rule(
        {
            "site_key": _SITE_KEY,
            "chapter": _BASE_CHAPTER,
            "search": fragment["search"],
        }
    )


def test_single_result_fallback_example_uses_the_final_url_and_search_title() -> None:
    rule = _search_rule(_block_with_key("search"))
    searcher = _searcher(rule)
    searcher._request_url = "https://example.test/search?q=a"
    searcher._final_url = searcher._request_url

    page = '<h1 class="book-name">Only One<a href="/book/9"></a></h1>'
    results = searcher._parse_html(page)
    assert len(results) == 1
    assert results[0]["title"] == "Only One"
    assert results[0]["book_url"] == searcher._final_url
    assert results[0]["latest_chapter"] == ""
    assert results[0]["word_count"] == "-"


def test_single_result_fallback_example_needs_evidence() -> None:
    rule = _search_rule(_block_with_key("search"))
    searcher = _searcher(rule)
    searcher._request_url = "https://example.test/search?q=a"
    searcher._final_url = searcher._request_url
    assert searcher._parse_html("<div>no results and no title</div>") == []


def test_single_result_fallback_example_accepts_a_redirect() -> None:
    rule = _search_rule(_block_with_key("search"))
    searcher = _searcher(rule)
    searcher._request_url = "https://example.test/search?q=a"
    searcher._final_url = "https://example.test/book/42"

    results = searcher._parse_html("<div>detail page without a title</div>")
    assert len(results) == 1
    assert results[0]["book_url"] == "https://example.test/book/42"


def test_single_result_fallback_prefers_book_fields_as_documented() -> None:
    fragment = _block_with_key("search")
    search = fragment["search"]
    search_fields = {
        **search["fields"],
        "author": {"selector": ".search-author"},
        "cover": {"selector": ".search-cover", "attr": "src"},
        "update": {"selector": ".search-update"},
        "word": {"selector": ".search-word"},
    }
    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "base_url": "https://example.test",
            "chapter": _BASE_CHAPTER,
            "search": {**search, "fields": search_fields},
            "book": {
                **_BASE_BOOK,
                "fields": {
                    "book_name": {"selector": "h1.book-name"},
                    "author": {"selector": ".book-author"},
                    "cover_url": {"selector": ".book-cover", "attr": "src"},
                    "update_time": {"selector": ".book-update"},
                    "word_count": {"selector": ".book-word"},
                },
            },
        }
    )
    searcher = _searcher(rule)
    searcher._request_url = "https://example.test/book/42"
    searcher._final_url = searcher._request_url
    page = """
    <h1 class="book-name">Book Title</h1>
    <span class="book-author">Book Author</span>
    <img class="book-cover" src="/book.jpg">
    <span class="book-update">2026-01-01</span>
    <span class="book-word">1234</span>
    <span class="search-author">Search Author</span>
    <img class="search-cover" src="/search.jpg">
    <span class="search-update">2025-01-01</span>
    <span class="search-word">999</span>
    """
    results = searcher._parse_html(page)
    assert len(results) == 1
    assert results[0]["title"] == "Book Title"
    assert results[0]["author"] == "Book Author"
    assert results[0]["cover_url"] == "https://example.test/book.jpg"
    assert results[0]["update_date"] == "2026-01-01"
    assert results[0]["word_count"] == "1234"


# --------------------------------------------------------------------------- #
# selector examples
# --------------------------------------------------------------------------- #
def test_correct_content_example_runs_and_wrong_selector_fails() -> None:
    fragments = _blocks_with_key("content")
    correct = next(item for item in fragments if "steps" in item["content"])
    wrong = next(item for item in fragments if "@js:" in item["content"]["selector"])

    rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "chapter": {**_BASE_CHAPTER, "content": correct["content"]},
        }
    )
    chapter = _parser(rule).parse_chapter_content(
        ['<div class="content">  Hello   world  </div>'], "1"
    )
    assert chapter is not None
    assert chapter["content"] == "Hello world"

    bad_rule = parse_rule(
        {
            "site_key": _SITE_KEY,
            "chapter": {**_BASE_CHAPTER, "content": wrong["content"]},
        }
    )
    with pytest.raises(RuleError, match="invalid selector"):
        _parser(bad_rule).parse_chapter_content(
            ['<div class="content">text</div>'], "1"
        )


# --------------------------------------------------------------------------- #
# prose claims about parser limits
# --------------------------------------------------------------------------- #
def test_pages_max_pages_upper_bound_matches_the_documentation() -> None:
    def make(max_pages: int) -> dict[str, Any]:
        return {
            "site_key": _SITE_KEY,
            "book": {
                **_BASE_BOOK,
                "pages": {
                    "url": "https://doc.example/book/{book_id}/p{page}",
                    "start": 2,
                    "max_pages": max_pages,
                },
            },
        }

    parse_rule(make(MAX_EXTRA_PAGES))
    with pytest.raises(RuleError, match="max_pages must be <="):
        parse_rule(make(MAX_EXTRA_PAGES + 1))

    below_start = parse_rule(
        {
            "site_key": _SITE_KEY,
            "book": {
                **_BASE_BOOK,
                "pages": {
                    "url": "https://doc.example/book/{book_id}/p{page}",
                    "start": 5,
                    "max_pages": 2,
                },
            },
        }
    )
    assert below_start.book is not None
    assert below_start.book.pages is not None
    assert below_start.book.pages.max_pages == 2


def test_encoding_is_validated_at_parse_time() -> None:
    parse_rule({"site_key": _SITE_KEY, "chapter": _BASE_CHAPTER, "encoding": "utf-8"})
    with pytest.raises(RuleError, match="not a known text encoding"):
        parse_rule(
            {
                "site_key": _SITE_KEY,
                "chapter": _BASE_CHAPTER,
                "encoding": "definitely-not-a-codec",
            }
        )


@pytest.mark.parametrize(
    "code",
    [
        "if (input) { 1 }",
        "for (let i = 0; i < 2; i = i + 1) { 1 }",
        "/x/.test(input)",
    ],
)
def test_documented_unsupported_js_is_rejected_at_load_time(code: str) -> None:
    with pytest.raises(RuleError, match="unsupported JavaScript"):
        Step.from_dict({"lang": "js", "code": code})


@pytest.mark.parametrize(
    "code",
    [
        "input.trim()",
        "input.replace(1, 2)",
        "new Date()",
        "parseInt(input)",
    ],
)
def test_documented_unsupported_js_fails_at_runtime(code: str) -> None:
    step = Step.from_dict({"lang": "js", "code": code})
    with pytest.raises(RuleError, match="js step failed"):
        apply_steps(" x ", [step])


def test_js_only_helpers_are_not_py_steps() -> None:
    with pytest.raises(RuleError, match="unknown py step"):
        Step.from_dict({"lang": "py", "name": "replace_all"})


# --------------------------------------------------------------------------- #
# complete examples: search + pagination
# --------------------------------------------------------------------------- #
def _complete(site_key: str) -> Rule:
    return parse_rule(_complete_rule(site_key), source=f"rule-dsl.md {site_key}")


def test_complete_examples_are_all_parseable_and_distinct() -> None:
    rules = _complete_rules()
    keys = [str(rule["site_key"]) for rule in rules]
    assert len(set(keys)) == len(keys), "complete examples must use distinct site_keys"
    # The five "complete examples" section entries must all be present.
    assert set(_DOC_EXAMPLE_KEYS) <= set(keys), (
        "missing complete examples: "
        + ", ".join(sorted(set(_DOC_EXAMPLE_KEYS) - set(keys)))
    )
    for rule in rules:
        parsed = parse_rule(rule, source=str(rule["site_key"]))
        assert parsed.book is not None or parsed.chapter is not None


def test_search_paged_example_documents_the_page_variable_and_max_pages() -> None:
    rule = _complete("doc-search-paged")
    assert rule.search is not None
    assert "{page}" in rule.search.url
    assert rule.book is not None and rule.book.pages is not None
    # ``search`` has no pagination section: the only page control is max_pages.
    assert rule.book.pages.max_pages == 12
    assert rule.book.pages.start == 2
    assert rule.search.book_id_group == 1


def test_search_paged_example_extracts_digits_from_a_word_count() -> None:
    rule = _complete("doc-search-paged")
    searcher = _searcher(rule)
    searcher._request_url = "https://doc-search.example/search?q=a&p=1"
    searcher._final_url = searcher._request_url
    page = """
    <ul class="result">
      <li>
        <a class="title" href="/book/77/"> A  Book </a>
        <span class="author"> Au Thor </span>
        <span class="latest">L1</span>
        <span class="words">12.3万字</span>
      </li>
    </ul>
    """
    results = searcher._parse_html(page)
    assert len(results) == 1
    assert results[0]["title"] == "A Book"
    assert results[0]["author"] == "Au Thor"
    assert results[0]["latest_chapter"] == "L1"
    assert results[0]["word_count"] == "12"
    assert results[0]["book_id"] == "77"


# --------------------------------------------------------------------------- #
# complete examples: catalog next-page walk
# --------------------------------------------------------------------------- #
def test_catalog_next_example_walks_pages_and_reverses_desc_order() -> None:
    rule = _complete("doc-catalog-next")
    assert rule.book is not None and rule.book.pages is not None
    pages_rule = rule.book.pages
    # No max_pages: the walk depends on the next link staying visible.
    assert pages_rule.max_pages is None
    assert pages_rule.stop_when is not None
    assert pages_rule.loop_guard is True
    assert rule.book.chapters.is_desc is True

    first = (
        '<ul class="chapter-list"><li><a href="/read/1.html">One</a></li></ul>'
        '<a href="/book/7/page_2.html">next</a>'
    )
    second = (
        '<ul class="chapter-list"><li><a href="/read/2.html">Two</a></li></ul>'
        '<a href="/book/7/page_3.html">next</a>'
    )
    third = (
        '<ul class="chapter-list"><li><a href="/read/3.html">Three</a></li></ul>'
        '<div id="no-more-chapters"></div>'
    )

    class _StubFetcher(RuleFetcher):
        site_name = "doc-catalog-next"

        def __init__(self) -> None:
            self.pages = {
                "https://doc-catalog.example/book/7/page_2.html": second,
                "https://doc-catalog.example/book/7/page_3.html": third,
            }
            self.calls: list[str] = []

        async def _get(
            self, url: str, extra_headers: dict[str, str] | None = None, **kwargs: Any
        ) -> str:
            self.calls.append(url)
            return self.pages[url]

    fetcher = _StubFetcher()
    extra = asyncio.run(
        fetcher._collect_pages(
            pages_rule,
            first,
            rule.base_url,
            None,
            first_url="https://doc-catalog.example/book/7/",
            book_id="7",
        )
    )
    assert fetcher.calls == [
        "https://doc-catalog.example/book/7/page_2.html",
        "https://doc-catalog.example/book/7/page_3.html",
    ]
    assert extra == [second, third]

    info = _parser(rule).parse_book_info([first, *extra])
    assert info is not None
    # is_desc reverses the merged list once: newest first across pages.
    assert [c["title"] for c in info["volumes"][0]["chapters"]] == [
        "Three",
        "Two",
        "One",
    ]


def test_catalog_next_example_stops_when_the_link_disappears() -> None:
    rule = _complete("doc-catalog-next")
    assert rule.book is not None and rule.book.pages is not None
    first = '<ul class="chapter-list"><li><a href="/read/1.html">One</a></li></ul>'

    class _StubFetcher(RuleFetcher):
        site_name = "doc-catalog-next"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def _get(
            self, url: str, extra_headers: dict[str, str] | None = None, **kwargs: Any
        ) -> str:
            self.calls.append(url)
            return "<html>unexpected</html>"

    fetcher = _StubFetcher()
    extra = asyncio.run(
        fetcher._collect_pages(
            rule.book.pages,
            first,
            rule.base_url,
            None,
            first_url="https://doc-catalog.example/book/7/",
            book_id="7",
        )
    )
    assert extra == []
    assert fetcher.calls == []


def test_chapters_rule_has_no_next_key() -> None:
    # The prose states the catalog "next" page comes from ``book.pages``; a
    # literal ``next`` key would be rejected as an unknown key.
    with pytest.raises(RuleError, match="unknown key 'next'"):
        parse_rule(
            {
                "site_key": _SITE_KEY,
                "book": {
                    **_BASE_BOOK,
                    "chapters": {**_BASE_BOOK["chapters"], "next": "a.next"},
                },
            }
        )


# --------------------------------------------------------------------------- #
# complete examples: content steps + fallback
# --------------------------------------------------------------------------- #
def test_content_steps_example_cleans_body_and_falls_back() -> None:
    rule = _complete("doc-content-steps")
    assert rule.chapter is not None
    assert rule.chapter.content.steps, "the example must demonstrate steps"
    assert len(rule.chapter.content.fallback) == 2

    primary = (
        '<html><body><h1 class="chapter-title">  Chapter   1 </h1>'
        '<div id="content">  <b>Alpha</b>\n[3/9]Beta  </div>'
        '<div class="read-content">decoy</div></body></html>'
    )
    chapter = _parser(rule).parse_chapter_content([primary], "1")
    assert chapter is not None
    assert chapter["title"] == "Chapter 1"
    # strip_html removes <b>, the js step drops the "[3/9]" page marker, and
    # normalize_space collapses the whitespace left behind.
    assert chapter["content"] == "Alpha Beta"

    css_fallback = (
        "<html><body><div class='read-content'> Fallback  Body </div></body></html>"
    )
    fallback = _parser(rule).parse_chapter_content([css_fallback], "2")
    assert fallback is not None
    assert fallback["content"] == "Fallback Body"

    xpath_fallback = (
        "<html><body><article id='content'> XPath  Body </article></body></html>"
    )
    xpath = _parser(rule).parse_chapter_content([xpath_fallback], "3")
    assert xpath is not None
    assert xpath["content"] == "XPath Body"


# --------------------------------------------------------------------------- #
# complete examples: JSON API
# --------------------------------------------------------------------------- #
def test_json_api_example_uses_json_body_and_dot_paths() -> None:
    rule = _complete("doc-json-api")
    assert rule.format == "json"
    assert rule.search is not None
    body = rule.search.json_body
    assert body is not None
    assert body["query"]["text"] == "{raw_keyword}"
    assert body["size"] == 20

    rendered = RuleSearcher._render_body(body, "a b")
    assert rendered["query"]["text"] == "a b"
    assert rendered["query"]["page"] == 1
    assert rendered["size"] == 20  # numbers are not templated

    searcher = _searcher(rule)
    searcher._request_url = "https://doc-api.example/v2/search"
    searcher._final_url = searcher._request_url
    payload = {
        "data": {
            "items": [
                {
                    "title": " T1 ",
                    "link": "/book/9/1",
                    "author": {"name": "A"},
                    "stats": {"words": 1234},
                },
                {"title": "T2", "link": "/book/9/2", "authorName": "B"},
            ]
        }
    }
    results = searcher._parse_html(json.dumps(payload))
    assert [(r["title"], r["author"], r["word_count"]) for r in results] == [
        ("T1", "A", "1234"),
        ("T2", "B", "-"),
    ]
    assert [r["book_id"] for r in results] == ["9", "9"]


def test_json_api_example_parses_book_and_chapter() -> None:
    rule = _complete("doc-json-api")
    book = {
        "data": {
            "title": "Book X",
            "intro": "Intro text",
            "tags": ["t1", "t2"],
            "items": [
                {"name": "C1", "url": "/book/5/1"},
                {"name": "C2", "url": "/book/5/2"},
            ],
        }
    }
    info = _parser(rule).parse_book_info([json.dumps(book)])
    assert info is not None
    assert info["book_name"] == "Book X"
    assert info["summary"] == "Intro text"
    assert info["tags"] == ["t1", "t2"]
    assert [(c["title"], c["chapterId"]) for c in info["volumes"][0]["chapters"]] == [
        ("C1", "1"),
        ("C2", "2"),
    ]

    chapter = _parser(rule).parse_chapter_content(
        [json.dumps({"data": {"title": "T", "body": "l1\nl2"}})], "1"
    )
    assert chapter is not None
    assert chapter["title"] == "T"
    assert chapter["content"] == "l1\nl2"


def test_json_api_example_stop_when_matches_an_empty_list() -> None:
    rule = _complete("doc-json-api")
    assert rule.book is not None and rule.book.pages is not None
    assert matches_stop_when(rule.book.pages, '{"data": {"items": []}}')
    assert not matches_stop_when(rule.book.pages, '{"data": {"items": [1]}}')


# --------------------------------------------------------------------------- #
# complete examples: headers
# --------------------------------------------------------------------------- #
def test_header_example_renders_variables_only_in_book_and_chapter() -> None:
    rule = _complete("doc-auth-headers")
    assert rule.headers["User-Agent"] == "novel-downloader-doc/1.0"
    assert rule.search is not None and rule.search.headers == {
        "Accept": "text/html,application/xhtml+xml"
    }
    assert rule.book is not None and rule.chapter is not None

    class _StubFetcher(RuleFetcher):
        site_name = "doc-auth-headers"

    # ``_render_headers`` is pure: skip the HTTP session setup entirely.
    fetcher = object.__new__(_StubFetcher)
    book_headers = fetcher._render_headers(
        rule.book.headers, rule.base_url, book_id="7"
    )
    assert book_headers["Referer"] == "https://doc-auth.example/search"
    # The cookie value is a placeholder, not a real credential.
    assert "<paste-your-session-cookie-here>" in book_headers["Cookie"]

    chapter_headers = fetcher._render_headers(
        rule.chapter.headers, rule.base_url, book_id="7", chapter_id="1"
    )
    assert chapter_headers["Referer"] == "https://doc-auth.example/book/7/"


def test_header_example_contains_no_credentials() -> None:
    text = json.dumps(_complete_rule("doc-auth-headers"))
    lowered = text.lower()
    for marker in ("password", "bearer ", "token=", "apikey", "api_key"):
        assert marker not in lowered, f"documented header example leaks {marker!r}"
