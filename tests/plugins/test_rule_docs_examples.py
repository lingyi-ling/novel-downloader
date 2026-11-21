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

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from novel_downloader.plugins.rule.engine import (
    PY_STEP_FUNCTIONS,
    apply_steps,
    extract,
    parse_page,
)
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
        # A complete rule (the private-source walkthrough) needs no wrapping.
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


def _block_with_key(key: str) -> dict[str, Any]:
    matches = [block for block in ZH_BLOCKS if key in _load_fragment(block)]
    assert len(matches) == 1, f"expected exactly one documented {key!r} example"
    return _load_fragment(matches[0])


def _blocks_with_key(key: str) -> list[dict[str, Any]]:
    return [
        _load_fragment(block) for block in ZH_BLOCKS if key in _load_fragment(block)
    ]


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
