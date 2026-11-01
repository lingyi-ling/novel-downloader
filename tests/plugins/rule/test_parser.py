#!/usr/bin/env python3
"""Parser tests: raw pages -> ``BookInfoDict`` / ``ChapterDict``."""

from __future__ import annotations

import re
from typing import Any

from novel_downloader.plugins.rule.model import Rule, parse_rule
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.schemas import ParserConfig

RULE_DICT: dict[str, Any] = {
    "site_key": "demo",
    "base_url": "https://demo.example",
    "book": {
        "url": "{base_url}/book/{book_id}/",
        "fields": {
            "book_name": {
                "selector": "meta[property='og:novel:book_name']",
                "attr": "content",
            },
            "author": {
                "selector": "meta[property='og:novel:author']",
                "attr": "content",
            },
            "cover_url": {"selector": "img.cover", "attr": "src"},
            "summary": {
                "selector": "meta[property='og:description']",
                "attr": "content",
            },
            "tags": {
                "selector": "meta[property='og:novel:category']",
                "attr": "content",
                "multi": True,
            },
        },
        "chapters": {
            "item": "div.list a",
            "title": {"attr": "text"},
            "url": {"attr": "href"},
            "id_regex": r"/(\d+)\.html",
        },
    },
    "chapter": {
        "url": "{base_url}/book/{book_id}/{chapter_id}.html",
        "title": {"selector": "h1", "attr": "text"},
        "content": {"selector": "article", "attr": "inner_text"},
        "replace": [["\u00a0", ""]],
        "drop": [r"^第\(\d+/\d+\)页$"],
    },
}

BOOK_PAGE_1 = """
<html><head>
  <meta property="og:novel:book_name" content="斗破苍穹">
  <meta property="og:novel:author" content="天蚕土豆">
  <meta property="og:novel:category" content="玄幻">
  <meta property="og:novel:category" content="热血">
  <meta property="og:description" content="这里是简介。">
</head><body>
  <img class="cover" src="/img/1.jpg">
  <div class="list"><a href="/book/7/1.html">第一章 陨落的天才</a></div>
  <div class="list"><a href="/book/7/2.html">第二章 斗之气</a></div>
  <div class="pager"><a href="/book/7/index_2.html">下一页</a></div>
</body></html>
"""

BOOK_PAGE_2 = """
<html><body>
  <div class="list"><a href="/book/7/2.html">第二章 斗之气</a></div>
  <div class="list"><a href="/book/7/3.html">第三章 客人</a></div>
</body></html>
"""


def make_parser(rule: Rule) -> RuleParser:
    """Build a concrete :class:`RuleParser` bound to ``rule``."""
    cls = type(
        f"RuleParser_{rule.site_key}",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    return cls(ParserConfig())


# --------------------------------------------------------------------------- #
# book info
# --------------------------------------------------------------------------- #
def test_parse_book_info_reads_fields_and_chapters() -> None:
    parser = make_parser(parse_rule(RULE_DICT))

    info = parser.parse_book_info([BOOK_PAGE_1])

    assert info is not None
    assert info["book_name"] == "斗破苍穹"
    assert info["author"] == "天蚕土豆"
    assert info["cover_url"] == "https://demo.example/img/1.jpg"
    assert info["summary"] == "这里是简介。"
    assert info["tags"] == ["玄幻", "热血"]
    assert info["extra"] == {"site": "demo"}
    assert len(info["volumes"]) == 1

    chapters = info["volumes"][0]["chapters"]
    assert [c["title"] for c in chapters] == ["第一章 陨落的天才", "第二章 斗之气"]
    assert chapters[0]["chapterId"] == "1"
    assert chapters[0]["url"] == "https://demo.example/book/7/1.html"


def test_parse_book_info_merges_paginated_catalog_and_dedupes() -> None:
    parser = make_parser(parse_rule(RULE_DICT))

    info = parser.parse_book_info([BOOK_PAGE_1, BOOK_PAGE_2])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1", "2", "3"]


def test_parse_book_info_returns_none_without_pages_or_chapters() -> None:
    parser = make_parser(parse_rule(RULE_DICT))

    assert parser.parse_book_info([]) is None
    assert parser.parse_book_info(["<html><body>empty</body></html>"]) is None


def test_parse_book_info_without_book_section() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
        }
    )
    assert make_parser(rule).parse_book_info([BOOK_PAGE_1]) is None


# --------------------------------------------------------------------------- #
# chapter ids
# --------------------------------------------------------------------------- #
def test_chapter_id_negative_group_falls_back_to_whole_match() -> None:
    """``parse_rule`` rejects negative groups; the parser stays defensive."""
    rx = re.compile(r"/(\d+)\.html")

    assert RuleParser._chapter_id("/book/7/1.html", rx, -1) == "/1.html"
    assert RuleParser._chapter_id("/book/7/1.html", rx, 9) == "/1.html"


def test_chapter_id_falls_back_to_filename() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": "https://demo.example",
            "book": {
                "url": "{base_url}/{book_id}/",
                "chapters": {"item": "div.list a"},
            },
        }
    )
    parser = make_parser(rule)

    info = parser.parse_book_info(
        ['<div class="list"><a href="/book/7/chapter-9.html">第九章</a></div>']
    )
    assert info is not None
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "chapter-9"


# --------------------------------------------------------------------------- #
# chapter content
# --------------------------------------------------------------------------- #
def test_parse_chapter_content_joins_pages_and_cleans_text() -> None:
    parser = make_parser(parse_rule(RULE_DICT))
    page_1 = (
        "<h1>第 1 章 陨落的天才</h1>"
        "<article>第一段\u00a0文字<br>第(1/2)页<br>第二段文字</article>"
    )
    page_2 = "<article>第三段文字</article>"

    chapter = parser.parse_chapter_content([page_1, page_2], "1")

    assert chapter is not None
    assert chapter["id"] == "1"
    assert chapter["title"] == "第 1 章 陨落的天才"
    assert chapter["content"] == "第一段文字\n第二段文字\n第三段文字"
    assert chapter["extra"] == {"site": "demo"}


def test_parse_chapter_content_returns_none_for_empty_body() -> None:
    parser = make_parser(parse_rule(RULE_DICT))

    assert parser.parse_chapter_content([], "1") is None
    assert parser.parse_chapter_content(["<html><body></body></html>"], "1") is None


def test_parse_chapter_content_without_chapter_section() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "book": {"url": "{base_url}/{book_id}/", "chapters": {"item": "a"}},
        }
    )
    assert make_parser(rule).parse_chapter_content(["<p>x</p>"], "1") is None


def test_chapter_title_is_empty_when_not_configured() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "chapter": {
                "url": "{base_url}/{book_id}/{chapter_id}.html",
                "content": {"selector": "article", "attr": "inner_text"},
            },
        }
    )
    chapter = make_parser(rule).parse_chapter_content(["<article>正文</article>"], "1")
    assert chapter is not None
    assert chapter["title"] == ""
    assert chapter["content"] == "正文"
