#!/usr/bin/env python3
"""Offline fixtures for the E-group site plugins (2026-11 network probe).

Live results from this host:

* ``n71ge``, ``novelpia``, ``piaotia``, ``qbtr``, ``sfacg`` and
  ``shaoniandream`` all answered and returned rows;
* ``piaotia`` redirects an exact title match (302) to the book-info page, so
  its chapter/review tables — also class ``grid`` — were being emitted as
  search hits with absolute URLs in ``book_id``;
* ``ruochu`` is reachable but has no registered searcher; following a book URL
  showed the old author selector picked the site disclaimer instead of the
  author link;
* ``pilibook`` returned HTTP 403 to every request from this host;
* ``shencou`` now redirects to ``wowenku.com`` and that origin answered 403,
  so its pages could not be re-verified;
* ``shauthor`` is reachable but has no registered searcher (its parser is the
  shared biquge4 one);
* ``n69shuba`` is still HTTP 403 here (Cloudflare), so its Referer fix below is
  pinned offline only.

The fixtures also pin the shared defects found across the reachable sites:
ids derived from raw ``href`` absorbed query strings/fragments, blank bodies
crashed ``lxml.html.fromstring``, and covers/resources were concatenated
without checking whether they were already absolute.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from novel_downloader.plugins.sites.n69shuba.fetcher import N69shubaFetcher
from novel_downloader.plugins.sites.n71ge.parser import N71geParser
from novel_downloader.plugins.sites.n71ge.searcher import N71geSearcher
from novel_downloader.plugins.sites.novelpia.parser import NovelpiaParser
from novel_downloader.plugins.sites.novelpia.searcher import NovelpiaSearcher
from novel_downloader.plugins.sites.piaotia.parser import PiaotiaParser
from novel_downloader.plugins.sites.piaotia.searcher import PiaotiaSearcher
from novel_downloader.plugins.sites.pilibook.parser import PilibookParser
from novel_downloader.plugins.sites.qbtr.parser import QbtrParser
from novel_downloader.plugins.sites.qbtr.searcher import QbtrSearcher
from novel_downloader.plugins.sites.ruochu.parser import RuochuParser
from novel_downloader.plugins.sites.sfacg.parser import SfacgParser
from novel_downloader.plugins.sites.sfacg.searcher import SfacgSearcher
from novel_downloader.plugins.sites.shaoniandream.parser import ShaoniandreamParser
from novel_downloader.plugins.sites.shaoniandream.searcher import (
    ShaoniandreamSearcher,
)
from novel_downloader.plugins.sites.shencou.parser import ShencouParser
from novel_downloader.schemas import FetcherConfig, ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


def _searcher(cls: type, session: Any = None) -> Any:
    return cls(session if session is not None else object())


# --------------------------------------------------------------------------- #
# n71ge: ids/urls from the search table and cover/chapter normalisation
# --------------------------------------------------------------------------- #
_N71GE_SEARCH = """
<html><body><table class="grid">
  <tr><th>文章名称</th><th>最新章节</th><th>作者</th><th>字数</th><th>更新</th></tr>
  <tr>
    <td><a href="/460_460073/?from=list#top">书A</a></td>
    <td><a>第一卷 第51章</a></td><td>作者A</td><td>120736K</td><td>26-11-12</td>
  </tr>
  <tr>
    <td><a href="https://www.71ge.com/460_460972/">书B</a></td>
    <td><a>第2章</a></td><td>作者B</td><td>463692K</td><td>26-11-12</td>
  </tr>
  <tr>
    <td><a href="javascript:void(0)">坏链接</a></td>
    <td><a>-</a></td><td>-</td><td>-</td><td>-</td>
  </tr>
  <tr>
    <td><a href="/48_48009/">书C</a></td>
    <td><a>第84章</a></td><td></td><td>27923137K</td><td>26-11-07</td>
  </tr>
</table></body></html>
"""

_N71GE_INFO = """
<html><body>
<meta property="og:image" content="/image/135/135051/135051s.jpg" />
<div class="introduce"><h1>书A</h1></div>
<div class="ml_list"><ul>
  <li><a href="/48_48009/1.html?from=list#top">第一章</a></li>
  <li><a href="https://www.71ge.com/48_48009/2.html">第二章</a></li>
  <li><a href="javascript:void(0)">坏链接</a></li>
  <li><a href="">空链接</a></li>
</ul></div>
</body></html>
"""

_N71GE_INFO_ABSOLUTE_COVER = _N71GE_INFO.replace(
    "/image/135/135051/135051s.jpg", "https://cdn.example/cover.jpg"
)
_N71GE_INFO_NO_COVER = _N71GE_INFO.replace(
    '<meta property="og:image" content="/image/135/135051/135051s.jpg" />', ""
)

_N71GE_CHAPTER = """
<html><body><div id="nr_content">
  <div class="nr_title"><h3>第一卷 第一章 惊蛰（1/3）</h3></div>
  <div class="novelcontent">
    <p class="articlecontent">第一段<br/>第二段</p>
  </div>
</div></body></html>
"""


def test_n71ge_search_ids_urls_and_limit() -> None:
    searcher = _searcher(N71geSearcher)

    rows = searcher._parse_html(_N71GE_SEARCH)

    assert [r["book_id"] for r in rows] == ["460_460073", "460_460972", "48_48009"]
    assert [r["book_url"] for r in rows] == [
        "https://www.71ge.com/460_460073/",
        "https://www.71ge.com/460_460972/",
        "https://www.71ge.com/48_48009/",
    ]
    assert rows[0]["author"] == "作者A"
    assert rows[2]["author"] == ""
    assert [r["priority"] for r in rows] == [
        N71geSearcher.priority,
        N71geSearcher.priority + 1,
        N71geSearcher.priority + 2,
    ]
    assert [r["book_id"] for r in searcher._parse_html(_N71GE_SEARCH, limit=2)] == [
        "460_460073",
        "460_460972",
    ]


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (_N71GE_INFO, "https://www.71ge.com/image/135/135051/135051s.jpg"),
        (_N71GE_INFO_ABSOLUTE_COVER, "https://cdn.example/cover.jpg"),
        (_N71GE_INFO_NO_COVER, ""),
    ],
)
def test_n71ge_book_cover_is_absolute_or_empty(
    cfg: ParserConfig, page: str, expected: str
) -> None:
    info = N71geParser(cfg).parse_book_info([page])

    assert info is not None
    assert info["cover_url"] == expected


def test_n71ge_book_chapter_ids_ignore_query(cfg: ParserConfig) -> None:
    info = N71geParser(cfg).parse_book_info([_N71GE_INFO])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1", "2"]
    assert chapters[0]["url"].startswith("https://www.71ge.com/48_48009/1.html")


def test_n71ge_chapter_title_strips_page_marker(cfg: ParserConfig) -> None:
    chapter = N71geParser(cfg).parse_chapter_content([_N71GE_CHAPTER], "1")

    assert chapter is not None
    assert chapter["title"] == "第一卷 第一章 惊蛰"
    assert chapter["content"] == "第一段\n第二段"


def test_n71ge_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = N71geParser(cfg)

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert parser.parse_chapter_content(["<html></html>"], "1") is None


# --------------------------------------------------------------------------- #
# novelpia: protocol-relative covers and tolerant JSON handling
# --------------------------------------------------------------------------- #
_NOVELPIA_SEARCH = """
<html><body>
  <div class="novelbox"><b>没有链接</b></div>
  <div class="novelbox"><table><tr>
    <td><img src="//cimage.novelpia.jp/cover/a.jpg" /></td>
    <td valign="top"><font>作者A</font>
      <b onclick="location.href='/novel/305?from=list'">书A</b>
    </td>
  </tr></table></div>
  <div class="novelbox"><table><tr>
    <td><img src="/img/b.jpg" /></td>
    <td valign="top"><font>作者B</font>
      <b><a href="/novel/117#top">书B</a></b>
    </td>
  </tr></table></div>
</body></html>
"""

_NOVELPIA_INFO = {
    "novel": {
        "novel_name": "书A",
        "writer_nick": "作者A",
        "cover_img": "//cimage.novelpia.jp/cover/a.jpg",
        "last_write_date": "2026-11-01",
        "novel_genre_arr": ["奇幻"],
        "novel_story": "第一行<br>x",
    }
}

_NOVELPIA_INFO_RELATIVE_COVER = {
    "novel": {**_NOVELPIA_INFO["novel"], "cover_img": "/img/a.jpg"}
}
_NOVELPIA_INFO_NO_COVER = {"novel": {**_NOVELPIA_INFO["novel"], "cover_img": ""}}

_NOVELPIA_CATALOG = """
<html><body><table>
  <tr class="ep_style5"><td class="font12" data-content-no="9001">
    <b>第1話 無料 タイトル</b>
  </td></tr>
  <tr class="ep_style5"><td class="font12"><b>没有编号</b></td></tr>
</table></body></html>
"""

_NOVELPIA_CHAPTER = {
    "s": [
        {"text": "第一段"},
        {"text": "<img src='//cimage.novelpia.jp/p.jpg'>"},
        {"text": ""},
    ]
}


def test_novelpia_search_ids_and_covers() -> None:
    searcher = _searcher(NovelpiaSearcher)

    rows = searcher._parse_html(_NOVELPIA_SEARCH)

    assert [r["book_id"] for r in rows] == ["305", "117"]
    assert [r["cover_url"] for r in rows] == [
        "https://cimage.novelpia.jp/cover/a.jpg",
        "https://novelpia.jp/img/b.jpg",
    ]
    assert [r["title"] for r in rows] == ["书A", "书B"]
    assert [r["priority"] for r in rows] == [
        NovelpiaSearcher.priority,
        NovelpiaSearcher.priority + 1,
    ]
    assert [r["book_id"] for r in searcher._parse_html(_NOVELPIA_SEARCH, limit=1)] == [
        "305"
    ]


@pytest.mark.parametrize(
    ("novel", "expected"),
    [
        (_NOVELPIA_INFO, "https://cimage.novelpia.jp/cover/a.jpg"),
        (_NOVELPIA_INFO_RELATIVE_COVER, "https://novelpia.jp/img/a.jpg"),
        (_NOVELPIA_INFO_NO_COVER, ""),
    ],
)
def test_novelpia_cover_normalisation(
    cfg: ParserConfig, novel: dict[str, Any], expected: str
) -> None:
    info = NovelpiaParser(cfg).parse_book_info([json.dumps(novel), _NOVELPIA_CATALOG])

    assert info is not None
    assert info["cover_url"] == expected


def test_novelpia_catalog_and_chapter(cfg: ParserConfig) -> None:
    parser = NovelpiaParser(cfg)
    info = parser.parse_book_info([json.dumps(_NOVELPIA_INFO), _NOVELPIA_CATALOG])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["9001"]
    assert chapters[0]["title"] == "第1話 タイトル"

    chapter = parser.parse_chapter_content([json.dumps(_NOVELPIA_CHAPTER)], "9001")
    assert chapter is not None
    assert chapter["content"] == "第一段"
    assert chapter["extra"]["resources"][0]["url"] == (
        "https://cimage.novelpia.jp/p.jpg"
    )


def test_novelpia_tolerates_bad_payloads(cfg: ParserConfig) -> None:
    parser = NovelpiaParser(cfg)

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["{bad json}", _NOVELPIA_CATALOG]) is None
    assert parser.parse_book_info([json.dumps({"novel": []}), ""]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert parser.parse_chapter_content(["{bad json}"], "1") is None
    assert _searcher(NovelpiaSearcher)._parse_html("") == []


# --------------------------------------------------------------------------- #
# piaotia: exact-match redirect + list rows
# --------------------------------------------------------------------------- #
_PIAOTIA_BOOK = """
<html><body>
<h1>剑来</h1>
<table>
  <tr>
    <td>类&nbsp;&nbsp;别：玄幻魔法</td>
    <td>作&nbsp;&nbsp;者：烽火戏诸侯</td>
    <td>全文长度：16421602字</td>
  </tr>
  <tr><td>最后更新：2026-10-15</td><td>文章状态：连载中</td></tr>
</table>
<td width="80%"><img src="/files/article/image/8/8717/8717s.jpg" /></td>
<a href="/html/8/8717/">返回目录</a>
<a href="/html/8/8717/13129846.html">第76章 灯谜</a>
<a href="/html/8/8717/index.html">(查看全部章节)</a>
</body></html>
"""

_PIAOTIA_SEARCH = """
<html><body><table class="grid">
  <tr><th>文章名称</th><th>最新章节</th><th>作者</th><th>字数</th><th>更新</th></tr>
  <tr>
    <td><a href="https://www.piaotia.com/bookinfo/10/10289.html?from=list#top">
      修真高手的田园生活</a></td>
    <td><a>第455章 尽忠职守</a></td><td>小学嗣业</td>
    <td>21247K</td><td>26-11-12</td>
  </tr>
  <tr>
    <td><a href="/bookinfo/15/15458.html">超级修真弃少</a></td>
    <td><a>第五千五百八十六章</a></td><td>木木鱼猫</td>
    <td>33885K</td><td>26-11-12</td>
  </tr>
  <tr>
    <td><a href="/modules/article/reviews.php?aid=1">书评</a></td>
    <td>-</td><td>-</td><td>-</td><td>-</td>
  </tr>
</table></body></html>
"""

_PIAOTIA_CATALOG = """
<html><body><div class="centent"><ul>
  <li><a href="13129846.html?from=list#top">第76章 灯谜</a></li>
  <li><a href="https://www.piaotia.com/html/8/8717/13124439.html">第75章 折桂</a></li>
  <li><a href="javascript:void(0)">坏链接</a></li>
</ul></div></body></html>
"""


def test_piaotia_exact_match_returns_one_book() -> None:
    rows = _searcher(PiaotiaSearcher)._parse_html(_PIAOTIA_BOOK)

    assert len(rows) == 1
    row = rows[0]
    assert row["book_id"] == "8-8717"
    assert row["book_url"] == "https://www.piaotia.com/bookinfo/8/8717.html"
    assert row["title"] == "剑来"
    assert row["author"] == "烽火戏诸侯"
    assert row["latest_chapter"] == "第76章 灯谜"
    assert row["update_date"] == "2026-10-15"
    assert row["word_count"] == "16421602字"
    assert row["cover_url"] == (
        "https://www.piaotia.com/files/article/image/8/8717/8717s.jpg"
    )


def test_piaotia_search_rows_ignore_reviews_and_query() -> None:
    searcher = _searcher(PiaotiaSearcher)

    rows = searcher._parse_html(_PIAOTIA_SEARCH)

    assert [r["book_id"] for r in rows] == ["10-10289", "15-15458"]
    assert rows[0]["book_url"] == "https://www.piaotia.com/bookinfo/10/10289.html"
    assert rows[0]["title"] == "修真高手的田园生活"
    assert [r["priority"] for r in rows] == [
        PiaotiaSearcher.priority,
        PiaotiaSearcher.priority + 1,
    ]
    assert [r["book_id"] for r in searcher._parse_html(_PIAOTIA_SEARCH, limit=1)] == [
        "10-10289"
    ]


def test_piaotia_parser_cover_and_chapter_ids(cfg: ParserConfig) -> None:
    info = PiaotiaParser(cfg).parse_book_info([_PIAOTIA_BOOK, _PIAOTIA_CATALOG])

    assert info is not None
    assert info["cover_url"] == (
        "https://www.piaotia.com/files/article/image/8/8717/8717s.jpg"
    )
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["13129846", "13124439"]


def test_piaotia_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = PiaotiaParser(cfg)

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["<html></html>", ""]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert _searcher(PiaotiaSearcher)._parse_html("   ") == []


# --------------------------------------------------------------------------- #
# qbtr: ids, chapter ids and download-link normalisation
# --------------------------------------------------------------------------- #
_QBTR_SEARCH = """
<html><body><div class="books m-cols">
  <div class="bk">
    <h3><a href="https://www.qbtr.cc/tongren/8038.html?from=list#top">书A</a></h3>
    <div class="booknews">作者： 作者A <label class="date">2024-03-17</label></div>
  </div>
  <div class="bk"><h3><a href="javascript:void(0)">坏链接</a></h3></div>
  <div class="bk"><h3><a href="/tongren/8039.html">书B</a></h3>
    <div class="booknews">作者： 作者B</div>
  </div>
</div></body></html>
"""

_QBTR_BOOK = """
<html><body>
<div class="infos"><h1>书A</h1><p>一段简介。</p></div>
<div class="date"><span><a>作者A</a></span>作者：作者A 日期：2024-03-17</div>
<div class="book_list"><ul>
  <li><a href="/tongren/8038/12345.html?from=list#top">第一章</a></li>
  <li><a href="https://www.qbtr.cc/tongren/8038/12346.html">第二章</a></li>
  <li><a href="javascript:void(0)">坏链接</a></li>
  <li><a href="">空链接</a></li>
</ul></div>
</body></html>
"""


def test_qbtr_search_ids_and_limit() -> None:
    searcher = _searcher(QbtrSearcher)

    rows = searcher._parse_html(_QBTR_SEARCH)

    assert [r["book_id"] for r in rows] == ["tongren-8038", "tongren-8039"]
    assert rows[0]["book_url"] == "https://www.qbtr.cc/tongren/8038.html"
    assert rows[0]["author"] == "作者A"
    assert [r["book_id"] for r in searcher._parse_html(_QBTR_SEARCH, limit=1)] == [
        "tongren-8038"
    ]


def test_qbtr_parser_chapters_and_blank(cfg: ParserConfig) -> None:
    parser = QbtrParser(cfg)
    info = parser.parse_book_info([_QBTR_BOOK])

    assert info is not None
    assert info["author"] == "作者A"
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == [
        "12345",
        "12346",
    ]
    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_chapter_content([""], "12345") is None


def test_qbtr_download_link_normalisation() -> None:
    assert QbtrParser._fix_download_link("") == ""
    assert QbtrParser._fix_download_link("qb../down.php?id=1") == (
        "https://www.qbtr.cc/e/DownSys/down.php?id=1"
    )
    assert QbtrParser._fix_download_link("https://cdn.example/a.txt") == (
        "https://cdn.example/a.txt"
    )
    assert QbtrParser._fix_download_link("//cdn.example/a.txt") == (
        "https://cdn.example/a.txt"
    )


# --------------------------------------------------------------------------- #
# sfacg: cover variants and blank JSON bodies
# --------------------------------------------------------------------------- #
_SFACG_SEARCH = json.dumps(
    {
        "Status": 200,
        "Novels": [
            {
                "NovelID": 387418,
                "NovelName": "书A",
                "AuthorName": "作者A",
                "NovelCover": "2020/10/a.jpg",
            },
            {
                "NovelID": 473447,
                "NovelName": "书B",
                "AuthorName": "作者B",
                "NovelCover": "https://cdn.example/b.jpg",
            },
            {
                "NovelID": 473448,
                "NovelName": "书C",
                "AuthorName": "作者C",
                "NovelCover": "//cdn.example/c.jpg",
            },
        ],
    }
)

_SFACG_INFO = """
<html><body>
<span class="book_newtitle">书A</span>
<div class="book_info2"><span>同人</span><span>已完结</span></div>
<span class="book_info3">作者A / 14971字 / x</span>
<span class="book_info3">18-07-01 22:48</span>
<ul class="book_info"><img src="//rs.sfacg.com/cover.jpg" /></ul>
<ul class="book_profile"><li class="book_bk_qs1">一段简介。</li></ul>
</body></html>
"""

_SFACG_INFO_ABSOLUTE_COVER = _SFACG_INFO.replace(
    "//rs.sfacg.com/cover.jpg", "https://cdn.example/cover.jpg"
)
_SFACG_INFO_NO_COVER = _SFACG_INFO.replace(
    '<ul class="book_info"><img src="//rs.sfacg.com/cover.jpg" /></ul>',
    '<ul class="book_info"></ul>',
)

_SFACG_CATALOG = """
<html><body>
<div class="mulu">第一卷</div>
<ul><ul class="mulu_list">
  <a href="/c/1889482/"><li>第1章 开始（1/2）</li></a>
  <a href="javascript:void(0)"><li>坏链接</li></a>
</ul></ul>
</body></html>
"""


def test_sfacg_search_cover_variants() -> None:
    rows = _searcher(SfacgSearcher)._parse_html(_SFACG_SEARCH)

    assert [r["book_id"] for r in rows] == ["387418", "473447", "473448"]
    assert [r["cover_url"] for r in rows] == [
        "https://rs.sfacg.com/web/novel/images/NovelCover/Big/2020/10/a.jpg",
        "https://cdn.example/b.jpg",
        "https://cdn.example/c.jpg",
    ]


def test_sfacg_search_tolerates_bad_json() -> None:
    searcher = _searcher(SfacgSearcher)

    assert searcher._parse_html("") == []
    assert searcher._parse_html("[]") == []
    assert searcher._parse_html("{bad json}") == []


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (_SFACG_INFO, "https://rs.sfacg.com/cover.jpg"),
        (_SFACG_INFO_ABSOLUTE_COVER, "https://cdn.example/cover.jpg"),
        (_SFACG_INFO_NO_COVER, ""),
    ],
)
def test_sfacg_cover_never_gets_a_bare_scheme(
    cfg: ParserConfig, page: str, expected: str
) -> None:
    info = SfacgParser(cfg).parse_book_info([page, _SFACG_CATALOG])

    assert info is not None
    assert info["cover_url"] == expected


def test_sfacg_catalog_and_blank_pages(cfg: ParserConfig) -> None:
    parser = SfacgParser(cfg)
    info = parser.parse_book_info([_SFACG_INFO, _SFACG_CATALOG])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1889482"]
    assert chapters[0]["title"] == "第1章 开始"
    assert chapters[0]["url"] == "https://m.sfacg.com/c/1889482/"

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_chapter_content([""], "1") is None


# --------------------------------------------------------------------------- #
# shaoniandream: search ids/covers and parser normalisation
# --------------------------------------------------------------------------- #
_SD_SEARCH = """
<html><body><div class="BookPicList">
  <dl>
    <dd class="img"><a href="https://www.shaoniandream.com/book_detail/4850?x=1#top">
      <img data-original="//alioss.example/cover.webp" /></a></dd>
    <dd class="title"><a>书A</a></dd>
    <dd class="author"><a>作者A</a></dd>
    <dd class="newChapter"><a>最新章节：第1章</a></dd>
  </dl>
  <dl><dd class="img"><a href="javascript:void(0)">坏</a></dd></dl>
  <dl>
    <dd class="img"><a href="/book_detail/7523"><img src="/img/b.webp" /></a></dd>
    <dd class="title"><a>书B</a></dd>
    <dd class="author"><a>作者B</a></dd>
  </dl>
</div></body></html>
"""

_SD_INFO = """
<html><body>
<div class="bookdetail-name"><span class="title">书A</span><i>连载中</i></div>
<span class="penName"><a>作者A</a></span>
<div class="cover"><img src="/img/cover.webp" /></div>
<div class="bookdetial-newchapter"><span>● 第1章</span></div>
<div class="font-list"><span>12万字</span></div>
<div class="label-list"><span>奇幻</span></div>
<div class="bookdetial-jianjie">一段简介。</div>
</body></html>
"""

_SD_CATALOG = {
    "data": {
        "readdir": [
            {
                "title": "第一卷",
                "miaoshu": "",
                "list": [
                    {
                        "title": "第一章",
                        "url": "/book/4850/1",
                        "id": 1,
                        "class": "",
                    }
                ],
            }
        ]
    }
}


def test_shaoniandream_search_ids_covers_and_limit() -> None:
    searcher = _searcher(ShaoniandreamSearcher)

    rows = searcher._parse_html(_SD_SEARCH)

    assert [r["book_id"] for r in rows] == ["4850", "7523"]
    assert [r["cover_url"] for r in rows] == [
        "https://alioss.example/cover.webp",
        "https://www.shaoniandream.com/img/b.webp",
    ]
    assert rows[0]["latest_chapter"] == "第1章"
    assert [r["book_id"] for r in searcher._parse_html(_SD_SEARCH, limit=1)] == ["4850"]


def test_shaoniandream_book_info_cover_and_blank(cfg: ParserConfig) -> None:
    parser = ShaoniandreamParser(cfg)
    info = parser.parse_book_info([_SD_INFO, json.dumps(_SD_CATALOG)])

    assert info is not None
    assert info["cover_url"] == "https://www.shaoniandream.com/img/cover.webp"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "1"

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["<html></html>", json.dumps(_SD_CATALOG)]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert _searcher(ShaoniandreamSearcher)._parse_html("") == []


# --------------------------------------------------------------------------- #
# ruochu: author must come from the author link, not the disclaimer
# --------------------------------------------------------------------------- #
_RUOCHU_INFO = """
<html><body>
<div class="pattern-cover-detail"><h1><span>神敌</span></h1></div>
<div class="notify"><i class="icon-cover">《神敌》为网站作者“喜了”所著虚构作品，
不涉及任何真实人物、事件等。</i></div>
<div class="cover-breadcrumb">
  <span class="name">神敌</span>
  作者：<a href="http://www.ruochu.com/people/1" class="author">喜了</a>
  <span class="divide">|</span>最新章节：<a class="chapter-name">第2章</a>
</div>
<div class="pic"><img class="book-cover" src="/img/fake.jpg" /></div>
<span class="words">1353715字</span>
<span class="time">2024-06-27 09:47</span>
<div class="summary"><pre class="note">一段简介。</pre></div>
</body></html>
"""

_RUOCHU_CATALOG = """
<html><body><div class="chapter-list"><ul>
  <li><a href="http://www.ruochu.com/book/143441/11875715?from=list#top">1</a></li>
  <li><a href="http://www.ruochu.com/book/143441/11876112" class="isvip">2</a></li>
  <li><a href="javascript:void(0)">坏链接</a></li>
</ul></div></body></html>
"""

_RUOCHU_CHAPTER = (
    'cb({"chapter":{"title":"1","htmlContent":"<p>第一段</p><p>第二段</p>"}})'
)


def test_ruochu_author_is_not_the_disclaimer(cfg: ParserConfig) -> None:
    info = RuochuParser(cfg).parse_book_info([_RUOCHU_INFO, _RUOCHU_CATALOG])

    assert info is not None
    assert info["book_name"] == "神敌"
    assert info["author"] == "喜了"
    assert info["cover_url"] == "https://www.ruochu.com/img/fake.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["11875715", "11876112"]
    assert chapters[1]["accessible"] is False


def test_ruochu_chapter_and_blank_pages(cfg: ParserConfig) -> None:
    parser = RuochuParser(cfg)
    chapter = parser.parse_chapter_content([_RUOCHU_CHAPTER], "11875715")

    assert chapter is not None
    assert chapter["content"] == "第一段\n第二段"
    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_chapter_content([""], "1") is None


# --------------------------------------------------------------------------- #
# blocked / searcher-less sites: only the blank-body guards are pinned
# --------------------------------------------------------------------------- #
def test_pilibook_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = PilibookParser(cfg)

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["<html></html>", ""]) is None
    assert parser.parse_chapter_content([""], "1") is None


def test_shencou_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = ShencouParser(cfg)

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["<html></html>", ""]) is None
    assert parser.parse_chapter_content([""], "1") is None


# --------------------------------------------------------------------------- #
# n69shuba: the chapter request must use the book id in its Referer
# --------------------------------------------------------------------------- #
def test_n69shuba_chapter_referer_uses_book_id(tmp_path: Any) -> None:
    fetcher = N69shubaFetcher(FetcherConfig(cache_dir=str(tmp_path)))
    seen: dict[str, Any] = {}

    async def fake_fetch(url: str, **kwargs: Any) -> str:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers", {})
        return "<html></html>"

    fetcher.fetch = fake_fetch  # type: ignore[method-assign]
    asyncio.run(fetcher.fetch_chapter_content("88724", "39943182"))

    assert seen["url"] == "https://www.69shuba.com/txt/88724/39943182"
    assert seen["headers"]["Referer"] == "https://www.69shuba.com/book/88724/"
