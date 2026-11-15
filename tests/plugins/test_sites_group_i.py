#!/usr/bin/env python3
"""Offline fixture tests for the group-I site plugins (rolling slot 7).

The A-H groups left six parsers with no coverage at all (``ciluke``,
``hetushu``, ``mjyhb``, ``n23ddw``, ``shauthor``, ``westnovel_sub``) and eight
more whose group tests only exercised the search row parser
(``biquguo``, ``bxwx9``, ``fsshu``, ``ktshu``, ``lewenn``, ``linovelib``,
``mangg_net``, ``n37yue``).  This file covers those fourteen sites with
trimmed synthetic copies of the markup described by
``docs/supported-sites/site-details``.

Defects pinned here:

* ``ciluke`` / ``ktshu`` / ``n23ddw`` inherited an empty ``BASE_URL`` from the
  shared biquge parsers, so a root-relative ``og:image``/cover stayed relative.
* ``ktshu`` derived the search book id from the raw href, so a query string,
  fragment or absolute URL leaked into the id (``book/47244/?from=list``).
* ``hetushu`` prefixed ``BASE_URL`` to every cover (absolute covers became
  ``https://www.hetushu.comhttps://...``, a missing cover became the site
  root), used ``rstrip("/index.html")`` (a character-set strip, not a suffix
  strip) for the search id, counted broken search rows against ``limit`` and
  let blank/doctype-only search pages raise ``ParserError``.
* ``westnovel_sub`` blindly prepended ``BASE_URL`` to the cover, raised on
  blank/doctype-only book or chapter pages, and skipped search rows whose
  href was absolute instead of channel-relative.
* ``linovelib`` left the cover, volume cover and inline illustration URLs
  relative, kept catalogue anchors without ``href`` (empty chapter ids),
  leaked a query into a suffix-less chapter href, and raised on a
  doctype-only info/volume/chapter page.
* the shared ``ManggNetSearcher`` used ``lxml.html.fromstring`` directly, so a
  blank, whitespace-only or doctype-only body raised instead of returning no
  results for every biquge-family site in this group.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from novel_downloader.plugins.sites.biquguo.parser import BiquguoParser
from novel_downloader.plugins.sites.biquguo.searcher import BiquguoSearcher
from novel_downloader.plugins.sites.bxwx9.parser import Bxwx9Parser
from novel_downloader.plugins.sites.bxwx9.searcher import Bxwx9Searcher
from novel_downloader.plugins.sites.ciluke.parser import CilukeParser
from novel_downloader.plugins.sites.ciluke.searcher import CilukeSearcher
from novel_downloader.plugins.sites.fsshu.parser import FsshuParser
from novel_downloader.plugins.sites.fsshu.searcher import FsshuSearcher
from novel_downloader.plugins.sites.hetushu.parser import HetushuParser
from novel_downloader.plugins.sites.hetushu.searcher import HetushuSearcher
from novel_downloader.plugins.sites.ktshu.parser import KtshuParser
from novel_downloader.plugins.sites.ktshu.searcher import KtshuSearcher
from novel_downloader.plugins.sites.lewenn.parser import LewennParser
from novel_downloader.plugins.sites.linovelib.parser import LinovelibParser
from novel_downloader.plugins.sites.mangg_net.parser import ManggNetParser
from novel_downloader.plugins.sites.mangg_net.searcher import ManggNetSearcher
from novel_downloader.plugins.sites.mjyhb.parser import MjyhbParser
from novel_downloader.plugins.sites.n23ddw.parser import N23ddwParser
from novel_downloader.plugins.sites.n23ddw.searcher import N23ddwSearcher
from novel_downloader.plugins.sites.n37yue.parser import N37yueParser
from novel_downloader.plugins.sites.n37yue.searcher import N37yueSearcher
from novel_downloader.plugins.sites.shauthor.parser import ShauthorParser
from novel_downloader.plugins.sites.westnovel_sub.parser import WestnovelSubParser
from novel_downloader.plugins.sites.westnovel_sub.searcher import (
    WestnovelSubSearcher,
)
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


ALL_PARSERS = [
    BiquguoParser,
    Bxwx9Parser,
    CilukeParser,
    FsshuParser,
    HetushuParser,
    KtshuParser,
    LewennParser,
    LinovelibParser,
    ManggNetParser,
    MjyhbParser,
    N23ddwParser,
    N37yueParser,
    ShauthorParser,
    WestnovelSubParser,
]

ALL_SEARCHERS = [
    BiquguoSearcher,
    Bxwx9Searcher,
    CilukeSearcher,
    FsshuSearcher,
    HetushuSearcher,
    KtshuSearcher,
    ManggNetSearcher,
    N23ddwSearcher,
    N37yueSearcher,
    WestnovelSubSearcher,
]


# --------------------------------------------------------------------------- #
# shared robustness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("parser_cls", ALL_PARSERS, ids=lambda c: c.site_name)
def test_blank_and_doctype_pages_degrade_to_none(
    parser_cls: type[Any], cfg: ParserConfig
) -> None:
    """A blocked/empty response must never escape as ``ParserError``."""
    parser = parser_cls(cfg)

    for payload in ("", "   ", "<!DOCTYPE html>"):
        assert parser.parse_book_info([payload, payload]) is None
        assert parser.parse_chapter_content([payload], "1") is None


@pytest.mark.parametrize("searcher_cls", ALL_SEARCHERS, ids=lambda c: c.site_name)
def test_searchers_tolerate_empty_or_non_html_bodies(
    searcher_cls: type[Any],
) -> None:
    searcher = searcher_cls(None)  # type: ignore[arg-type]

    for payload in ("", "   ", "\n\t", "<!DOCTYPE html>"):
        assert searcher._parse_html(payload) == []


# --------------------------------------------------------------------------- #
# biquge1 family: ciluke / ktshu / biquguo / bxwx9 / fsshu / mangg_net / n37yue
# --------------------------------------------------------------------------- #
BQ1_SITES = [
    (BiquguoParser, "https://www.biquguo.com"),
    (Bxwx9Parser, "https://www.bxwx9.org"),
    (CilukeParser, "https://www.ciluke.com"),
    (FsshuParser, "https://www.fsshu.com"),
    (KtshuParser, "https://www.ktshu.cc"),
    (ManggNetParser, "https://www.mangg.net"),
    (N37yueParser, "https://www.37yue.com"),
]

BQ1_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="凡人修仙传">
  <meta property="og:novel:author" content="忘语">
  <meta property="og:image" content="/img/137_137144.jpg">
  <meta property="og:novel:status" content="连载">
  <meta property="og:novel:category" content="仙侠">
  <meta property="og:description" content="一个普通山村小子。">
</head><body>
  <div class="book_list2">
    <a href="/137_137144/628955328.html?from=toc#top">第一章 山边小村</a>
    <a href="javascript:;">广告位</a>
    <a href="/137_137144/628955329.html">第二章 青牛镇</a>
  </div>
</body></html>
"""

BQ1_CHAPTER_HTML = """
<html><body>
<h1>第一章 山边小村 - 《凡人修仙传》</h1>
<article>
<p>二愣子睁大着双眼。</p>
<p>这是一个小山村。</p>
</article>
</body></html>
"""


@pytest.mark.parametrize(
    ("parser_cls", "base_url"),
    BQ1_SITES,
    ids=[cls.site_name for cls, _ in BQ1_SITES],
)
def test_biquge1_family_cover_is_absolute(
    parser_cls: type[Any], base_url: str, cfg: ParserConfig
) -> None:
    info = parser_cls(cfg).parse_book_info([BQ1_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "凡人修仙传"
    assert info["author"] == "忘语"
    assert info["cover_url"] == f"{base_url}/img/137_137144.jpg"
    assert info["tags"] == ["仙侠"]
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["628955328", "628955329"]


@pytest.mark.parametrize(
    "parser_cls",
    [cls for cls, _ in BQ1_SITES],
    ids=[cls.site_name for cls, _ in BQ1_SITES],
)
def test_biquge1_family_chapter_content(
    parser_cls: type[Any], cfg: ParserConfig
) -> None:
    chapter = parser_cls(cfg).parse_chapter_content([BQ1_CHAPTER_HTML], "628955328")

    assert chapter is not None
    assert chapter["title"] == "第一章 山边小村"
    assert "二愣子睁大着双眼。" in chapter["content"]
    assert "这是一个小山村。" in chapter["content"]


# --------------------------------------------------------------------------- #
# n23ddw (biquge3 template)
# --------------------------------------------------------------------------- #
N23DDW_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="顶点样本">
  <meta property="og:novel:author" content="作者甲">
  <meta property="og:image" content="/img/80892.jpg">
  <meta property="og:novel:status" content="连载">
  <meta property="og:description" content="顶点简介。">
</head><body>
  <div id="list"><dl>
    <dt>最新章节</dt>
    <dd><a href="/du/80/80892/13055110.html">第一章 起点</a></dd>
    <dt>目录章节</dt>
    <a href="/du/80/80892/13055110.html?from=toc"><dd>第一章 起点</dd></a>
    <a href="/du/80/80892/13055111.html"><dd>第二章 远行</dd></a>
  </dl></div>
</body></html>
"""

N23DDW_CHAPTER_HTML = """
<html><body>
<div class="bookname"><h1>第一章 起点（1/2）</h1></div>
<div id="content"><div id="booktxt">
<p>第一段。</p>
<p>第二段。</p>
</div></div>
</body></html>
"""


def test_n23ddw_book_info_and_chapter(cfg: ParserConfig) -> None:
    info = N23ddwParser(cfg).parse_book_info([N23DDW_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.23ddw.net/img/80892.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["13055110", "13055111"]

    chapter = N23ddwParser(cfg).parse_chapter_content([N23DDW_CHAPTER_HTML], "13055110")
    assert chapter is not None
    assert chapter["title"] == "第一章 起点"
    assert chapter["content"] == "第一段。\n第二段。"


N23DDW_SEARCH_HTML = """
<html><body><div id="hotcontent">
  <div class="item"><dl><dt><a>没有链接的行</a></dt></dl></div>
  <div class="item">
    <div class="image"><img data-original="/img/80892.jpg"></div>
    <dl><dt>
      <a href="https://www.23ddw.net/du/80/80892/?from=search">顶点样本</a>
    </dt></dl>
    <div class="btm">
      <a>作者甲</a>
      <em class="orange">120万字</em>
      <em class="blue">2026-01-02</em>
    </div>
  </div>
</div></body></html>
"""


def test_n23ddw_search_ids_ignore_query_and_limit_counts_valid_rows() -> None:
    searcher = N23ddwSearcher(None)  # type: ignore[arg-type]

    rows = searcher._parse_html(N23DDW_SEARCH_HTML, limit=1)

    assert [r["book_id"] for r in rows] == ["80-80892"]
    assert rows[0]["book_url"] == "https://www.23ddw.net/du/80/80892/?from=search"
    assert rows[0]["cover_url"] == "https://www.23ddw.net/img/80892.jpg"
    assert rows[0]["author"] == "作者甲"
    assert rows[0]["priority"] == N23ddwSearcher.priority


# --------------------------------------------------------------------------- #
# mjyhb / shauthor / lewenn: template variants
# --------------------------------------------------------------------------- #
BQ4_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="模板样本">
  <meta property="og:novel:author" content="作者乙">
  <meta property="og:image" content="/img/3119.jpg">
</head><body>
  <div class="info_chapters">
    <ul class="p2">
      <li><a href="/read_3119/62d0d.html?from=toc">第一章 开端</a></li>
      <li><a href="/read_3119/62d0e.html">第二章 发展</a></li>
    </ul>
  </div>
</body></html>
"""

BQ4_CHAPTER_HTML = """
<html><body>
<h1>第一章 开端</h1>
<div id="novelcontent">
<p>第一段。</p>
<p>第二段。</p>
</div>
</body></html>
"""


@pytest.mark.parametrize(
    ("parser_cls", "base_url"),
    [
        (MjyhbParser, "https://m.mjyhb.com"),
        (ShauthorParser, "https://m.shauthor.com"),
    ],
    ids=["mjyhb", "shauthor"],
)
def test_biquge4_sites_cover_ids_and_chapter(
    parser_cls: type[Any], base_url: str, cfg: ParserConfig
) -> None:
    info = parser_cls(cfg).parse_book_info([BQ4_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == f"{base_url}/img/3119.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["62d0d", "62d0e"]

    chapter = parser_cls(cfg).parse_chapter_content([BQ4_CHAPTER_HTML], "62d0d")
    assert chapter is not None
    assert chapter["title"] == "第一章 开端"
    assert chapter["content"] == "第一段。\n第二段。"


LEWENN_INFO_HTML = """
<html><body>
<div id="info">
  <h1>乐文样本</h1>
  <p>作&nbsp;&nbsp;者：作者丙</p>
  <p>状&nbsp;&nbsp;态：连载</p>
  <p>最后更新：2026-01-03</p>
</div>
<div id="sidebar"><img src="/img/lw1.jpg"></div>
<div id="intro"><p>乐文简介。</p></div>
<div class="listmain"><dl>
  <dt>正文</dt>
  <dd><a href="/lw1/30038546.html?from=toc">第一章 初见</a></dd>
</dl></div>
</body></html>
"""

LEWENN_CHAPTER_HTML = """
<html><body>
<div class="content"><h1>第一章 初见</h1></div>
<div id="content">
<p>第一段。</p>
<p>第二段。</p>
</div>
</body></html>
"""


def test_lewenn_book_info_and_chapter(cfg: ParserConfig) -> None:
    info = LewennParser(cfg).parse_book_info([LEWENN_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "乐文样本"
    assert info["author"] == "作者丙"
    assert info["cover_url"] == "https://www.lewenn.net/img/lw1.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["30038546"]
    assert chapters[0]["url"] == "https://www.lewenn.net/lw1/30038546.html?from=toc"

    chapter = LewennParser(cfg).parse_chapter_content([LEWENN_CHAPTER_HTML], "30038546")
    assert chapter is not None
    assert chapter["title"] == "第一章 初见"
    assert chapter["content"] == "第一段。\n第二段。"


# --------------------------------------------------------------------------- #
# hetushu
# --------------------------------------------------------------------------- #
HETUSHU_INFO_HTML = """
<html><body>
<div class="book_info finish">
  <h2>和图书样本</h2>
  <div><span>作者：</span><a>作者丁</a></div>
  <img src="/img/7631.jpg">
</div>
<dl class="tag"><dd><a>玄幻</a></dd><dd><a> </a></dd></dl>
<div class="intro"><p>和图书简介。</p></div>
</body></html>
"""

HETUSHU_CATALOG = json.dumps(
    [
        ["dt", "第一卷"],
        ["dd", "第一章 初见", "1001"],
        ["dd", "第二章 远行", "1002"],
        ["dt", "第二卷"],
        ["dd", "第三章 归途", "1003"],
        ["dd", "坏行"],
    ]
)

HETUSHU_CHAPTER_HTML = """
<html><body>
<div id="content">
  <div>卷首语。</div>
  <h2 class="h2">第一章 初见</h2>
  <div>第一段。</div>
  <div>第二段。</div>
  <div>第三段。</div>
</div>
</body></html>
"""


def test_hetushu_book_info(cfg: ParserConfig) -> None:
    info = HetushuParser(cfg).parse_book_info([HETUSHU_INFO_HTML, HETUSHU_CATALOG])

    assert info is not None
    assert info["book_name"] == "和图书样本"
    assert info["author"] == "作者丁"
    assert info["cover_url"] == "https://www.hetushu.com/img/7631.jpg"
    assert info["serial_status"] == "已完结"
    assert info["tags"] == ["玄幻"]
    assert info["summary"] == "和图书简介。"
    assert [v["volume_name"] for v in info["volumes"]] == ["第一卷", "第二卷"]
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == [
        "1001",
        "1002",
    ]
    assert [c["chapterId"] for c in info["volumes"][1]["chapters"]] == ["1003"]


@pytest.mark.parametrize(
    ("cover", "expected"),
    [
        ("/img/7631.jpg", "https://www.hetushu.com/img/7631.jpg"),
        ("//cdn.example.com/c.jpg", "https://cdn.example.com/c.jpg"),
        ("https://cdn.example.com/c.jpg", "https://cdn.example.com/c.jpg"),
        (None, ""),
    ],
)
def test_hetushu_cover_is_resolved_once(
    cover: str | None, expected: str, cfg: ParserConfig
) -> None:
    html_str = HETUSHU_INFO_HTML
    if cover is None:
        html_str = html_str.replace('<img src="/img/7631.jpg">', "")
    else:
        html_str = html_str.replace("/img/7631.jpg", cover)

    info = HetushuParser(cfg).parse_book_info([html_str, HETUSHU_CATALOG])

    assert info is not None
    assert info["cover_url"] == expected


def test_hetushu_chapter_reorders_and_keeps_volume_intro(cfg: ParserConfig) -> None:
    order = base64.b64encode(b"1AB%0CD%2").decode()

    chapter = HetushuParser(cfg).parse_chapter_content(
        [HETUSHU_CHAPTER_HTML, order], "1001"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 初见"
    assert chapter["content"] == "第二段。\n第一段。\n第三段。"
    assert chapter["extra"]["vol_intro"] == "卷首语。"


HETUSHU_SEARCH_HTML = """
<html><body><dl class="list" id="body">
  <dd><h4><a>没有链接的行</a></h4></dd>
  <dd>
    <h4>
      <a href="/book/7631/index.html?from=search#hit">和图书样本</a>
      <span> / 作者丁 / </span>
    </h4>
    <a href="/book/7631/index.html"><img src="/img/7631.jpg"></a>
  </dd>
</dl></body></html>
"""


def test_hetushu_search_id_and_limit() -> None:
    searcher = HetushuSearcher(None)  # type: ignore[arg-type]

    rows = searcher._parse_html(HETUSHU_SEARCH_HTML, limit=1)

    assert [r["book_id"] for r in rows] == ["7631"]
    assert rows[0]["author"] == "作者丁"
    assert rows[0]["cover_url"] == "https://www.hetushu.com/img/7631.jpg"
    assert rows[0]["priority"] == HetushuSearcher.priority


# --------------------------------------------------------------------------- #
# westnovel_sub
# --------------------------------------------------------------------------- #
WESTNOVEL_INFO_HTML = """
<html><body>
<div id="bookinfo"><h1>西方奇幻样本</h1></div>
<div id="count"><ul><li><strong>作者：</strong><a>作者戊</a></li></ul></div>
<div id="bookimg"><img src="/q/images/725.jpg"></div>
<div id="bookintro">西方奇幻简介。</div>
<ul id="chapterList">
  <li><a href="/q/showinfo-2-40238-0.html?from=toc">第一章 序章</a></li>
  <li><a href="/q/showinfo-2-40239-0.html">第二章 启程</a></li>
</ul>
</body></html>
"""

WESTNOVEL_CHAPTER_HTML = """
<html><body>
<div id="mlfy_main_text"><h1>第一章 序章</h1></div>
<div id="TextContent"><p>第一段。</p><p>第二段。</p></div>
</body></html>
"""


def test_westnovel_sub_book_info_and_chapter(cfg: ParserConfig) -> None:
    info = WestnovelSubParser(cfg).parse_book_info([WESTNOVEL_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "西方奇幻样本"
    assert info["author"] == "作者戊"
    assert info["cover_url"] == "https://www.westnovel.com/q/images/725.jpg"
    assert info["summary"] == "西方奇幻简介。"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["2-40238-0", "2-40239-0"]

    chapter = WestnovelSubParser(cfg).parse_chapter_content(
        [WESTNOVEL_CHAPTER_HTML], "2-40238-0"
    )
    assert chapter is not None
    assert chapter["title"] == "第一章 序章"
    assert chapter["content"] == "第一段。\n第二段。"


WESTNOVEL_SEARCH_HTML = """
<html><body><div id="sitebox">
  <dl>
    <dt><img src="/q/images/1.jpg"></dt>
    <dd><h3><a href="/q/list/1.html">其他作品</a></h3></dd>
  </dl>
  <dl>
    <dt><img src="/q/images/725.jpg"></dt>
    <dd><h3>
      <a href="https://www.westnovel.com/q/list/725.html?from=list#hit">
        西方奇幻样本
      </a>
    </h3></dd>
  </dl>
</div></body></html>
"""


def test_westnovel_sub_search_absolute_href_and_limit() -> None:
    searcher = WestnovelSubSearcher(None)  # type: ignore[arg-type]
    searcher._query = "西方奇幻"

    rows = searcher._parse_html(WESTNOVEL_SEARCH_HTML, limit=1)

    assert [r["book_id"] for r in rows] == ["q-list-725"]
    assert rows[0]["book_url"] == (
        "https://www.westnovel.com/q/list/725.html?from=list#hit"
    )
    assert rows[0]["cover_url"] == "https://www.westnovel.com/q/images/725.jpg"
    assert rows[0]["priority"] == WestnovelSubSearcher.priority


# --------------------------------------------------------------------------- #
# linovelib
# --------------------------------------------------------------------------- #
LINOVELIB_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="哔哩样本">
  <meta property="og:novel:author" content="作者己">
  <meta property="og:image" content="/img/cover2727.jpg">
  <meta property="og:description" content="哔哩简介。">
</head><body></body></html>
"""

LINOVELIB_VOLUME_HTML = """
<html><head>
  <meta property="og:title" content="哔哩样本 第一卷">
  <meta property="og:image" content="/img/vol296755.jpg">
</head><body>
<div class="book-new-chapter">
  <a href="/novel/2727/296757.html?from=toc">第一章 初见</a>
  <a>没有链接的行</a>
  <a href="/novel/2727/296758.html">第二章 远行</a>
</div>
</body></html>
"""

LINOVELIB_CHAPTER_HTML = """
<html><body>
<div id="mlfy_main_text"><h1>第一章 初见</h1></div>
<div id="TextContent">
  <p>第一段。</p>
  <img src="/img/pic001.jpg">
  <p>第二段。</p>
  <img src="//cdn.example.com/pic002.jpg">
</div>
</body></html>
"""


def test_linovelib_book_info_resolves_cover_and_ids(cfg: ParserConfig) -> None:
    info = LinovelibParser(cfg).parse_book_info(
        [LINOVELIB_INFO_HTML, LINOVELIB_VOLUME_HTML]
    )

    assert info is not None
    assert info["book_name"] == "哔哩样本"
    assert info["cover_url"] == "https://www.linovelib.com/img/cover2727.jpg"
    assert len(info["volumes"]) == 1
    volume = info["volumes"][0]
    assert volume["volume_name"] == "第一卷"
    assert volume["volume_cover"] == "https://www.linovelib.com/img/vol296755.jpg"
    assert [c["chapterId"] for c in volume["chapters"]] == ["296757", "296758"]
    assert volume["chapters"][0]["url"] == "/novel/2727/296757.html?from=toc"


def test_linovelib_chapter_resolves_inline_images(cfg: ParserConfig) -> None:
    chapter = LinovelibParser(cfg).parse_chapter_content(
        [LINOVELIB_CHAPTER_HTML], "296757"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 初见"
    assert chapter["content"] == "第一段。\n第二段。"
    assert [r["url"] for r in chapter["extra"]["resources"]] == [
        "https://www.linovelib.com/img/pic001.jpg",
        "https://cdn.example.com/pic002.jpg",
    ]


def test_linovelib_skips_doctype_only_volume_page(cfg: ParserConfig) -> None:
    assert (
        LinovelibParser(cfg).parse_book_info([LINOVELIB_INFO_HTML, "<!DOCTYPE html>"])
        is None
    )


# --------------------------------------------------------------------------- #
# biquge-family search ids (ManggNetSearcher template)
# --------------------------------------------------------------------------- #
MANGG_SEARCH_HTML = """
<html><body><div class="row">
  <div class="col-12 col-md-6">
    <dl>
      <dt><a><img src="/images/broken.jpg"></a></dt>
      <dd><h3><a>没有链接的行</a></h3></dd>
    </dl>
  </div>
  <div class="col-12 col-md-6">
    <dl>
      <dt><a href="{href}"><img src="/images/cover.jpg"></a></dt>
      <dd><h3><a href="{href}">搜索样本</a></h3></dd>
      <dd class="book_other">作者：<span>作者庚</span></dd>
      <dd class="book_other">更新时间：2026-01-04</dd>
      <dd class="book_other">最新章节：<a href="/1/2/3.html">第一章</a></dd>
    </dl>
  </div>
</div></body></html>
"""

MANGG_SEARCH_CASES = [
    (CilukeSearcher, "/19/19747/?from=search#top", "19-19747"),
    (BiquguoSearcher, "https://www.biquguo.com/0/352/#comments", "0-352"),
    (Bxwx9Searcher, "//www.bxwx9.org/b/48/48453/?from=list", "48-48453"),
    (FsshuSearcher, "https://www.fsshu.com/biquge/0_139/?from=list", "0_139"),
    (KtshuSearcher, "https://www.ktshu.cc/book/47244/?from=list#top", "47244"),
    (ManggNetSearcher, "/id26581/?from=list", "id26581"),
    (N37yueSearcher, "//www.37yue.com/0/180/", "0-180"),
]


@pytest.mark.parametrize(
    ("searcher_cls", "href", "expected"),
    MANGG_SEARCH_CASES,
    ids=[cls.site_name for cls, _, _ in MANGG_SEARCH_CASES],
)
def test_biquge_family_search_ids_ignore_query_and_broken_rows(
    searcher_cls: type[Any], href: str, expected: str
) -> None:
    searcher = searcher_cls(None)  # type: ignore[arg-type]

    rows = searcher._parse_html(MANGG_SEARCH_HTML.format(href=href), limit=1)

    assert [r["book_id"] for r in rows] == [expected]
    assert rows[0]["title"] == "搜索样本"
    assert rows[0]["author"] == "作者庚"
    expected_base = searcher_cls.BASE_URL.rstrip("/")
    assert rows[0]["cover_url"] == f"{expected_base}/images/cover.jpg"
    assert rows[0]["priority"] == searcher_cls.priority


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/book/62773/", "62773"),
        ("/book/62773.html", "62773"),
        ("https://www.ktshu.cc/book/62773/?from=list#top", "62773"),
        ("//www.ktshu.cc/book/62773/", "62773"),
        ("", ""),
    ],
)
def test_ktshu_search_id_is_path_only(href: str, expected: str) -> None:
    assert KtshuSearcher._url_to_id(href) == expected
