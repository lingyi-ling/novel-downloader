#!/usr/bin/env python3
"""Searcher tests: search result HTML -> ``SearchResult`` list."""

from __future__ import annotations

import re
from typing import Any

import pytest

from novel_downloader.plugins.rule.model import Rule, RuleError, parse_rule
from novel_downloader.plugins.rule.searcher import RuleSearcher

RULE_DICT: dict[str, Any] = {
    "site_key": "demo",
    "base_url": "https://demo.example",
    "search": {
        "url": "https://demo.example/search.php?q={keyword}",
        "item": "div.result",
        "priority": 45,
        "book_id_regex": r"/(\d+)/",
        "fields": {
            "title": {"selector": "dd h3 a", "attr": "text"},
            "url": {"selector": "dt a", "attr": "href"},
            "cover": {"selector": "dt a img", "attr": "src"},
            "author": {
                "selector": "dd p.author",
                "attr": "text",
                "regex": r"作者：(\S+)",
                "group": 1,
            },
            "latest": {"selector": "dd p.latest", "attr": "text"},
        },
    },
    "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
}

SEARCH_PAGE = """
<html><body>
  <div class="result">
    <dt><a href="/book/7/"><img src="//cdn.example/7.jpg"></a></dt>
    <dd>
      <h3><a href="/book/7/">斗破苍穹</a></h3>
      <p class="author">作者：天蚕土豆</p>
      <p class="latest">最新章节：第三章</p>
    </dd>
  </div>
  <div class="result">
    <dt><a href="/book/9/"></a></dt>
    <dd><h3><a href="/book/9/">武动乾坤</a></h3></dd>
  </div>
  <div class="result">
    <dt>没有链接</dt>
    <dd><h3>应被跳过</h3></dd>
  </div>
</body></html>
"""


def make_searcher(rule: Rule) -> RuleSearcher:
    """Build a concrete :class:`RuleSearcher` bound to ``rule``."""
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
    return cls(session=None)  # type: ignore[arg-type]


def test_parse_html_maps_every_field() -> None:
    results = make_searcher(parse_rule(RULE_DICT))._parse_html(SEARCH_PAGE)

    assert len(results) == 2
    first = results[0]
    assert first["site"] == "demo"
    assert first["title"] == "斗破苍穹"
    assert first["author"] == "天蚕土豆"
    assert first["book_id"] == "7"
    assert first["book_url"] == "https://demo.example/book/7/"
    assert first["cover_url"] == "https://cdn.example/7.jpg"
    assert first["latest_chapter"] == "最新章节：第三章"
    assert first["update_date"] == ""
    assert first["word_count"] == "-"


def test_parse_html_orders_results_by_priority() -> None:
    results = make_searcher(parse_rule(RULE_DICT))._parse_html(SEARCH_PAGE)

    assert [r["priority"] for r in results] == [45, 46]


def test_parse_html_respects_limit() -> None:
    searcher = make_searcher(parse_rule(RULE_DICT))

    assert len(searcher._parse_html(SEARCH_PAGE, limit=1)) == 1


def test_parse_html_skips_items_without_url() -> None:
    results = make_searcher(parse_rule(RULE_DICT))._parse_html(SEARCH_PAGE)

    assert all(r["book_url"] for r in results)
    assert "应被跳过" not in [r["title"] for r in results]


def test_parse_html_returns_empty_for_blank_html() -> None:
    searcher = make_searcher(parse_rule(RULE_DICT))

    assert searcher._parse_html("") == []


def test_parse_html_without_search_section() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
        }
    )
    assert make_searcher(rule)._parse_html(SEARCH_PAGE) == []


def test_book_id_falls_back_to_last_path_segment() -> None:
    rule_dict = {**RULE_DICT}
    rule_dict["search"] = {
        k: v for k, v in RULE_DICT["search"].items() if k != "book_id_regex"
    }
    searcher = make_searcher(parse_rule(rule_dict))

    results = searcher._parse_html(SEARCH_PAGE)

    assert [r["book_id"] for r in results] == ["7", "9"]


def test_book_id_negative_group_falls_back_to_whole_match() -> None:
    """``parse_rule`` rejects negative groups; the searcher stays defensive."""
    rx = re.compile(r"/(\d+)/")

    assert RuleSearcher._book_id("https://demo.example/book/7/", rx, -1) == "/7/"
    assert RuleSearcher._book_id("https://demo.example/book/7/", rx, 9) == "/7/"


def test_default_item_falls_back_to_root() -> None:
    rule_dict = {
        "site_key": "demo",
        "base_url": "https://demo.example",
        "search": {
            "url": "https://demo.example/s?q={keyword}",
            "fields": {
                "title": {"selector": "h3", "attr": "text"},
                "url": {"selector": "a", "attr": "href"},
            },
        },
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }
    searcher = make_searcher(parse_rule(rule_dict))

    html = '<a href="/book/1/">A</a><h3>只有一个结果</h3>'
    results = searcher._parse_html(html)

    assert len(results) == 1
    assert results[0]["title"] == "只有一个结果"
    assert results[0]["book_url"] == "https://demo.example/book/1/"


# --------------------------------------------------------------------------- #
# JSON / AJAX search
# --------------------------------------------------------------------------- #
JSON_RULE: dict[str, Any] = {
    "site_key": "demo-api",
    "base_url": "https://api.demo.example",
    "search": {
        "url": "https://api.demo.example/search",
        "method": "POST",
        "format": "json",
        "json_body": {"kw": "{raw_keyword}", "page": "{page}"},
        "item": "data.list",
        "book_id_regex": r"/book/(\d+)",
        "fields": {
            "title": {"selector": "name", "attr": "text"},
            "url": {"selector": "url", "attr": "text"},
            "cover": {"selector": "coverUrl", "attr": "text"},
            "author": {"selector": "author", "attr": "text"},
        },
    },
    "chapter": {"url": "{base_url}/read/{book_id}/{chapter_id}.html"},
}

JSON_PAYLOAD = """
{
  "data": {
    "total": 2,
    "list": [
      {
        "name": "斗破苍穹",
        "url": "https://api.demo.example/book/7",
        "author": "天蚕土豆",
        "coverUrl": "https://cdn.example/7.jpg",
        "tags": ["玄幻", "热血"]
      },
      {
        "name": "武动乾坤",
        "url": "https://api.demo.example/book/9"
      }
    ]
  }
}
"""


def test_parse_json_search_maps_every_field() -> None:
    results = make_searcher(parse_rule(JSON_RULE))._parse_html(JSON_PAYLOAD)

    assert len(results) == 2
    first = results[0]
    assert first["site"] == "demo-api"
    assert first["title"] == "斗破苍穹"
    assert first["author"] == "天蚕土豆"
    assert first["book_id"] == "7"
    assert first["book_url"] == "https://api.demo.example/book/7"
    assert first["cover_url"] == "https://cdn.example/7.jpg"


def test_parse_json_search_tolerates_missing_keys() -> None:
    results = make_searcher(parse_rule(JSON_RULE))._parse_html(JSON_PAYLOAD)

    second = results[1]
    assert second["title"] == "武动乾坤"
    assert second["author"] == ""
    assert second["cover_url"] == ""


def test_parse_json_search_rejects_malformed_body() -> None:
    searcher = make_searcher(parse_rule(JSON_RULE))

    with pytest.raises(RuleError, match="unparseable JSON"):
        searcher._parse_html("{ not json")


def test_render_body_expands_templates_recursively() -> None:
    payload = {
        "kw": "{raw_keyword}",
        "q": "{keyword}",
        "page": "{page}",
        "flags": {"nested": "{raw_keyword}"},
    }

    rendered = RuleSearcher._render_body(payload, "斗 破")

    assert rendered == {
        "kw": "斗 破",
        "q": "%E6%96%97+%E7%A0%B4",
        "page": "1",
        "flags": {"nested": "斗 破"},
    }


def test_render_body_leaves_non_strings_untouched() -> None:
    payload: dict[str, Any] = {"limit": 20, "enabled": True, "items": [1, 2]}

    assert RuleSearcher._render_body(payload, "x") == payload
