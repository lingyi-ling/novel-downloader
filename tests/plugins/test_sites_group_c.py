#!/usr/bin/env python3
"""Offline fixtures for the C-group site plugins (2026-11 network probe).

Only :mod:`n8novel` and :mod:`xiguashuwu` answered with a parseable search
page during the probe; the other endpoints were Cloudflare challenges
(``n69shuba``, ``trxs``) or unreachable from this host (``n69hao``,
``n23ddw``, ``shu111``, ``tongrenshe``, ``tongrenquan``, ``yodu``).
The fixtures below are trimmed copies of the markup those sites serve, and
pin the defects found while following a search hit into a download:

* ids derived from the raw ``href`` absorbed absolute URLs, query strings and
  fragments (``n23ddw`` produced ``https:--host-du-...``; ``n8novel`` used
  ``href.split("?")[-1]``, which returns the whole URL when no query exists);
* chapter links that are ``javascript:`` handlers or carry no chapter id were
  still emitted as downloadable chapters;
* ``n8novel``/``xiguashuwu`` prefixed covers unconditionally, so an absolute
  ``og:image`` became ``https://sitehttps://cdn/...`` and a missing image
  became the bare origin;
* ``xiguashuwu`` kept the ``(1/3)`` page counter in the chapter title and the
  decrypted pages' ``si m i s h u wu. c o m`` watermark in the text;
* every parser/searcher crashed on a whitespace-only or non-HTML body because
  ``lxml.html.fromstring`` raises instead of returning an empty document.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.sites.n8novel.fetcher import N8novelFetcher
from novel_downloader.plugins.sites.n8novel.parser import N8novelParser
from novel_downloader.plugins.sites.n8novel.searcher import N8novelSearcher
from novel_downloader.plugins.sites.n23ddw.searcher import N23ddwSearcher
from novel_downloader.plugins.sites.n69shuba.parser import N69shubaParser
from novel_downloader.plugins.sites.shu111.parser import Shu111Parser
from novel_downloader.plugins.sites.tongrenquan.parser import TongrenquanParser
from novel_downloader.plugins.sites.tongrenquan.searcher import TongrenquanSearcher
from novel_downloader.plugins.sites.tongrenshe.parser import TongrensheParser
from novel_downloader.plugins.sites.tongrenshe.searcher import TongrensheSearcher
from novel_downloader.plugins.sites.trxs.parser import TrxsParser
from novel_downloader.plugins.sites.trxs.searcher import TrxsSearcher
from novel_downloader.plugins.sites.xiguashuwu.parser import XiguashuwuParser
from novel_downloader.plugins.sites.xiguashuwu.searcher import XiguashuwuSearcher
from novel_downloader.plugins.sites.yodu.parser import YoduParser
from novel_downloader.plugins.sites.yodu.searcher import YoduSearcher
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


def _searcher(cls: type, session: Any = None) -> Any:
    return cls(session if session is not None else object())


# --------------------------------------------------------------------------- #
# n8novel: search ids, book ids and chapter ids
# --------------------------------------------------------------------------- #
_N8_SEARCH = """
<html><body>
<div class="picsize"><a><img src="/pics/broken.jpg" /></a></div>
<div class="picsize"><a href="javascript:void(0)" title="坏">坏</a></div>
<div class="picsize">
  <a href="/novelbooks/286712" title="书A"><eps>1416篇 </eps></a>
</div>
<div class="picsize">
  <a href="https://www.8novel.com/novelbooks/96640/?from=list#top"
     title="书B"><eps>296篇 </eps></a>
</div>
<div class="picsize">
  <a href="//www.8novel.com/novelbooks/10402/" title="书C"></a>
</div>
</body></html>
"""


def test_n8novel_search_ids_ignore_scheme_query_and_fragment() -> None:
    searcher = _searcher(N8novelSearcher)

    rows = searcher._parse_html(_N8_SEARCH)

    assert [r["book_id"] for r in rows] == ["286712", "96640", "10402"]
    assert [r["priority"] for r in rows] == [
        N8novelSearcher.priority,
        N8novelSearcher.priority + 1,
        N8novelSearcher.priority + 2,
    ]
    assert rows[0]["book_url"] == "https://www.8novel.com/novelbooks/286712"
    assert rows[1]["book_url"].startswith("https://www.8novel.com/")
    assert rows[2]["word_count"] == "-"


def test_n8novel_search_limit_counts_valid_rows() -> None:
    searcher = _searcher(N8novelSearcher)

    rows = searcher._parse_html(_N8_SEARCH, limit=1)

    # The two broken cards must not consume the caller's limit.
    assert [r["book_id"] for r in rows] == ["286712"]
    assert rows[0]["priority"] == N8novelSearcher.priority


_N8_BOOK = """
<html><body>
<li class="h2">测试书</li>
<span class="item-info-author">作者: 测试作者</span>
<div class="item-cover"><img src="https://cdn.example/cover.jpg" /></div>
<span class="item-info-date">更新: 2026-01-02</span>
<li class="small text-gray">
  <span class="item-info-num">1</span>
  <span class="item-info-num">23</span>
</li>
<li class="full_text mt-2"><p>一段简介。</p></li>
<div class="folder" pid="1">
  <div class="vol-title"><h3>第一卷 / 12</h3></div>
  <a class="episode_li d-block" href="/read/3355/?270015#top">第一章</a>
  <a class="episode_li d-block"
     href="https://article.8novel.com/read/3355/270016.html?x=1">第二章</a>
  <a class="episode_li d-block" href="/read/3355/">没有章节号</a>
  <a class="episode_li d-block" href="javascript:void(0)">坏链接</a>
</div>
</body></html>
"""

_N8_BOOK_RELATIVE_COVER = _N8_BOOK.replace(
    "https://cdn.example/cover.jpg", "/pics/0/286712-arxt.jpg"
)
_N8_BOOK_NO_COVER = _N8_BOOK.replace(
    '<div class="item-cover"><img src="https://cdn.example/cover.jpg" /></div>',
    '<div class="item-cover"></div>',
)


def test_n8novel_book_info_normalises_cover_and_chapter_ids(
    cfg: ParserConfig,
) -> None:
    info = N8novelParser(cfg).parse_book_info([_N8_BOOK])

    assert info is not None
    # An absolute og:image must not be prefixed with the site origin.
    assert info["cover_url"] == "https://cdn.example/cover.jpg"
    assert info["word_count"] == "23萬字"
    assert info["volumes"][0]["volume_name"] == "第一卷"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["270015", "270016"]
    assert chapters[0]["url"] == "https://www.8novel.com/read/3355/?270015#top"
    assert chapters[1]["url"] == (
        "https://article.8novel.com/read/3355/270016.html?x=1"
    )


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (_N8_BOOK_RELATIVE_COVER, "https://www.8novel.com/pics/0/286712-arxt.jpg"),
        (_N8_BOOK_NO_COVER, ""),
    ],
)
def test_n8novel_cover_is_absolute_or_empty(
    cfg: ParserConfig, page: str, expected: str
) -> None:
    info = N8novelParser(cfg).parse_book_info([page])

    assert info is not None
    assert info["cover_url"] == expected


def test_n8novel_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = N8novelParser(cfg)

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None
    assert parser.parse_book_info(["{not html}"]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert parser.parse_chapter_content(["<html></html>", ""], "1") is None


def test_n8novel_fetcher_tolerates_non_numeric_chapter_id() -> None:
    url = N8novelFetcher._build_chapter_content_url(
        seed="0123456789", book_id="3355", chapter_id="abc", txt_dir="1"
    )
    assert "/txt/1/3355/abc" in url

    numeric = N8novelFetcher._build_chapter_content_url(
        seed="0123456789", book_id="3355", chapter_id="4887137", txt_dir="1"
    )
    assert "/txt/1/3355/4887137" in numeric


# --------------------------------------------------------------------------- #
# n23ddw: book ids from /du/<parent>/<book>/
# --------------------------------------------------------------------------- #
_N23DDW_SEARCH = """
<html><body><div id="hotcontent">
  <div class="item">
    <dl><dt><a href="/du/291/291325/">书A</a></dt></dl>
    <div class="btm"><a>作者A</a></div>
  </div>
  <div class="item">
    <dl><dt><a href="https://www.23ddw.net/du/292/292326/?from=list#top">
      书B</a></dt></dl>
    <div class="btm"><a>作者B</a></div>
  </div>
  <div class="item">
    <dl><dt><a href="//www.23ddw.net/du/293/293327/">书C</a></dt></dl>
    <div class="btm"><a>作者C</a></div>
  </div>
  <div class="item"><dl><dt><a href="javascript:void(0)">坏</a></dt></dl></div>
  <div class="item"><dl><dt><a>没有链接</a></dt></dl></div>
</div></body></html>
"""


def test_n23ddw_search_ids_from_relative_and_absolute_links() -> None:
    searcher = _searcher(N23ddwSearcher)

    rows = searcher._parse_html(_N23DDW_SEARCH)

    assert [r["book_id"] for r in rows] == [
        "291-291325",
        "292-292326",
        "293-293327",
    ]
    assert rows[0]["book_url"] == "https://www.23ddw.net/du/291/291325/"
    assert [r["priority"] for r in rows] == [
        N23ddwSearcher.priority,
        N23ddwSearcher.priority + 1,
        N23ddwSearcher.priority + 2,
    ]


def test_n23ddw_search_limit_counts_valid_rows() -> None:
    searcher = _searcher(N23ddwSearcher)

    rows = searcher._parse_html(_N23DDW_SEARCH, limit=2)

    assert [r["book_id"] for r in rows] == ["291-291325", "292-292326"]


# --------------------------------------------------------------------------- #
# n69shuba: chapter ids and blank-page guards
# --------------------------------------------------------------------------- #
_N69SHUBA_INFO = """
<html><body>
<div class="booknav2">
  <h1><a>测试书</a></h1>
  <p>作者：<a>作者A</a></p>
  <p>123.45万字 | 连载</p>
  <p>更新：2026-01-01</p>
</div>
<div class="bookimg2"><img src="/cover.jpg" /></div>
<div class="navtxt"><p>一段简介。</p></div>
</body></html>
"""

_N69SHUBA_CATALOG = """
<html><body><div id="catalog"><ul>
  <li><a href="/txt/9/12345">第一章</a></li>
  <li><a href="/txt/9/12346?from=list#top">第二章</a></li>
  <li><a href="javascript:void(0)">坏链接</a></li>
  <li><a href="">空链接</a></li>
</ul></div></body></html>
"""


def test_n69shuba_chapter_ids_clean_and_reversed(cfg: ParserConfig) -> None:
    info = N69shubaParser(cfg).parse_book_info([_N69SHUBA_INFO, _N69SHUBA_CATALOG])

    assert info is not None
    assert info["book_name"] == "测试书"
    assert info["word_count"] == "123.45万字"
    chapters = info["volumes"][0]["chapters"]
    # The catalogue is newest-first, so the parser reverses it.
    assert [c["chapterId"] for c in chapters] == ["12346", "12345"]
    assert [c["title"] for c in chapters] == ["第二章", "第一章"]


def test_n69shuba_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = N69shubaParser(cfg)

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["<html></html>", "<html></html>"]) is None
    assert parser.parse_chapter_content([""], "1") is None


# --------------------------------------------------------------------------- #
# shu111: chapter ids from <dd> anchors
# --------------------------------------------------------------------------- #
_SHU111_INFO = """
<html><head>
<meta property="og:novel:book_name" content="测试书" />
<meta property="og:novel:author" content="作者A" />
</head><body>
<div id="list-chapterAll">
  <dd><a href="/book/1/2.html?from=list#top">第一章</a></dd>
  <dd><a href="https://www.shu111.com/book/1/3.html">第二章</a></dd>
  <dd><a href="javascript:;">坏链接</a></dd>
  <dd><a href="">空链接</a></dd>
</div>
</body></html>
"""


def test_shu111_chapter_ids_ignore_query_and_fragment(cfg: ParserConfig) -> None:
    info = Shu111Parser(cfg).parse_book_info([_SHU111_INFO])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["2", "3"]
    assert chapters[0]["url"].startswith("http://www.shu111.com/book/1/2.html")
    assert chapters[1]["url"] == "https://www.shu111.com/book/1/3.html"


def test_shu111_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = Shu111Parser(cfg)

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None
    assert parser.parse_chapter_content([""], "1") is None


# --------------------------------------------------------------------------- #
# trxs / tongrenshe / tongrenquan: shared template
# --------------------------------------------------------------------------- #
_TRXS_BOOK = """
<html><body>
<div class="infos"><h1>测试书</h1><p>一段简介。</p></div>
<div class="date">
  <span><a>作者A</a></span>日期：2026-01-01
</div>
<div class="pic"><img src="/cover.jpg" /></div>
<div class="book_list"><ul>
  <li><a href="/tongren/9302/12345.html?from=list#top">第一章</a></li>
  <li><a href="https://www.trxs.cc/tongren/9302/12346.html">第二章</a></li>
  <li><a href="javascript:void(0)">坏链接</a></li>
  <li><a href="">空链接</a></li>
</ul></div>
</body></html>
"""

_TRXS_SEARCH = """
<html><body><div class="books m-cols">
  <div class="bk">
    <div class="pic"><a href="/tongren/9302.html"><img src="/i.jpg" /></a></div>
    <div class="bk_right">
      <h3><a href="/tongren/9302.html">书A</a></h3>
      <div class="booknews">作者：作者A<label class="date">2026-11-01</label></div>
    </div>
  </div>
  <div class="bk">
    <div class="bk_right">
      <h3><a href="https://www.trxs.cc/tongren/9303.html?from=list#top">
        书B</a></h3>
      <div class="booknews">作者：作者B</div>
    </div>
  </div>
  <div class="bk"><h3><a href="javascript:void(0)">坏链接</a></h3></div>
  <div class="bk"><h3><a>没有链接</a></h3></div>
</div></body></html>
"""

_TRXS_FAMILY_PARSERS = [TrxsParser, TongrensheParser, TongrenquanParser]
_TRXS_FAMILY_SEARCHERS = [TrxsSearcher, TongrensheSearcher, TongrenquanSearcher]


@pytest.mark.parametrize(
    "parser_cls",
    _TRXS_FAMILY_PARSERS,
    ids=["trxs", "tongrenshe", "tongrenquan"],
)
def test_trxs_family_chapter_ids_and_blank_pages(
    cfg: ParserConfig, parser_cls: type
) -> None:
    parser = parser_cls(cfg)
    info = parser.parse_book_info([_TRXS_BOOK])

    assert info is not None
    assert info["book_name"] == "测试书"
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == [
        "12345",
        "12346",
    ]
    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["<html></html>"]) is None
    assert parser.parse_chapter_content([""], "12345") is None


@pytest.mark.parametrize(
    "searcher_cls",
    _TRXS_FAMILY_SEARCHERS,
    ids=["trxs", "tongrenshe", "tongrenquan"],
)
def test_trxs_family_search_ids_and_limit(searcher_cls: type) -> None:
    searcher = _searcher(searcher_cls)

    rows = searcher._parse_html(_TRXS_SEARCH)

    assert [r["book_id"] for r in rows] == ["9302", "9303"]
    assert rows[0]["title"] == "书A"
    assert rows[0]["author"] == "作者A"
    assert rows[0]["update_date"] == "2026-11-01"
    assert [r["priority"] for r in rows] == [
        searcher_cls.priority,
        searcher_cls.priority + 1,
    ]
    assert [r["book_id"] for r in searcher._parse_html(_TRXS_SEARCH, limit=1)] == [
        "9302"
    ]


# --------------------------------------------------------------------------- #
# yodu: book info, chapter ids and search rows
# --------------------------------------------------------------------------- #
_YODU_BOOK = """
<html><body>
<div class="det-info"><h1>测试书</h1></div>
<ol id="chapterList">
  <li class="volumes">第一卷</li>
  <li><a href="/book/17551/28890.html?from=list#top">第一章</a></li>
  <li><a href="javascript:readbook(1);">坏链接</a></li>
  <li><a href="">空链接</a></li>
</ol>
</body></html>
"""

_YODU_CHAPTER = """
<html><body>
<div id="mlfy_main_text"><h1>第一章</h1></div>
<div id="TextContent"><p>一段正文。</p></div>
</body></html>
"""

_YODU_SEARCH = """
<html><body><ul class="ser-ret">
  <li>
    <a class="g_thumb" href="/book/17551/?for-search">
      <img _src="//img.example/c.jpg" /></a>
    <h3><a href="/book/17551/" title="书A">书A</a></h3>
    <em><span>作者：</span><span>作者A</span></em>
    <p>最新章节：<a href="/book/17551/28890.html">第一章</a></p>
  </li>
  <li>
    <a class="g_thumb" href="https://www.yodu.org/book/17552/?x=1#top">
      <img src="//img.example/d.jpg" /></a>
    <h3><a title="书B">书B</a></h3>
  </li>
  <li><a class="g_thumb" href="javascript:void(0)">坏链接</a></li>
  <li><h3><a>没有链接</a></h3></li>
</ul></body></html>
"""


def test_yodu_chapter_ids_skip_javascript_links(cfg: ParserConfig) -> None:
    info = YoduParser(cfg).parse_book_info([_YODU_BOOK])

    assert info is not None
    assert info["volumes"][0]["volume_name"] == "第一卷"
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["28890"]


def test_yodu_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = YoduParser(cfg)

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None
    assert parser.parse_chapter_content([""], "28890") is None
    assert parser.parse_chapter_content(["<html></html>", "   "], "28890") is None


def test_yodu_chapter_content_still_parses(cfg: ParserConfig) -> None:
    chapter = YoduParser(cfg).parse_chapter_content([_YODU_CHAPTER], "28890")

    assert chapter is not None
    assert chapter["title"] == "第一章"
    assert chapter["content"] == "一段正文。"


def test_yodu_search_ids_covers_and_limit() -> None:
    searcher = _searcher(YoduSearcher)

    rows = searcher._parse_html(_YODU_SEARCH)

    assert [r["book_id"] for r in rows] == ["17551", "17552"]
    assert rows[0]["cover_url"] == "https://img.example/c.jpg"
    assert rows[1]["cover_url"] == "https://img.example/d.jpg"
    assert rows[0]["latest_chapter"] == "第一章"
    assert [r["book_id"] for r in searcher._parse_html(_YODU_SEARCH, limit=1)] == [
        "17551"
    ]


# --------------------------------------------------------------------------- #
# xiguashuwu: reachable; book info, chapter title and watermark
# --------------------------------------------------------------------------- #
_XIGUA_BOOK = """
<html><body>
<div class="BGsectionOne-top-left">
  <img _src="/files/article/image/224/224386/224386s.jpg" />
</div>
<p class="title">测试书</p>
<p class="author">作者：<span><a>作者A</a></span></p>
<p class="category">类别：<span><a>其他小说</a></span></p>
<p class="time">更新时间：<span>2026-11-02 04:16:23</span></p>
<section id="intro"><p>一段简介。</p></section>
</body></html>
"""

_XIGUA_BOOK_NO_CATEGORY = _XIGUA_BOOK.replace(
    '<p class="category">类别：<span><a>其他小说</a></span></p>', ""
)

_XIGUA_BOOK_ABSOLUTE_COVER = _XIGUA_BOOK.replace(
    "/files/article/image/224/224386/224386s.jpg",
    "https://cdn.example/224386s.jpg",
)

_XIGUA_CATALOG = """
<html><body>
<section class="BCsectionTwo">
  <h3>正文</h3>
  <ol>
    <li><a href="/book/224386/136649.html?from=list#top">第一章</a></li>
    <li><a href="https://www.xiguashuwu.com/book/224386/136650.html">
      第二章</a></li>
    <li><a href="javascript:;">坏链接</a></li>
  </ol>
</section>
</body></html>
"""

_XIGUA_SEARCH = """
<html><body><div class="SHsectionThree-middle">
  <p>
    <span>[其他小说]</span>
    <span><a href="/book/224386/iszip/0/?from=list#top">书A</a></span>
    / <span><a href="/writer/146830/">作者A</a></span>
  </p>
  <p>
    <span><a href="https://www.xiguashuwu.net/book/224013/">书B</a></span>
    / <span><a href="/writer/142881/">作者B</a></span>
  </p>
  <p><span><a href="/writer/3/">没有书链接</a></span></p>
</div></body></html>
"""

_XIGUA_CHAPTER = """
<html><body>
<h1 id="chapterTitle">第一章 开始 (2/3)</h1>
<div id="C0NTENT">
  <div class="s_m">本站新（短）域名：xiguashuwu.com</div>
  <p>第一段正文。</p>
  <p>si m i s h u wu. c o m</p>
  <p>第二段正文。</p>
</div>
</body></html>
"""


def test_xigua_book_info_normalises_cover_tags_and_ids(
    cfg: ParserConfig,
) -> None:
    info = XiguashuwuParser(cfg).parse_book_info([_XIGUA_BOOK, _XIGUA_CATALOG])

    assert info is not None
    assert info["cover_url"] == (
        "https://www.xiguashuwu.com/files/article/image/224/224386/224386s.jpg"
    )
    assert info["tags"] == ["其他小说"]
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["136649", "136650"]
    assert chapters[0]["url"].startswith(
        "https://www.xiguashuwu.com/book/224386/136649.html"
    )
    assert chapters[1]["url"] == ("https://www.xiguashuwu.com/book/224386/136650.html")


@pytest.mark.parametrize(
    ("page", "expected_cover", "expected_tags"),
    [
        (_XIGUA_BOOK_ABSOLUTE_COVER, "https://cdn.example/224386s.jpg", ["其他小说"]),
        (
            _XIGUA_BOOK_NO_CATEGORY,
            ("https://www.xiguashuwu.com/files/article/image/224/224386/224386s.jpg"),
            [],
        ),
    ],
)
def test_xigua_cover_and_tags_are_clean(
    cfg: ParserConfig,
    page: str,
    expected_cover: str,
    expected_tags: list[str],
) -> None:
    info = XiguashuwuParser(cfg).parse_book_info([page, _XIGUA_CATALOG])

    assert info is not None
    assert info["cover_url"] == expected_cover
    assert info["tags"] == expected_tags


def test_xigua_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = XiguashuwuParser(cfg)

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["<html></html>", "<html></html>"]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert parser.parse_chapter_content(["<html></html>", "  "], "1") is None


def test_xigua_chapter_strips_page_marker_and_watermark(
    cfg: ParserConfig,
) -> None:
    chapter = XiguashuwuParser(cfg).parse_chapter_content([_XIGUA_CHAPTER], "1")

    assert chapter is not None
    assert chapter["title"] == "第一章 开始"
    assert chapter["content"] == "第一段正文。\n第二段正文。"
    assert "si m i s h u wu" not in chapter["content"]


def test_xigua_search_ids_authors_and_limit() -> None:
    searcher = _searcher(XiguashuwuSearcher)

    rows = searcher._parse_html(_XIGUA_SEARCH)

    assert [r["book_id"] for r in rows] == ["224386", "224013"]
    assert [r["title"] for r in rows] == ["书A", "书B"]
    assert [r["author"] for r in rows] == ["作者A", "作者B"]
    assert [r["book_id"] for r in searcher._parse_html(_XIGUA_SEARCH, limit=1)] == [
        "224386"
    ]


# --------------------------------------------------------------------------- #
# every searcher must tolerate an empty / non-HTML response body
# --------------------------------------------------------------------------- #
_SEARCHERS = [
    N8novelSearcher,
    N23ddwSearcher,
    XiguashuwuSearcher,
    TrxsSearcher,
    TongrensheSearcher,
    TongrenquanSearcher,
    YoduSearcher,
]


@pytest.mark.parametrize("body", ["", "   ", "<html></html>", '{"error": 1}'])
@pytest.mark.parametrize(
    "searcher_cls",
    _SEARCHERS,
    ids=[cls.__name__ for cls in _SEARCHERS],
)
def test_searchers_tolerate_empty_or_non_html_bodies(
    searcher_cls: type, body: str
) -> None:
    assert _searcher(searcher_cls)._parse_html(body) == []
