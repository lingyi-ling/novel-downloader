#!/usr/bin/env python3
"""
Documentation/code consistency tests for the rule DSL.

Three things are locked down here:

* ``docs/guide/rule-dsl.md`` must not document a field the model rejects, and
  every field the model accepts must appear in the doc.  The comparison is
  driven by the tables between the ``<!-- rule-dsl:keys:* -->`` markers, so a
  stale doc fails the test with the exact differences instead of silently
  drifting.
* the ``rules/_example*.json`` files must parse with the real ``parse_rule``
  and must really use the newer capabilities.  There is deliberately no
  "optional key" escape hatch: a skip here would let a broken example pass.
* the examples must actually *run*.  Each one is driven through
  ``RuleParser`` / ``RuleSearcher`` / ``RuleFetcher`` with mock responses so
  that ``steps``, ``fallback``, ``preprocess`` and the pagination guards are
  proven to change the output, not merely to be accepted by the parser.

All checks are offline and read-only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from novel_downloader.plugins.rule import (
    _is_ignored,
    iter_rules,
    load_rules,
)
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.model import (
    _BOOK_FIELD_NAMES,
    _BOOK_KEYS,
    _CHAPTER_KEYS,
    _CHAPTERS_KEYS,
    _FIELD_KEYS,
    _LIMIT_KEYS,
    _PAGES_KEYS,
    _RESOLVE_KEYS,
    _SEARCH_FIELD_NAMES,
    _SEARCH_KEYS,
    _STEP_KEYS,
    _TOP_LEVEL_KEYS,
    Rule,
    loads_rule_json,
    parse_rule,
)
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.schemas import ParserConfig

ROOT = Path(__file__).resolve().parents[2]
RULE_DOC = ROOT / "docs" / "guide" / "rule-dsl.md"
RULES_DIR = ROOT / "rules"
BUILTIN_DIR = ROOT / "src" / "novel_downloader" / "plugins" / "rule" / "builtin"

ADVANCED = "_example-advanced-html.json"
JSON_API = "_example-json-api.json"

#: Schema group name used by the doc markers -> the real set in ``model.py``.
REAL_KEYS: dict[str, frozenset[str]] = {
    "top_level": frozenset(_TOP_LEVEL_KEYS),
    "search": frozenset(_SEARCH_KEYS),
    "search_fields": frozenset(_SEARCH_FIELD_NAMES),
    "book": frozenset(_BOOK_KEYS),
    "book_fields": frozenset(_BOOK_FIELD_NAMES),
    "chapters": frozenset(_CHAPTERS_KEYS),
    "pages": frozenset(_PAGES_KEYS),
    "chapter": frozenset(_CHAPTER_KEYS),
    "field": frozenset(_FIELD_KEYS),
    "step": frozenset(_STEP_KEYS),
    "limits": frozenset(_LIMIT_KEYS),
    "resolve": frozenset(_RESOLVE_KEYS),
}

#: Capabilities the examples are supposed to demonstrate.
NEW_FEATURES = frozenset(
    {
        "steps",
        "fallback",
        "preprocess",
        "container",
        "limits",
        "stop_when",
        "loop_guard",
        "single_result_fallback",
    }
)

_KEYS_BLOCK_RE = re.compile(
    r"<!--\s*rule-dsl:keys:(\w+)\s*-->(.*?)<!--\s*/rule-dsl:keys:\1\s*-->", re.S
)
_KEYS_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.M)


# --------------------------------------------------------------------------- #
# doc parsing
# --------------------------------------------------------------------------- #
def _read_doc() -> str:
    assert RULE_DOC.is_file(), f"missing rule DSL doc: {RULE_DOC}"
    return RULE_DOC.read_text(encoding="utf-8")


def _key_blocks(text: str) -> dict[str, set[str]]:
    blocks: dict[str, set[str]] = {}
    for name, body in _KEYS_BLOCK_RE.findall(text):
        blocks[str(name)] = {str(match) for match in _KEYS_ROW_RE.findall(str(body))}
    return blocks


# --------------------------------------------------------------------------- #
# example rules
# --------------------------------------------------------------------------- #
def _example_files() -> list[Path]:
    return sorted(RULES_DIR.glob("_example*.json"))


def _param_id(path: Path) -> str:
    return path.name


def _rule_document(path: Path) -> dict[str, Any]:
    return loads_rule_json(path.read_text(encoding="utf-8"), source=path.name)


def _load_example(name: str) -> Rule:
    return parse_rule(_rule_document(RULES_DIR / name), source=name)


def _collect_keys(value: Any) -> set[str]:
    """Collect real JSON keys, ignoring ``_``-prefixed comment keys."""
    if isinstance(value, dict):
        keys = {str(key) for key in value if not str(key).startswith("_")}
        for item in value.values():
            keys |= _collect_keys(item)
        return keys
    if isinstance(value, list):
        collected: set[str] = set()
        for item in value:
            collected |= _collect_keys(item)
        return collected
    return set()


# --------------------------------------------------------------------------- #
# doc <-> model consistency
# --------------------------------------------------------------------------- #
def test_doc_has_a_field_table_for_every_schema_group() -> None:
    blocks = _key_blocks(_read_doc())
    missing = sorted(set(REAL_KEYS) - set(blocks))
    assert not missing, (
        "docs/guide/rule-dsl.md is missing a <!-- rule-dsl:keys:* --> table for: "
        + ", ".join(missing)
    )


def test_documented_fields_exist_in_model() -> None:
    blocks = _key_blocks(_read_doc())
    problems: list[str] = []
    for block, names in sorted(blocks.items()):
        real = REAL_KEYS.get(block)
        if real is None:
            problems.append(f"{block}: unknown schema group (not in model.py)")
            continue
        extra = sorted(names - real)
        if extra:
            problems.append(f"{block}: documented but absent from model.py: {extra}")
    assert not problems, "documented fields drift from model.py:\n" + "\n".join(
        problems
    )


def test_model_keys_are_documented() -> None:
    current = _key_blocks(_read_doc())
    problems: list[str] = []
    for block, real in sorted(REAL_KEYS.items()):
        missing = sorted(real - current.get(block, set()))
        if missing:
            problems.append(f"{block}: in model.py but not documented: {missing}")
    assert not problems, (
        "model.py has fields the doc does not mention (add them to the matching "
        "field table):\n" + "\n".join(problems)
    )


# --------------------------------------------------------------------------- #
# example rules: schema
# --------------------------------------------------------------------------- #
def test_example_rule_files_exist() -> None:
    files = _example_files()
    assert files, f"no rules/_example*.json files found in {RULES_DIR}"
    names = {path.name for path in files}
    assert {ADVANCED, JSON_API} <= names


def test_example_rules_are_not_auto_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for path in _example_files():
        assert path.parent == RULES_DIR
        assert path.name.startswith("_"), f"{path.name} would be auto-loaded"
        # The loader skips every file whose name starts with "_" (templates
        # and drafts), which is exactly why examples live here.
        assert _is_ignored(path.name)

    assert not list(BUILTIN_DIR.glob("_example*.json")), (
        "example rules must not be placed in the builtin package: every *.json "
        "there is registered as a real source"
    )

    # And prove it against the real loader: loading from this checkout must not
    # register a single example site.
    example_keys = {_load_example(path.name).site_key for path in _example_files()}
    monkeypatch.chdir(ROOT)
    load_rules(force=True)
    try:
        rows = iter_rules()
        loaded_keys = {key for key, _, _ in rows}
        loaded_files = {str(meta.get("file", "")) for _, _, meta in rows}
    finally:
        # Leave the global cache in a deterministic state for later tests.
        load_rules(force=True)

    assert not (example_keys & loaded_keys), (
        "example site keys were registered as real sources: "
        + ", ".join(sorted(example_keys & loaded_keys))
    )
    assert not any("_example" in name for name in loaded_files), (
        "loader returned an example file: " + ", ".join(sorted(loaded_files))
    )


@pytest.mark.parametrize("path", _example_files(), ids=_param_id)
def test_example_rule_parses_with_every_feature(path: Path) -> None:
    """No skip hatch: every key in the shipped example must be supported."""
    rule = parse_rule(_rule_document(path), source=path.name)
    assert rule.site_key


def test_example_rules_cover_the_new_capabilities() -> None:
    keys: set[str] = set()
    for path in _example_files():
        keys |= _collect_keys(_rule_document(path))
    missing = sorted(NEW_FEATURES - keys)
    assert not missing, (
        "example rules do not use these capabilities outside of comments: "
        + ", ".join(missing)
    )


# --------------------------------------------------------------------------- #
# example rules: end-to-end behaviour
# --------------------------------------------------------------------------- #
def _parser(rule: Rule) -> RuleParser:
    cls = type(
        f"RuleParser_{rule.site_key}",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    return cast(RuleParser, cls(ParserConfig()))


def _searcher(rule: Rule) -> RuleSearcher:
    cls = type(
        f"RuleSearcher_{rule.site_key}",
        (RuleSearcher,),
        {
            "_rule": rule,
            "site_name": rule.site_key,
            "BASE_URL": rule.base_url,
            "priority": rule.search.priority if rule.search else rule.priority,
        },
    )
    searcher = cast(RuleSearcher, cls(session=None))
    searcher._request_url = ""
    searcher._final_url = ""
    return searcher


#: Fallback page for the HTML example: no ``meta`` tags (so ``.book-name`` /
#: ``.book-author`` must win) and a double-escaped catalog (so only a working
#: ``preprocess``+``container`` produces chapter links).
ADVANCED_BOOK_PAGE = """<html><body>
<h1 class="book-name">Fallback Name</h1>
<span class="book-author">Fallback Author</span>
<div class="summary">&nbsp; line1&nbsp;
 line1&nbsp;
 line2</div>
<a class="tag" href="#">tag1</a><a class="tag" href="#">tag2</a>
<div id="catalog"><div class="payload">
  &lt;dd&gt;&lt;a href="/book/7/101.html"&gt;  Chapter One  &lt;/a&gt;&lt;/dd&gt;
  &lt;dd&gt;&lt;a href="/book/7/102.html"&gt;Chapter Two&lt;/a&gt;&lt;/dd&gt;
</div></div>
</body></html>"""

ADVANCED_CHAPTER_PAGE = """<html><body><h1>Chapter Title</h1>
<div id="content"><div class="payload">
&lt;p&gt;Alpha&lt;/p&gt;&lt;p&gt;Alpha&lt;/p&gt;&lt;p&gt;Beta&lt;/p&gt;&lt;p&gt;第(1/3)页&lt;/p&gt;
</div></div></body></html>"""

ADVANCED_SEARCH_PAGE = """<ul class="result-list">
<li><h3><a href="/book/11/"> <b>Great</b>  Book&nbsp;Name </a></h3>
    <span class="author"> Au  Thor </span><span class="latest">L1</span></li>
<li><h3><a href="/book/12/">Second</a></h3><span class="book-author">A2</span></li>
</ul>"""


def test_advanced_example_parses_book_info() -> None:
    rule = _load_example(ADVANCED)
    info = _parser(rule).parse_book_info([ADVANCED_BOOK_PAGE])

    assert info is not None
    # fallback: the string form would inherit attr="content" and read nothing
    # from <h1>; the example must use object fallbacks with attr="text".
    assert info["book_name"] == "Fallback Name"
    assert info["author"] == "Fallback Author"
    # replace + steps: &nbsp; normalised, duplicate line dropped.
    assert info["summary"] == "line1 line2"
    # multi: every tag collected.
    assert info["tags"] == ["tag1", "tag2"]
    # preprocess + container: the escaped catalog only becomes real <dd><a>
    # nodes after html_unescape, and `item` must be relative to the rewritten
    # document (the container itself is gone).
    chapters = info["volumes"][0]["chapters"]
    assert [c["title"] for c in chapters] == ["Chapter One", "Chapter Two"]
    assert chapters[0]["url"] == "https://novel.example-advanced.test/book/7/101.html"
    assert [c["chapterId"] for c in chapters] == ["101", "102"]


def test_advanced_example_parses_chapter_content() -> None:
    rule = _load_example(ADVANCED)
    chapter = _parser(rule).parse_chapter_content([ADVANCED_CHAPTER_PAGE], "101")

    assert chapter is not None
    # title and content both come from the preprocessed (whole-page) document.
    assert chapter["title"] == "Chapter Title"
    # dedupe_lines step + drop (the "第(1/3)页" line) both change the output.
    assert chapter["content"] == "Alpha\nBeta"


def test_advanced_example_search_applies_steps_and_fallback() -> None:
    rule = _load_example(ADVANCED)
    results = _searcher(rule)._parse_html(ADVANCED_SEARCH_PAGE)

    # js step: tags stripped and whitespace collapsed (raw text has a <b>,
    # double spaces and a non-breaking space).
    assert results[0]["title"] == "GreatBook Name"
    assert results[0]["author"] == "Au Thor"
    assert results[1]["author"] == "A2"  # string fallback inherits attr="text"
    assert [result["priority"] for result in results] == [900, 901]
    assert results[0]["book_id"] == "11"


class _StubFetcher(RuleFetcher):
    """A :class:`RuleFetcher` whose HTTP layer is a URL -> HTML map."""

    site_name = "example-advanced"

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    async def _get(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> str:
        self.calls.append(url)
        return self.pages.get(url, "<html>page</html>")


@pytest.mark.asyncio
async def test_advanced_example_stop_when_prevents_extra_requests() -> None:
    rule = _load_example(ADVANCED)
    assert rule.book is not None
    pages = rule.book.pages
    assert pages is not None and pages.max_pages is not None

    fetcher = _StubFetcher()
    extra = await fetcher._collect_pages(
        pages,
        "<html>已显示全部章节</html>",
        rule.base_url,
        None,
        first_url="https://novel.example-advanced.test/book/7/",
        book_id="7",
    )

    assert extra == []
    assert fetcher.calls == []


def test_json_example_parses_book_and_chapter() -> None:
    rule = _load_example(JSON_API)
    book = {
        "data": {
            "name": "  Book X  ",
            "author": "Auth",
            "cover": "/c.jpg",
            "updatedAt": "2020-01-01",
            "intro": "<b>hi</b> there",
            "tags": ["a", "b"],
            "list": [
                {"title": " C1 ", "url": "/book/5/1"},
                {"title": "C2", "url": "/book/5/2"},
            ],
        }
    }

    info = _parser(rule).parse_book_info([json.dumps(book)])

    assert info is not None
    assert info["book_name"] == "Book X"
    assert info["summary"] == "hi there"  # strip_html js step
    assert info["tags"] == ["a", "b"]
    chapters = info["volumes"][0]["chapters"]
    assert [(c["title"], c["chapterId"]) for c in chapters] == [
        ("C1", "1"),
        ("C2", "2"),
    ]

    chapter = _parser(rule).parse_chapter_content(
        [json.dumps({"data": {"title": "T1", "content": "line1\nline2  "}})],
        "1",
    )
    assert chapter is not None
    assert chapter["title"] == "T1"
    assert chapter["content"] == "line1\nline2"


def test_json_example_search_and_single_result_fallback() -> None:
    rule = _load_example(JSON_API)
    searcher = _searcher(rule)
    searcher._request_url = "https://api.example-json.test/v1/search"
    searcher._final_url = searcher._request_url

    payload = {
        "data": {
            "list": [
                {
                    "name": "  N1 ",
                    "url": "/book/9",
                    "author": {"name": "A"},
                    "cover": "/x.jpg",
                    "wordCount": 123,
                }
            ]
        }
    }
    results = searcher._parse_html(json.dumps(payload))

    assert results[0]["title"] == "N1"
    assert results[0]["author"] == "A"
    assert results[0]["word_count"] == "123"
    assert results[0]["book_id"] == "9"

    # No items and no evidence -> no synthetic result.
    assert searcher._parse_html(json.dumps({"data": {"list": []}})) == []

    # Zero items + a redirect to a detail page -> one synthetic result.
    redirected = _searcher(rule)
    redirected._request_url = "https://api.example-json.test/v1/search"
    redirected._final_url = "https://api.example-json.test/v1/book/42"
    fallback = redirected._parse_html(json.dumps({"data": {}}))

    assert len(fallback) == 1
    assert fallback[0]["book_url"] == "https://api.example-json.test/v1/book/42"
