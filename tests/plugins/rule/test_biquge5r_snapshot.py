#!/usr/bin/env python3
"""
Offline snapshot test for the built-in ``biquge5r`` rule source.

Minimal hand-written pages shaped exactly like the real site's markup (the
selectors come from the *shipped* ``builtin/biquge5r.json``, not a copy) are
pushed through the complete engine pipeline:

    search  ->  book info  ->  catalog (with pagination)  ->  chapter content
                                                                   (with pagination)

The assertions pin the observable behaviour of every stage, so a change to
either the shipped rule JSON or the extraction engine that alters the
contract fails here.  Fully offline: the HTTP layer of the fetcher is stubbed
with a URL -> HTML map.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.rule import load_rules, resolve_url
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.model import Rule
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.schemas import ParserConfig

BASE = "https://www.biquge5.com"
BOOK_ID = "0_143"
CHAPTER_ID = "155832"

BOOK_URL = f"{BASE}/{BOOK_ID}/"
INDEX_2_URL = f"{BASE}/{BOOK_ID}/index_2.html"
CHAPTER_URL = f"{BASE}/{BOOK_ID}/{CHAPTER_ID}.html"
CHAPTER_2_URL = f"{BASE}/{BOOK_ID}/{CHAPTER_ID}_2.html"


# --------------------------------------------------------------------------- #
# minimal fake pages, shaped after the real biquge5.com markup
# --------------------------------------------------------------------------- #
SEARCH_PAGE = f"""
<html><body>
<div class="row">
  <div class="col-12 col-md-6">
    <dl>
      <dt><a href="/{BOOK_ID}/"><img src="/files/article/image/0/143/143.jpg"></a></dt>
      <dd><h3><a href="/{BOOK_ID}/">斗破苍穹</a></h3></dd>
      <dd class="book_other">
        <span>天蚕土豆</span>
        <a href="/{BOOK_ID}/155999.html">最新章节：大结局</a>
        更新时间：2026-10-30
      </dd>
    </dl>
  </div>
  <div class="col-12 col-md-6">
    <dl>
      <dt><a href="/0_144/"><img src="/files/article/image/0/144/144.jpg"></a></dt>
      <dd><h3><a href="/0_144/">武动乾坤</a></h3></dd>
      <dd class="book_other">
        <span>天蚕土豆</span>
        <a href="/0_144/288001.html">最新章节：完本</a>
        更新时间：2026-09-01
      </dd>
    </dl>
  </div>
</div>
</body></html>
"""

BOOK_PAGE = f"""
<html><head>
  <meta property="og:novel:book_name" content="斗破苍穹">
  <meta property="og:novel:author" content="天蚕土豆">
  <meta property="og:image" content="/files/article/image/0/143/143.jpg">
  <meta property="og:novel:update_time" content="2026-10-31">
  <meta property="og:novel:status" content="连载中">
  <meta property="og:novel:category" content="玄幻">
  <meta property="og:novel:category" content="热血">
  <meta property="og:description" content="这是一个少年的故事。">
</head><body>
  <div class="book_list2">
    <a href="/{BOOK_ID}/{CHAPTER_ID}.html">第一章 陨落的天才</a>
    <a href="/{BOOK_ID}/155833.html">第二章 斗之气</a>
  </div>
  <div class="page"><a href="index_2.html">下一页</a></div>
</body></html>
"""

# Second catalog page: repeats the last chapter of page 1 (the parser must
# dedupe) and adds one new chapter; no further "next page" reference.
INDEX_2_PAGE = f"""
<html><body>
  <div class="book_list2">
    <a href="/{BOOK_ID}/155833.html">第二章 斗之气</a>
    <a href="/{BOOK_ID}/155834.html">第三章 客人</a>
  </div>
</body></html>
"""

CHAPTER_PAGE = f"""
<html><body>
  <h1>第一章 陨落的天才 - 《斗破苍穹》</h1>
  <article>　　正文第一段\u00a0文字。<br>第(1/2)页<br>　　正文第二段。</article>
  <div class="page"><a href="{CHAPTER_ID}_2.html">下一页</a></div>
</body></html>
"""

CHAPTER_PAGE_2 = """
<html><body>
  <article>　　正文第三段。</article>
</body></html>
"""

# 155833 is a single-page chapter: no "next page" reference in its payload.
CHAPTER_2_ID = "155833"
CHAPTER_SINGLE_URL = f"{BASE}/{BOOK_ID}/{CHAPTER_2_ID}.html"
CHAPTER_SINGLE_PAGE = """
<html><body>
  <h1>第二章 斗之气 - 《斗破苍穹》</h1>
  <article>　　只有一页的正文。</article>
</body></html>
"""

PAGES: dict[str, str] = {
    BOOK_URL: BOOK_PAGE,
    INDEX_2_URL: INDEX_2_PAGE,
    CHAPTER_URL: CHAPTER_PAGE,
    CHAPTER_2_URL: CHAPTER_PAGE_2,
    CHAPTER_SINGLE_URL: CHAPTER_SINGLE_PAGE,
}


def _rule() -> Rule:
    rules = load_rules()
    rule = rules.get("biquge5r")
    assert rule is not None, "built-in biquge5r rule is missing"
    return rule


class _SnapshotFetcher(RuleFetcher):
    """RuleFetcher with the HTTP layer replaced by the page map above."""

    site_name = "biquge5r"

    def __init__(self, rule: Rule) -> None:
        self._rule = rule  # type: ignore[misc]
        self._base_url = ""
        self.fetched: list[str] = []

    async def fetch(self, url: str, encoding: str = "utf-8", **kwargs: Any) -> str:
        self.fetched.append(url)
        return PAGES[url]


def _make_searcher(rule: Rule) -> RuleSearcher:
    cls: type[RuleSearcher] = type(
        "RuleSearcher_snapshot",
        (RuleSearcher,),
        {"_rule": rule, "site_name": "biquge5r", "BASE_URL": rule.base_url},
    )
    return cls(session=None)  # type: ignore[arg-type]


def _make_parser(rule: Rule) -> RuleParser:
    cls: type[RuleParser] = type(
        "RuleParser_snapshot",
        (RuleParser,),
        {"_rule": rule, "site_name": "biquge5r"},
    )
    return cls(ParserConfig())


# --------------------------------------------------------------------------- #
# stage 0: URL -> source resolution
# --------------------------------------------------------------------------- #
def test_resolve_url_maps_book_and_chapter_links() -> None:
    assert resolve_url("www.biquge5.com", f"/{BOOK_ID}/") == {
        "site_key": "biquge5r",
        "book_id": BOOK_ID,
        "chapter_id": None,
    }
    assert resolve_url("www.biquge5.com", f"/{BOOK_ID}/{CHAPTER_ID}.html") == {
        "site_key": "biquge5r",
        "book_id": BOOK_ID,
        "chapter_id": CHAPTER_ID,
    }


# --------------------------------------------------------------------------- #
# stage 1: search
# --------------------------------------------------------------------------- #
def test_search_stage_maps_every_field() -> None:
    results = _make_searcher(_rule())._parse_html(SEARCH_PAGE)

    assert len(results) == 2
    first, second = results

    assert first["site"] == "biquge5r"
    assert first["title"] == "斗破苍穹"
    assert first["author"] == "天蚕土豆"
    assert first["book_id"] == BOOK_ID
    assert first["book_url"] == BOOK_URL
    assert first["cover_url"] == f"{BASE}/files/article/image/0/143/143.jpg"
    assert first["latest_chapter"] == "最新章节：大结局"
    assert first["update_date"] == "2026-10-30"
    assert first["priority"] == 45

    assert second["title"] == "武动乾坤"
    assert second["book_id"] == "0_144"
    assert second["update_date"] == "2026-09-01"
    assert second["latest_chapter"] == "最新章节：完本"
    assert second["priority"] == 46


def test_search_stage_result_book_id_drives_the_detail_url() -> None:
    """The ``book_id`` extracted by search is what the detail URL is built on."""
    rule = _rule()
    results = _make_searcher(rule)._parse_html(SEARCH_PAGE)

    assert rule.book is not None
    template = rule.book.url
    assert template == "{base_url}/{book_id}/"
    assert results[0]["book_id"] == BOOK_ID


# --------------------------------------------------------------------------- #
# stage 2 + 3: book info and paginated catalog
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_book_stage_walks_catalog_pagination_and_parses_info() -> None:
    rule = _rule()
    fetcher = _SnapshotFetcher(rule)

    raw_pages = await fetcher.fetch_book_info(BOOK_ID)

    # entry page + index_2, then the walker stops (no index_3 reference)
    assert fetcher.fetched == [BOOK_URL, INDEX_2_URL]
    assert len(raw_pages) == 2

    info = _make_parser(rule).parse_book_info(raw_pages)
    assert info is not None

    assert info["book_name"] == "斗破苍穹"
    assert info["author"] == "天蚕土豆"
    assert info["cover_url"] == f"{BASE}/files/article/image/0/143/143.jpg"
    assert info["update_time"] == "2026-10-31"
    assert info["serial_status"] == "连载中"
    assert info["summary"] == "这是一个少年的故事。"
    assert info["tags"] == ["玄幻", "热血"]
    assert info["extra"] == {"site": "biquge5r"}

    volumes = info["volumes"]
    assert len(volumes) == 1
    assert volumes[0]["volume_name"] == "正文"

    chapters = volumes[0]["chapters"]
    # page 2 repeats 第二章 -- the catalog must dedupe across pages
    assert [c["title"] for c in chapters] == [
        "第一章 陨落的天才",
        "第二章 斗之气",
        "第三章 客人",
    ]
    assert [c["chapterId"] for c in chapters] == ["155832", "155833", "155834"]
    assert chapters[0]["url"] == CHAPTER_URL


# --------------------------------------------------------------------------- #
# stage 4 + 5: chapter content and content pagination
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_chapter_stage_walks_content_pagination_and_cleans_text() -> None:
    rule = _rule()
    fetcher = _SnapshotFetcher(rule)

    raw_pages = await fetcher.fetch_chapter_content(BOOK_ID, CHAPTER_ID)

    assert fetcher.fetched == [CHAPTER_URL, CHAPTER_2_URL]
    assert len(raw_pages) == 2

    chapter = _make_parser(rule).parse_chapter_content(raw_pages, CHAPTER_ID)
    assert chapter is not None

    assert chapter["id"] == CHAPTER_ID
    # the h1 suffix "- 《斗破苍穹》" is stripped by the rule's regex
    assert chapter["title"] == "第一章 陨落的天才"
    # pages are joined; \u00a0 removed; the "第(1/2)页" marker line dropped
    assert chapter["content"] == "正文第一段文字。\n正文第二段。\n正文第三段。"
    assert chapter["extra"] == {"site": "biquge5r"}


# --------------------------------------------------------------------------- #
# stage 5b: a single-page chapter (no "next page" reference) stops early
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_chapter_stage_stops_without_a_next_page_link() -> None:
    rule = _rule()
    fetcher = _SnapshotFetcher(rule)

    raw_pages = await fetcher.fetch_chapter_content(BOOK_ID, CHAPTER_2_ID)

    assert fetcher.fetched == [CHAPTER_SINGLE_URL]
    assert raw_pages == [CHAPTER_SINGLE_PAGE]

    chapter = _make_parser(rule).parse_chapter_content(raw_pages, CHAPTER_2_ID)
    assert chapter is not None
    assert chapter["title"] == "第二章 斗之气"
    assert chapter["content"] == "只有一页的正文。"
