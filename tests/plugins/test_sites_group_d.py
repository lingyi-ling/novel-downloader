#!/usr/bin/env python3
"""Offline fixtures for the D-group site plugins (2026-11 audit).

Every page here is a trimmed copy of the markup the live sites served during
the network probe; nothing in this file touches the network.  Each fixture
pins one defect that showed up when a search hit was followed through to a
download:

* ``akatsuki_novels`` answered the old POST with ``405`` and redesigned both
  the result cards (``/novels/view/<id>``) and the book/chapter pages;
* ``alicesw`` moved the title/state/tags out of the old selectors and keeps
  the cover in ``data-src``;
* ``alphapolis``/``dxmwx`` were unreachable (Cloudflare challenge / timeout),
  so their parsers are pinned against the degraded shapes that used to crash
  or produce URL-shaped ids;
* ``bixiange``/``ciluke``/``czbooks``/``dxmwx`` derived ids from the raw
  href, so absolute links and query strings produced unusable ids;
* ``ciyuanji`` built chapter URLs from a ``bookId`` that chapter records do
  not contain, yielding ``/chapter/None_<id>.html``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from novel_downloader.plugins.sites.akatsuki_novels.parser import AkatsukiNovelsParser
from novel_downloader.plugins.sites.akatsuki_novels.searcher import (
    AkatsukiNovelsSearcher,
)
from novel_downloader.plugins.sites.alicesw.parser import AliceswParser
from novel_downloader.plugins.sites.alicesw.searcher import AliceswSearcher
from novel_downloader.plugins.sites.alphapolis.parser import AlphapolisParser
from novel_downloader.plugins.sites.alphapolis.searcher import AlphapolisSearcher
from novel_downloader.plugins.sites.bixiange.parser import BixiangeParser
from novel_downloader.plugins.sites.bixiange.searcher import BixiangeSearcher
from novel_downloader.plugins.sites.ciluke.searcher import CilukeSearcher
from novel_downloader.plugins.sites.ciyuanji.parser import CiyuanjiParser
from novel_downloader.plugins.sites.ciyuanji.searcher import CiyuanjiSearcher
from novel_downloader.plugins.sites.czbooks.parser import CzbooksParser
from novel_downloader.plugins.sites.czbooks.searcher import CzbooksSearcher
from novel_downloader.plugins.sites.dushu.parser import DushuParser
from novel_downloader.plugins.sites.dxmwx.parser import DxmwxParser
from novel_downloader.plugins.sites.dxmwx.searcher import DxmwxSearcher
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


def _searcher(cls: type, session: Any = None) -> Any:
    return cls(session if session is not None else object())


# --------------------------------------------------------------------------- #
# akatsuki_novels: 2026 layout (GET /novels/index, /novels/view/<id>)
# --------------------------------------------------------------------------- #
_AKATSUKI_SEARCH = """
<html><body>
<ul class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
  <li class="rounded-lg border border-border p-4">
    <div class="flex gap-3"><div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-2">
        <a href="/novels/view/58" class="min-w-0 text-base font-semibold">
          作品A
        </a>
      </div>
      <p class="mt-1 text-sm text-subtle">
        <a href="/users/view/51" class="hover:underline">作者A</a>
      </p>
      <p class="mt-1 line-clamp-2 text-sm text-muted">简介A</p>
      <p class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums">
        <span>全164話</span>
        <span>3,455,558文字</span>
        <span>最終更新 2026年11月11日</span>
      </p>
    </div></div>
  </li>
  <li class="rounded-lg border border-border p-4">
    <div class="flex gap-3"><div class="min-w-0 flex-1">
      <p class="mt-1 text-sm text-subtle">没有作品链接的卡片</p>
    </div></div>
  </li>
  <li class="rounded-lg border border-border p-4">
    <div class="flex gap-3"><div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-2">
        <a href="https://www.akatsuki-novels.com/novels/view/99?from=list#top">
          作品B
        </a>
      </div>
      <p class="mt-1 text-sm text-subtle"><a href="/users/view/2">作者B</a></p>
      <p class="mt-2 tabular-nums"><span>全3話</span><span>1,200文字</span>
        <span>最終更新 2026年1月2日</span></p>
    </div></div>
  </li>
</ul>
</body></html>
"""

_AKATSUKI_SEARCH_LEGACY = """
<html><body>
<div class="topicsBox">
  <strong class="novelTitle">
    <a href="/stories/index/novel_id~18228">旧作品</a>
    （全 64話）
    <a href="/users/view/7">旧作者</a>
  </strong>
  <span>最終掲載日時：2026年1月3日</span>
  <span>文字数： 315,731 / 1話当たりの文字数： 4,933</span>
</div>
</body></html>
"""


def test_akatsuki_search_parses_new_cards_and_skips_broken_rows() -> None:
    rows = _searcher(AkatsukiNovelsSearcher)._parse_html(_AKATSUKI_SEARCH)

    assert [r["book_id"] for r in rows] == ["58", "99"]
    assert rows[0]["book_url"] == "https://www.akatsuki-novels.com/novels/view/58"
    assert rows[0]["title"] == "作品A"
    assert rows[0]["author"] == "作者A"
    assert rows[0]["latest_chapter"] == "全164話"
    assert rows[0]["word_count"] == "3,455,558"
    assert rows[0]["update_date"] == "2026年11月11日"
    # The absolute href is canonicalised and its query/fragment are ignored
    # for the id.
    assert rows[1]["book_id"] == "99"
    assert rows[1]["title"] == "作品B"
    assert rows[1]["priority"] == AkatsukiNovelsSearcher.priority + 1


def test_akatsuki_search_limit_counts_parsed_rows() -> None:
    rows = _searcher(AkatsukiNovelsSearcher)._parse_html(_AKATSUKI_SEARCH, limit=1)

    assert [r["book_id"] for r in rows] == ["58"]


def test_akatsuki_search_legacy_topics_box_still_parses() -> None:
    rows = _searcher(AkatsukiNovelsSearcher)._parse_html(_AKATSUKI_SEARCH_LEGACY)

    assert [r["book_id"] for r in rows] == ["18228"]
    assert rows[0]["book_url"] == (
        "https://www.akatsuki-novels.com/stories/index/novel_id~18228"
    )
    assert rows[0]["title"] == "旧作品"
    assert rows[0]["author"] == "旧作者"
    assert rows[0]["word_count"] == "315,731"


_AKATSUKI_BOOK = """
<html><body><article>
  <header>
    <h1 class="min-w-0 text-xl font-bold">作品A</h1>
    <p class="mt-1 text-sm text-muted">
      <a href="/users/view/51" class="hover:underline">作者A</a>
    </p>
    <p class="mt-3 whitespace-pre-line text-sm">作品简介。</p>
  </header>
  <h2 class="mt-8 text-lg font-bold">目次</h2>
  <ol class="mt-3 divide-y divide-border">
    <li><a href="/stories/view/851/novel_id~58" class="min-w-0">第一話</a></li>
    <li><a href="https://www.akatsuki-novels.com/stories/view/852/novel_id~58?from=1#top">第二話</a></li>
    <li><a href="/stories/view/851/novel_id~58">第一話 重复</a></li>
    <li><a href="javascript:void(0)">展开</a></li>
  </ol>
</article></body></html>
"""

_AKATSUKI_BOOK_LEGACY = """
<html><body>
<h3 class="font-bb"><a>旧作品</a></h3>
<h3 class="font-bb">作者：<a>旧作者</a></h3>
<div class="body-x1 body-normal body-w640"><div><div>旧简介。</div></div></div>
<table class="list"><tbody>
  <tr><td><a href="/stories/view/1471/novel_id~103">第一話</a></td></tr>
</tbody></table>
</body></html>
"""


def test_akatsuki_book_info_new_layout(cfg: ParserConfig) -> None:
    info = AkatsukiNovelsParser(cfg).parse_book_info([_AKATSUKI_BOOK])

    assert info is not None
    assert info["book_name"] == "作品A"
    assert info["author"] == "作者A"
    assert info["summary"] == "作品简介。"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["851", "852"]
    assert [c["url"] for c in chapters] == [
        "https://www.akatsuki-novels.com/stories/view/851/novel_id~58",
        "https://www.akatsuki-novels.com/stories/view/852/novel_id~58",
    ]


def test_akatsuki_book_info_legacy_layout(cfg: ParserConfig) -> None:
    info = AkatsukiNovelsParser(cfg).parse_book_info([_AKATSUKI_BOOK_LEGACY])

    assert info is not None
    assert info["book_name"] == "旧作品"
    assert info["author"] == "旧作者"
    assert info["summary"] == "旧简介。"
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["1471"]


_AKATSUKI_CHAPTER = """
<html><body><article>
  <h1 class="min-w-0 text-xl font-bold">第一話　标题</h1>
  <div class="whitespace-pre-line reading-body mt-6">第一段。

第二段。</div>
</article></body></html>
"""


def test_akatsuki_chapter_content_new_and_legacy(cfg: ParserConfig) -> None:
    parser = AkatsukiNovelsParser(cfg)
    new = parser.parse_chapter_content([_AKATSUKI_CHAPTER], "851")
    assert new is not None
    assert new["title"] == "第一話　标题"
    assert "第一段。" in new["content"]
    assert "第二段。" in new["content"]

    legacy = parser.parse_chapter_content(
        ['<html><body><div class="body-novel">甲<br>乙</div></body></html>'], "1"
    )
    assert legacy is not None
    assert legacy["content"] == "甲\n乙"


@pytest.mark.parametrize("payload", ["", "   ", "<!DOCTYPE html>"])
def test_akatsuki_blank_pages_do_not_crash(cfg: ParserConfig, payload: str) -> None:
    parser = AkatsukiNovelsParser(cfg)
    assert parser.parse_book_info([payload]) is None
    assert parser.parse_chapter_content([payload], "1") is None
    assert _searcher(AkatsukiNovelsSearcher)._parse_html(payload) == []


# --------------------------------------------------------------------------- #
# alicesw: 2026 metadata layout, lazy cover, absolute chapter links
# --------------------------------------------------------------------------- #
_ALICESW_INFO = """
<html><body><div id="detail-box"><div class="detail-cols">
  <div class="box_intro">
    <div class="pic">
      <img src="https://img.example/placeholder.webp"
           data-src="/covers/14411.webp"
           class="lazyload_book_cover fengmian2">
    </div>
    <div class="box_info">
      <div class="novel_title"> 作品A </div>
      <div class="novel_info">
        <p>作 者：<a href="/search.html?q=a&f=author">作者A</a></p>
        <p>字 数：1.52万 · 章 节：2</p>
        <p>状 态：已完结</p>
      </div>
      <div class="tags_list">
        标签：<a href="/search.html?q=R-18&f=tag">R-18</a>
        <a href="/search.html?q=修仙&f=tag">修仙</a>
      </div>
    </div>
  </div>
</div></div>
<div class="jianjie"><h6>内容简介：</h6><p>一段简介。</p></div>
</body></html>
"""

_ALICESW_CATALOG = """
<html><body>
<ul class="mulu_list">
  <li><a href="/book/14597/aaa.html">第1章</a></li>
  <li><a href="https://www.alicesw.com/book/14597/bbb.html?from=list#top">第2章</a></li>
  <li><a href="/book/14597/aaa.html">第1章 重复</a></li>
  <li><a href="javascript:void(0)">展开</a></li>
</ul>
</body></html>
"""


def test_alicesw_book_info_new_metadata_and_ids(cfg: ParserConfig) -> None:
    info = AliceswParser(cfg).parse_book_info([_ALICESW_INFO, _ALICESW_CATALOG])

    assert info is not None
    assert info["book_name"] == "作品A"
    assert info["author"] == "作者A"
    assert info["cover_url"] == "https://www.alicesw.com/covers/14411.webp"
    assert info["tags"] == ["R-18", "修仙"]
    assert info["serial_status"] == "已完结"
    assert info["word_count"] == "1.52万"
    assert info["summary"] == "一段简介。"

    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["14597-aaa", "14597-bbb"]
    assert [c["url"] for c in chapters] == [
        "https://www.alicesw.com/book/14597/aaa.html",
        "https://www.alicesw.com/book/14597/bbb.html",
    ]


def test_alicesw_chapter_content(cfg: ParserConfig) -> None:
    page = """
    <html><body><h3 class="j_chapterName">第一章</h3>
    <div class="read-content j_readContent"><p>第一段。</p><p>第二段。</p></div>
    </body></html>
    """
    chapter = AliceswParser(cfg).parse_chapter_content([page], "14597-aaa")
    assert chapter is not None
    assert chapter["title"] == "第一章"
    assert chapter["content"] == "第一段。\n第二段。"


_ALICESW_SEARCH = """
<html><body>
<div class="list-group-item"><p>没有链接的行</p></div>
<div class="list-group-item">
  <h5><a href="/novel/14411.html">书A</a></h5>
  <p class="text-muted"><a>作者A</a> 字数：1.52万</p>
  <p class="timedesc">更新时间：2026-01-01</p>
</div>
<div class="list-group-item">
  <h5><a href="https://www.alicesw.com/novel/14412.html?from=list#top">书B</a></h5>
  <p class="text-muted"><a>作者B</a></p>
</div>
</body></html>
"""


def test_alicesw_search_ids_are_https_and_limit_skips_broken_rows() -> None:
    searcher = _searcher(AliceswSearcher)

    rows = searcher._parse_html(_ALICESW_SEARCH)

    assert [r["book_id"] for r in rows] == ["14411", "14412"]
    assert rows[0]["book_url"] == "https://www.alicesw.com/novel/14411.html"
    assert rows[1]["book_url"] == (
        "https://www.alicesw.com/novel/14412.html?from=list#top"
    )
    assert [r["book_id"] for r in searcher._parse_html(_ALICESW_SEARCH, limit=1)] == [
        "14411"
    ]


# --------------------------------------------------------------------------- #
# alphapolis: empty pages / anchor-less episode divs / absolute ids
# --------------------------------------------------------------------------- #
_ALPHAPOLIS_BOOK = """
<html><body>
<div class="cover novels section"><div class="content-main">
  <h1 class="title">作品A</h1>
  <div class="author"><a>作者A</a></div>
  <div class="content-tags"><span class="tag"><a>奇幻</a></span></div>
  <div class="abstract">一段简介。</div>
  <div class="episodes">
    <h3>第一卷</h3>
    <div class="episode"><a href="/novel/123/456?from=list#top">
      <span class="title">第一话</span></a></div>
    <div class="episode"></div>
    <div class="episode"><a href="/novel/123/456">
      <span class="title">重复</span></a></div>
    <h3>第二卷</h3>
    <div class="episode"><a href="https://www.alphapolis.co.jp/novel/123/457">
      <span class="title">第二话</span></a></div>
  </div>
</div></div>
<div class="content-info gray-menu section">
  <div class="cover"><img src="/cover/a.jpg"></div>
  <table class="detail"><tr><th>更新日時</th><td>2026-01-01</td></tr></table>
</div>
</body></html>
"""


def test_alphapolis_book_info_survives_anchorless_episode(cfg: ParserConfig) -> None:
    info = AlphapolisParser(cfg).parse_book_info([_ALPHAPOLIS_BOOK])

    assert info is not None
    assert info["book_name"] == "作品A"
    assert info["author"] == "作者A"
    assert info["cover_url"] == "https://www.alphapolis.co.jp/cover/a.jpg"
    assert info["update_time"] == "2026-01-01"
    assert [v["volume_name"] for v in info["volumes"]] == ["第一卷", "第二卷"]
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["456"]
    assert info["volumes"][0]["chapters"][0]["url"] == (
        "https://www.alphapolis.co.jp/novel/123/456"
    )
    assert [c["chapterId"] for c in info["volumes"][1]["chapters"]] == ["457"]


_ALPHAPOLIS_SEARCH = """
<html><body>
<div class="content-block novels">
  <h2 class="title"><a href="/novel/100/200?from=list#top">书A</a></h2>
  <div class="author"><a>作者A</a></div>
  <span class="content-status">連載</span>
  <span class="wordcount">文字数 10,000</span>
  <span class="updated">最終更新日 2026-01-01</span>
  <img src="https://cdn-image.example/a.jpg">
</div>
<div class="content-block novels"><p>没有链接的行</p></div>
<div class="content-block novels">
  <h2 class="title"><a href="https://www.alphapolis.co.jp/novel/100/201">书B</a></h2>
</div>
</body></html>
"""


def test_alphapolis_search_absolute_hrefs_and_limit() -> None:
    searcher = _searcher(AlphapolisSearcher)

    rows = searcher._parse_html(_ALPHAPOLIS_SEARCH)

    assert [r["book_id"] for r in rows] == ["100-200", "100-201"]
    assert rows[0]["title"] == "书A"
    assert rows[0]["cover_url"] == "https://cdn-image.example/a.jpg"
    assert [
        r["book_id"] for r in searcher._parse_html(_ALPHAPOLIS_SEARCH, limit=1)
    ] == ["100-200"]


@pytest.mark.parametrize("payload", ["", "   ", "<!DOCTYPE html>"])
def test_alphapolis_blank_pages_do_not_crash(cfg: ParserConfig, payload: str) -> None:
    parser = AlphapolisParser(cfg)
    assert parser.parse_book_info([payload]) is None
    assert parser.parse_chapter_content([payload], "1") is None
    assert _searcher(AlphapolisSearcher)._parse_html(payload) == []


# --------------------------------------------------------------------------- #
# bixiange: absolute/protocol-relative result links, chapter ids
# --------------------------------------------------------------------------- #
_BIXIANGE_SEARCH = """
<html><body><div class="list"><ul>
  <li>
    <div class="cover"><a href="/khjj/11945.html?from=list#top">
      <img src="//cdn.example/1.jpg"></a></div>
    <div class="title"><a>书A</a></div>
    <div class="tips"><span>时间：2026-01-01</span></div>
  </li>
  <li>
    <div class="cover"><a href="https://m.bixiange.top/wxxz/24318.html">书B</a></div>
  </li>
  <li><div class="cover"><a>没有链接</a></div></li>
</ul></div></body></html>
"""


def test_bixiange_search_absolute_hrefs_and_limit() -> None:
    searcher = _searcher(BixiangeSearcher)

    rows = searcher._parse_html(_BIXIANGE_SEARCH)

    assert [r["book_id"] for r in rows] == ["khjj-11945", "wxxz-24318"]
    assert rows[0]["cover_url"] == "https://cdn.example/1.jpg"
    assert rows[0]["update_date"] == "2026-01-01"
    assert [r["book_id"] for r in searcher._parse_html(_BIXIANGE_SEARCH, limit=1)] == [
        "khjj-11945"
    ]


_BIXIANGE_BOOK = """
<html><body>
<div class="info-right">
  <div class="desc">
    <h1>书A</h1>
    <div class="descTip">
      <p><span>分类：武侠修真</span></p>
      <p><span>作者：作者A</span></p>
      <p><span>时间：2026-01-01</span></p>
    </div>
    <div class="descInfo"><p>一段简介。</p></div>
  </div>
  <div class="cover"><img src="//cdn.example/cover.jpg"></div>
  <div class="catalog">
    <a href="/wxxz/24318/index/1.html?from=list#top">第一章</a>
    <a href="https://m.bixiange.top/wxxz/24318/index/2.html">第二章</a>
    <a href="/wxxz/24318/index/1.html">第一章 重复</a>
    <a href="javascript:void(0)">展开</a>
  </div>
</div>
</body></html>
"""


def test_bixiange_book_info_ids_and_urls(cfg: ParserConfig) -> None:
    info = BixiangeParser(cfg).parse_book_info([_BIXIANGE_BOOK])

    assert info is not None
    assert info["book_name"] == "书A"
    assert info["author"] == "作者A"
    assert info["cover_url"] == "https://cdn.example/cover.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1", "2"]
    assert [c["url"] for c in chapters] == [
        "/wxxz/24318/index/1.html",
        "https://m.bixiange.top/wxxz/24318/index/2.html",
    ]


# --------------------------------------------------------------------------- #
# ciluke: search ids from path-only forms
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "href",
    [
        "/54/54978/",
        "//www.ciluke.com/54/54978/",
        "https://www.ciluke.com/54/54978/",
        "https://www.ciluke.com/54/54978/?from=list#top",
        "https://www.ciluke.com/54/54978.html",
    ],
)
def test_ciluke_url_to_id_is_path_only(href: str) -> None:
    assert CilukeSearcher._url_to_id(href) == "54-54978"


# --------------------------------------------------------------------------- #
# ciyuanji: JSON search guards and chapter book id
# --------------------------------------------------------------------------- #
_CYUANJI_SEARCH_STATE = {
    "props": {
        "pageProps": {
            "list": [
                None,
                {
                    "bookId": 6723,
                    "bookName": "书A",
                    "authorName": None,
                    "imgUrl": "//img.example/a.jpg",
                    "latestUpdateTime": "2026-01-01",
                    "latestChapterName": "第1章",
                    "wordCount": 1234,
                },
                {"bookName": "没有 id"},
                {
                    "bookId": 6724,
                    "bookName": "书B",
                    "authorName": "作者B",
                    "blurryImgUrl": "/blur.jpg",
                },
            ]
        }
    }
}


def _next_data(payload: Any) -> str:
    return (
        '<html><body><script id="__NEXT_DATA__">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def test_ciyuanji_search_skips_null_rows_and_absolutizes_cover() -> None:
    searcher = _searcher(CiyuanjiSearcher)

    rows = searcher._parse_html(_next_data(_CYUANJI_SEARCH_STATE))

    assert [r["book_id"] for r in rows] == ["6723", "6724"]
    assert rows[0]["title"] == "书A"
    assert rows[0]["author"] == ""
    assert rows[0]["cover_url"] == "https://img.example/a.jpg"
    assert rows[0]["word_count"] == "1234"
    assert rows[1]["cover_url"] == "https://www.ciyuanji.com/blur.jpg"
    assert [
        r["book_id"]
        for r in searcher._parse_html(_next_data(_CYUANJI_SEARCH_STATE), limit=1)
    ] == ["6723"]


def test_ciyuanji_search_malformed_json_and_blank_page() -> None:
    searcher = _searcher(CiyuanjiSearcher)

    assert (
        searcher._parse_html('<html><script id="__NEXT_DATA__">{oops</script></html>')
        == []
    )
    assert searcher._parse_html("") == []


_CYUANJI_BOOK_STATE = {
    "props": {
        "pageProps": {
            "book": {
                "bookId": 6723,
                "bookName": "书A",
                "authorName": "作者A",
                "imgUrl": "https://img.example/cover.jpg",
            },
            "bookChapter": {
                "chapterList": [
                    {
                        "bookId": None,
                        "volumeId": 1,
                        "title": "第一卷",
                        "chapterId": 2306300,
                        "chapterName": "第一章",
                        "sortNum": 1,
                        "volumeSortNum": 1,
                        "isFee": "0",
                        "isBuy": "0",
                    },
                    {
                        "volumeId": 1,
                        "title": "第一卷",
                        "chapterId": 2306301,
                        "chapterName": "第二章",
                        "sortNum": 2,
                        "volumeSortNum": 1,
                        "isFee": "1",
                        "isBuy": "1",
                    },
                ]
            },
        }
    }
}


def test_ciyuanji_chapter_url_uses_the_book_id(cfg: ParserConfig) -> None:
    info = CiyuanjiParser(cfg).parse_book_info([_next_data(_CYUANJI_BOOK_STATE)])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["2306300", "2306301"]
    assert [c["url"] for c in chapters] == [
        "/chapter/6723_2306300.html",
        "/chapter/6723_2306301.html",
    ]
    # The paid chapter is listed but marked unreadable.
    assert [c["accessible"] for c in chapters] == [True, True]


def test_ciyuanji_broken_ciphertext_does_not_raise(cfg: ParserConfig) -> None:
    chapter_state = {
        "props": {
            "pageProps": {
                "chapterContent": {
                    "chapterId": 1,
                    "chapterName": "第一章",
                    "content": "!!!not-base64!!!",
                }
            }
        }
    }
    assert (
        CiyuanjiParser(cfg).parse_chapter_content([_next_data(chapter_state)], "1")
        is None
    )


# --------------------------------------------------------------------------- #
# czbooks: book/chapter titles, ids and urls
# --------------------------------------------------------------------------- #
_CZBOOKS_SEARCH = """
<html><body>
<ul class="nav novel-list style-default">
  <li class="novel-item-wrapper">
    <div class="novel-item">
      <div class="novel-item-cover-wrapper">
        <a href="//czbooks.net/n/u81a?from=list#top">
          <div class="novel-item-thumbnail"><img src="//img.example/a.jpg"></div>
          <div class="novel-item-title">书A</div>
        </a>
      </div>
      <div class="novel-item-author">作者: <a href="//czbooks.net/a/x">作者A</a></div>
      <div class="novel-item-newest-chapter">最新: <a>第9章</a></div>
      <div class="novel-item-date">2026-01-01</div>
    </div>
  </li>
  <li class="novel-item-wrapper"><div class="novel-item">没有链接</div></li>
  <li class="novel-item-wrapper">
    <div class="novel-item">
      <div class="novel-item-cover-wrapper"><a href="/n/s6ob0">书B</a></div>
    </div>
  </li>
</ul>
</body></html>
"""


def test_czbooks_search_ids_and_limit() -> None:
    searcher = _searcher(CzbooksSearcher)

    rows = searcher._parse_html(_CZBOOKS_SEARCH)

    assert [r["book_id"] for r in rows] == ["u81a", "s6ob0"]
    assert rows[0]["title"] == "书A"
    assert rows[0]["author"] == "作者A"
    assert [r["book_id"] for r in searcher._parse_html(_CZBOOKS_SEARCH, limit=1)] == [
        "u81a"
    ]


_CZBOOKS_BOOK = """
<html><body>
<div class="state"><table>
  <tr><td>更新時間</td><td>2026-01-01</td></tr>
  <tr><td>連載狀態</td><td>連載中</td></tr>
</table></div>
<div class="info-wrap"><div class="info">
  <h1 class="title">《书A》</h1>
  <span class="author">作者: <a>作者A</a></span>
</div></div>
<div class="thumbnail"><img src="//img.example/cover.jpg"></div>
<a id="novel-category">玄幻</a>
<div class="description">一段简介。</div>
<ul id="chapter-list">
  <li><a href="//czbooks.net/n/u81a/c1?chapterNumber=0">第0章</a></li>
  <li><a href="/n/u81a/c2#top">第1章</a></li>
  <li><a href="//czbooks.net/n/u81a/c1?chapterNumber=0">第0章 重复</a></li>
  <li><a href="javascript:void(0)">展开</a></li>
</ul>
</body></html>
"""


def test_czbooks_book_info_and_chapter_titles(cfg: ParserConfig) -> None:
    parser = CzbooksParser(cfg)
    info = parser.parse_book_info([_CZBOOKS_BOOK])

    assert info is not None
    assert info["book_name"] == "《书A》"
    assert info["author"] == "作者A"
    assert info["cover_url"] == "https://img.example/cover.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["c1", "c2"]
    assert [c["url"] for c in chapters] == [
        "https://czbooks.net/n/u81a/c1",
        "https://czbooks.net/n/u81a/c2",
    ]

    chapter = parser.parse_chapter_content(
        [
            '<html><body><h1 class="name">《书A》第0章</h1>'
            '<div class="content">第一段。<br>第二段。</div></body></html>'
        ],
        "c1",
    )
    assert chapter is not None
    assert chapter["title"] == "《书A》第0章"
    assert chapter["content"] == "第一段。\n第二段。"


# --------------------------------------------------------------------------- #
# dushu: cover/chapter urls are absolute, ids ignore query/fragment
# --------------------------------------------------------------------------- #
_DUSHU_BOOK = """
<html><body>
<div class="book-title"><h1>书A</h1></div>
<div class="book-details"><table>
  <tr><td>作 者</td><td>作者A</td></tr>
</table></div>
<div class="book-pic"><img src="/cover/a.jpg"></div>
<div class="txtsummary"> 一段简介。 </div>
<div class="book-summary">
  <div class="book-chapter">正文</div>
  <table>
    <tr>
      <td><a href="/showbook/139190/1987964.html?from=list#top">第一章</a></td>
      <td><a href="https://www.dushu.com/showbook/139190/1987965.html">第二章</a></td>
    </tr>
    <tr><td><a href="/showbook/139190/1987964.html">第一章 重复</a></td></tr>
  </table>
</div>
</body></html>
"""


def test_dushu_book_info_absolutizes_urls(cfg: ParserConfig) -> None:
    info = DushuParser(cfg).parse_book_info([_DUSHU_BOOK])

    assert info is not None
    assert info["book_name"] == "书A"
    assert info["author"] == "作者A"
    assert info["cover_url"] == "https://www.dushu.com/cover/a.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1987964", "1987965"]
    assert [c["url"] for c in chapters] == [
        "https://www.dushu.com/showbook/139190/1987964.html",
        "https://www.dushu.com/showbook/139190/1987965.html",
    ]


# --------------------------------------------------------------------------- #
# dxmwx: cover must not be prefixed twice, chapter ids use the suffix
# --------------------------------------------------------------------------- #
_DXMWX_INFO = """
<html><body>
<span style="font-size: 24px">书A</span>
<div style="height: 28px">作者：<a>作者A</a></div>
<span class="typebut"><a>玄幻</a></span>
<img class="imgwidth" src="IMGSRC">
<span>更新时间：2026-01-01</span>
<div style="border-bottom: 1px solid #ccc"><div>一段简介。</div></div>
</body></html>
"""

_DXMWX_CATALOG = """
<html><body>
<div style="height:40px;border-bottom:1px solid #ccc">
  <a href="/read/57215_50197663.html?from=list#top">第一章</a>
</div>
<div style="height:40px;border-bottom:1px solid #ccc">
  <a href="https://www.dxmwx.org/read/57215_50197664.html">第二章</a>
</div>
<div style="height:40px;border-bottom:1px solid #ccc">
  <a href="/book/10409.html">返回目录</a>
</div>
<div style="height:40px;border-bottom:1px solid #ccc">
  <a href="/read/57215_50197663.html">第一章 重复</a>
</div>
</body></html>
"""


@pytest.mark.parametrize(
    ("img_src", "expected"),
    [
        ("/files/cover.jpg", "https://www.dxmwx.org/files/cover.jpg"),
        ("//cdn.example/cover.jpg", "https://cdn.example/cover.jpg"),
        (
            "https://cdn.example/already.jpg",
            "https://cdn.example/already.jpg",
        ),
    ],
)
def test_dxmwx_cover_is_resolved_once(
    cfg: ParserConfig, img_src: str, expected: str
) -> None:
    info = DxmwxParser(cfg).parse_book_info(
        [_DXMWX_INFO.replace("IMGSRC", img_src), _DXMWX_CATALOG]
    )

    assert info is not None
    assert info["cover_url"] == expected


def test_dxmwx_book_info_chapter_ids_and_urls(cfg: ParserConfig) -> None:
    info = DxmwxParser(cfg).parse_book_info([_DXMWX_INFO, _DXMWX_CATALOG])

    assert info is not None
    assert info["book_name"] == "书A"
    assert info["author"] == "作者A"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["50197663", "50197664"]
    assert [c["url"] for c in chapters] == [
        "https://www.dxmwx.org/read/57215_50197663.html",
        "https://www.dxmwx.org/read/57215_50197664.html",
    ]


_DXMWX_SEARCH = """
<html><body><div id="ListContents">
  <div style="position: relative">
    <div class="margin0h5"><a href="/book/10409.html?from=list#top">书A</a>
      <a>作者A</a></div>
    <div class="imgwidth"><img src="//cdn.example/a.jpg"></div>
    <span class="lefth5">2026-01-01</span>
  </div>
  <div style="position: relative"><p>没有链接的行</p></div>
  <div style="position: relative">
    <div class="margin0h5"><a href="https://www.dxmwx.org/book/10410.html">书B</a></div>
  </div>
</div></body></html>
"""


def test_dxmwx_search_ids_and_limit() -> None:
    searcher = _searcher(DxmwxSearcher)

    rows = searcher._parse_html(_DXMWX_SEARCH)

    assert [r["book_id"] for r in rows] == ["10409", "10410"]
    assert rows[0]["cover_url"] == "https://cdn.example/a.jpg"
    assert [r["book_id"] for r in searcher._parse_html(_DXMWX_SEARCH, limit=1)] == [
        "10409"
    ]


@pytest.mark.parametrize("payload", ["", "   ", "<!DOCTYPE html>"])
def test_dxmwx_blank_pages_do_not_crash(cfg: ParserConfig, payload: str) -> None:
    parser = DxmwxParser(cfg)
    assert parser.parse_book_info([payload, payload]) is None
    assert parser.parse_chapter_content([payload], "1") is None
    assert _searcher(DxmwxSearcher)._parse_html(payload) == []
