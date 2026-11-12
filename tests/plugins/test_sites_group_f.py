#!/usr/bin/env python3
"""Offline fixture tests for the group-F site plugins (rolling slot 4).

The fixtures are trimmed copies of the markup/JSON observed during the
2026-11 network probe.  They pin the defects found while following a search
hit through to a download:

* ``i25zw`` returned relative ``book_url`` values and let ``?from=`` bytes
  leak into the book id; its parser also raised ``IndexError`` when the
  catalogue page had no ``#list/dl`` and ``ParserError`` on an empty body.
* ``ixdzs8`` derived ids from the raw ``data-url`` / ``og:read_url`` value.
* ``jpxs123`` served GBK pages with no charset header, so the base fetcher
  decoded every title as UTF-8 (mojibake); the search id split an absolute
  href on ``.`` and the download link was prefixed unconditionally.
* ``kadokado`` called ``json.loads`` on whatever the API returned, so a WAF
  HTML body or an empty page raised instead of degrading to ``None``.
* ``kunnu`` indexed ``book-intro``/``book-describe`` without checking and
  kept a relative cover.
* ``linovel`` had a wrong ``BASE_URL`` (lnovel.org), a non-absolutized cover
  and an id taken from the raw href.
* ``laoyaoxs`` built a path-shaped ``chapterId`` from ``/list/7449/1.html``.
* ``ktshu`` is behind a Cloudflare 521; only the blank-body guard is local.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from novel_downloader.plugins.sites.i25zw.parser import I25zwParser
from novel_downloader.plugins.sites.i25zw.searcher import I25zwSearcher
from novel_downloader.plugins.sites.ixdzs8.parser import Ixdzs8Parser
from novel_downloader.plugins.sites.ixdzs8.searcher import Ixdzs8Searcher
from novel_downloader.plugins.sites.jpxs123.fetcher import Jpxs123Fetcher
from novel_downloader.plugins.sites.jpxs123.parser import Jpxs123Parser
from novel_downloader.plugins.sites.jpxs123.searcher import Jpxs123Searcher
from novel_downloader.plugins.sites.kadokado.parser import KadokadoParser
from novel_downloader.plugins.sites.kadokado.searcher import KadokadoSearcher
from novel_downloader.plugins.sites.ktshu.parser import KtshuParser
from novel_downloader.plugins.sites.kunnu.parser import KunnuParser
from novel_downloader.plugins.sites.kunnu.searcher import KunnuSearcher
from novel_downloader.plugins.sites.laoyaoxs.parser import LaoyaoxsParser
from novel_downloader.plugins.sites.laoyaoxs.searcher import LaoyaoxsSearcher
from novel_downloader.plugins.sites.lewenn.parser import LewennParser
from novel_downloader.plugins.sites.linovel.parser import LinovelParser
from novel_downloader.plugins.sites.linovel.searcher import LinovelSearcher
from novel_downloader.plugins.sites.linovelib.parser import LinovelibParser
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


def _parse(cls: type[Any], html_str: str, limit: int | None = None) -> list[Any]:
    # ``_parse_html`` never touches ``self.session``; skip constructing an
    # aiohttp session so the test stays offline.
    return cls(None)._parse_html(html_str, limit)


# --------------------------------------------------------------------------- #
# i25zw
# --------------------------------------------------------------------------- #
I25ZW_SEARCH_HTML = """
<html><body>
  <div id="alistbox">
    <div class="pic"><a
      href="https://www.i25zw.com/book/309209.html?from=search#top">
      <img src="/upload/309209.jpg"></a></div>
    <div class="title"><h2><a
      href="https://www.i25zw.com/book/309209.html?from=search#top">凡人修仙传</a></h2>
      <span>作者：忘语</span></div>
    <div class="sys"><ul><li><a href="/309209/5953296.html">第一章</a></li></ul>
    </div>
  </div>
</body></html>
"""

I25ZW_INFO_HTML = """
<html><body>
  <div class="pic"><img src="/upload/cover.jpg"></div>
  <h1 class="f21h">测试书<em>作者:<a href="/author/x.html">忘语</a></em></h1>
  <table>
    <tr><td><b>小说分类：</b>玄幻 </td></tr>
    <tr><td><b>小说状态：</b>连载中</td></tr>
    <tr><td><b>全文字数：</b>100万字</td></tr>
    <tr><td><b>更新时间：</b>(2026-01-01 00:00:00)</td></tr>
  </table>
  <div class="intro" style="text-indent:2em;">关于测试书：简介。</div>
</body></html>
"""

I25ZW_CATALOG_HTML = """
<html><body>
  <div id="list"><dl>
    <dt>正文</dt>
    <dd><a href="/118262/181292519.html?from=catalog">第1章</a></dd>
    <dd><a href="/118262/181292520.html">第2章</a></dd>
  </dl></div>
</body></html>
"""

I25ZW_CHAPTER_HTML = """
<html><body>
  <div class="zhangjieming"><h1>第1章</h1></div>
  <div id="content"><p>第一段</p><p>  </p><p>第二段</p></div>
</body></html>
"""


def test_i25zw_search_absolutizes_and_cleans_id() -> None:
    result = _parse(I25zwSearcher, I25ZW_SEARCH_HTML)[0]

    assert result["book_id"] == "309209"
    assert result["book_url"] == (
        "https://www.i25zw.com/book/309209.html?from=search#top"
    )
    assert result["cover_url"] == "https://www.i25zw.com/upload/309209.jpg"
    assert result["author"] == "忘语"
    assert result["latest_chapter"] == "第一章"


def test_i25zw_parser_missing_catalog_returns_none(cfg: ParserConfig) -> None:
    parser = I25zwParser(cfg)
    empty_catalog = "<html><body><p>no list</p></body></html>"

    assert parser.parse_book_info([I25ZW_INFO_HTML, empty_catalog]) is None
    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info(["   ", "   "]) is None


def test_i25zw_parser_absolutizes_cover_and_chapter_id(
    cfg: ParserConfig,
) -> None:
    info = I25zwParser(cfg).parse_book_info([I25ZW_INFO_HTML, I25ZW_CATALOG_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.i25zw.com/upload/cover.jpg"
    assert info["book_name"] == "测试书"
    assert info["author"] == "忘语"
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == [
        "181292519",
        "181292520",
    ]


def test_i25zw_chapter_empty_body_returns_none(cfg: ParserConfig) -> None:
    parser = I25zwParser(cfg)

    assert parser.parse_chapter_content([""], "1") is None
    assert parser.parse_chapter_content(["   "], "1") is None
    chapter = parser.parse_chapter_content([I25ZW_CHAPTER_HTML], "1")
    assert chapter is not None
    assert chapter["content"] == "第一段\n第二段"


# --------------------------------------------------------------------------- #
# ixdzs8
# --------------------------------------------------------------------------- #
IXDZS_SEARCH_HTML = """
<html><body>
  <ul class="u-list">
    <li class="burl" data-url="/read/633013/?from=search#hit">
      <div class="l-img"><img src="/images/633013.jpg"></div>
      <h3 class="bname"><a href="/read/633013/" title="测试书">测试书</a></h3>
      <span class="bauthor"><a>作者</a></span><span class="size">1万字</span>
      <p class="l-last"><span class="l-chapter">最新章</span>
        <span class="l-time">2026-01-01</span></p>
    </li>
  </ul>
</body></html>
"""

IXDZS_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="测试书">
  <meta property="og:novel:author" content="作者">
  <meta property="og:novel:read_url"
    content="https://ixdzs8.com/read/633013/?from=share#x">
  <meta property="og:description" content="简介">
</head><body>
  <div class="n-img"><img src="/covers/633013.jpg"></div>
  <div class="n-text"><h1>测试书</h1><span class="nsize">1万字</span></div>
</body></html>
"""

IXDZS_CATALOG_JSON = json.dumps(
    {
        "data": [
            {"ordernum": 1, "title": "第1章"},
            {"ordernum": 2, "title": ""},
        ]
    },
    ensure_ascii=False,
)

IXDZS_CHAPTER_HTML = """
<html><body>
  <div class="page-d-top"><h1>第1章</h1></div>
  <article class="page-content"><section>
    <p>第一段</p><p class="abg">广告</p><p>第二段</p>
  </section></article>
</body></html>
"""


def test_ixdzs8_search_cleans_id_and_absolutizes_urls() -> None:
    result = _parse(Ixdzs8Searcher, IXDZS_SEARCH_HTML)[0]

    assert result["book_id"] == "633013"
    assert result["book_url"] == ("https://ixdzs8.com/read/633013/?from=search#hit")
    assert result["cover_url"] == "https://ixdzs8.com/images/633013.jpg"


def test_ixdzs8_parser_read_url_query_and_relative_cover(
    cfg: ParserConfig,
) -> None:
    info = Ixdzs8Parser(cfg).parse_book_info([IXDZS_INFO_HTML, IXDZS_CATALOG_JSON])

    assert info is not None
    assert info["cover_url"] == "https://ixdzs8.com/covers/633013.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["p1", "p2"]
    assert chapters[0]["url"] == "/read/633013/p1.html"
    assert chapters[1]["title"] == "未命名章节"


def test_ixdzs8_parser_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = Ixdzs8Parser(cfg)

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_chapter_content([""], "p1") is None
    assert parser.parse_chapter_content(["   "], "p1") is None
    chapter = parser.parse_chapter_content([IXDZS_CHAPTER_HTML], "p1")
    assert chapter is not None
    assert chapter["content"] == "第一段\n第二段"


# --------------------------------------------------------------------------- #
# jpxs123
# --------------------------------------------------------------------------- #
JPXS_SEARCH_HTML = """
<html><body>
  <div class="books m-cols">
    <div class="bk">
      <h3><a href="https://www.jpxs123.com/cyjk/11624.html?from=s#x">
        测试书</a></h3>
      <div class="pic"><a><img src="/d/file/p/cover.jpg"></a></div>
      <div class="booknews">作者：无戒僧<label class="date">2026-01-01</label>
      </div>
    </div>
  </div>
</body></html>
"""

JPXS_INFO_HTML = """
<html><body>
  <div class="infos"><h1>测试书</h1><p>简介</p></div>
  <div class="menNav"><a href="/">首页</a><a href="/cyjk/">穿越架空</a></div>
  <div class="date"><span><a>无戒僧</a></span>
    <span>时间：2026-01-01</span></div>
  <div class="pic"><img src="/d/file/p/cover.jpg"></div>
  <div class="book_list"><ul>
    <li><a href="/cyjk/11624/1.html?from=catalog">第1章</a></li>
  </ul></div>
</body></html>
"""

JPXS_DOWNLOAD_HTML = """
<html><body>
  <a id="dowloadnUrl" link="xs../doaction.php?enews=DownSoft&amp;id=11624"
    href="xs../doaction.php?enews=DownSoft&amp;id=11624">txt下载</a>
</body></html>
"""

JPXS_CHAPTER_HTML = """
<html><body>
  <div class="readTop"><a href="/">首页</a><a href="/cyjk/11624/">测试书</a>
  </div>
  <div class="read_chapterName"><h1>测试书 第1章</h1></div>
  <div class="read_chapterDetail"><p>第一段。</p><p>第二段。</p></div>
</body></html>
"""


def test_jpxs_search_id_from_absolute_query_href() -> None:
    result = _parse(Jpxs123Searcher, JPXS_SEARCH_HTML)[0]

    assert result["book_id"] == "cyjk-11624"
    assert result["book_url"] == ("https://www.jpxs123.com/cyjk/11624.html?from=s#x")
    assert result["cover_url"] == ("https://www.jpxs123.com/d/file/p/cover.jpg")
    assert result["author"] == "无戒僧"


def test_jpxs_parser_download_link_and_cover(cfg: ParserConfig) -> None:
    info = Jpxs123Parser(cfg).parse_book_info([JPXS_INFO_HTML, JPXS_DOWNLOAD_HTML])

    assert info is not None
    assert info["cover_url"] == ("https://www.jpxs123.com/d/file/p/cover.jpg")
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "1"
    assert info["extra"]["download_url"] == (
        "https://www.jpxs123.com/e/DownSys/doaction.php?enews=DownSoft&id=11624"
    )


def test_jpxs_parser_does_not_prefix_absolute_download_link() -> None:
    absolute = "https://cdn.example.com/book.txt"

    assert Jpxs123Parser._fix_download_link(absolute) == absolute
    assert Jpxs123Parser._fix_download_link("//cdn.example.com/book.txt") == (
        "https://cdn.example.com/book.txt"
    )
    assert Jpxs123Parser._fix_download_link("xs../doaction.php?id=1") == (
        "https://www.jpxs123.com/e/DownSys/doaction.php?id=1"
    )


def test_jpxs_fetcher_resolves_download_href() -> None:
    assert Jpxs123Fetcher._absolute_url("/txt/17-11624-0.html") == (
        "https://www.jpxs123.com/txt/17-11624-0.html"
    )
    assert Jpxs123Fetcher._absolute_url("https://cdn.example/x") == (
        "https://cdn.example/x"
    )
    assert Jpxs123Fetcher._absolute_url("//cdn.example/x") == ("https://cdn.example/x")


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.ok = True
        self.status = 200
        self.headers: dict[str, str] = {}


class _FakeSession:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._content)


def test_jpxs_fetcher_decodes_gbk_with_stray_byte() -> None:
    """One invalid byte in an ad block must not mojibake the whole page."""
    body = (
        "<title>全球迷宮</title>".encode("gb18030")
        + b"\xfd\x87"
        + ("<p>正文</p>".encode("gb18030"))
    )
    fetcher = Jpxs123Fetcher.__new__(Jpxs123Fetcher)
    fetcher.session = _FakeSession(body)  # type: ignore[assignment]
    fetcher._rate_limiter = None
    fetcher._retry_times = 0

    text = asyncio.run(fetcher._fetch_page("https://www.jpxs123.com/x.html"))

    assert "全球迷宮" in text
    assert "正文" in text
    assert Jpxs123Fetcher.PAGE_ENCODING == "gb18030"


def test_jpxs_parser_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = Jpxs123Parser(cfg)

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None
    assert parser.parse_chapter_content([""], "1") is None
    chapter = parser.parse_chapter_content([JPXS_CHAPTER_HTML], "1")
    assert chapter is not None
    assert chapter["title"] == "第1章"


def test_jpxs_search_blank_html_returns_empty() -> None:
    assert _parse(Jpxs123Searcher, "") == []
    assert _parse(Jpxs123Searcher, "   ") == []


# --------------------------------------------------------------------------- #
# kadokado
# --------------------------------------------------------------------------- #
KADO_INFO_JSON = json.dumps(
    {
        "displayName": "测试书",
        "ownerDisplayName": "作者",
        "logline": "简介",
        "tags": ["奇幻", 123],
        "coverUrls": ["https://img.kadokado.com.tw/cover.jpg"],
        "wordCount": 1000,
    },
    ensure_ascii=False,
)
KADO_CATALOG_JSON = json.dumps(
    [
        {
            "collectionDisplayName": "第一卷",
            "chapters": [
                {
                    "chapterId": 11,
                    "chapterDisplayName": "第1章",
                    "isFree": True,
                },
                {
                    "chapterId": 12,
                    "chapterDisplayName": "第2章",
                    "isPurchased": False,
                },
                {"chapterDisplayName": "无 id 章节"},
            ],
        },
        "not-a-volume",
    ],
    ensure_ascii=False,
)
KADO_CHAPTER_INFO_JSON = json.dumps({"chapterDisplayName": "第1章"}, ensure_ascii=False)
KADO_CHAPTER_CONTENT_JSON = json.dumps(
    {
        "content": '<p>第一段</p><p>第二段</p><img src="//img/x.jpg">',
        "imageUrls": ["//img/y.jpg", 5],
    },
    ensure_ascii=False,
)


def test_kadokado_parser_non_json_returns_none(cfg: ParserConfig) -> None:
    parser = KadokadoParser(cfg)

    for body in ("", "   ", "<html>blocked</html>", "null", "[]"):
        assert parser.parse_book_info([body, body]) is None
        assert parser.parse_chapter_content([body, body], "1") is None


def test_kadokado_parser_book_info(cfg: ParserConfig) -> None:
    info = KadokadoParser(cfg).parse_book_info([KADO_INFO_JSON, KADO_CATALOG_JSON])

    assert info is not None
    assert info["book_name"] == "测试书"
    assert info["tags"] == ["奇幻"]
    assert info["word_count"] == "1000"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["11", "12"]
    assert chapters[1]["accessible"] is False


def test_kadokado_parser_chapter_resources(cfg: ParserConfig) -> None:
    chapter = KadokadoParser(cfg).parse_chapter_content(
        [KADO_CHAPTER_INFO_JSON, KADO_CHAPTER_CONTENT_JSON], "11"
    )

    assert chapter is not None
    assert chapter["content"] == "第一段\n第二段"
    resources = chapter["extra"]["resources"]
    assert [r["url"] for r in resources] == [
        "https://img/x.jpg",
        "https://img/y.jpg",
    ]


# --------------------------------------------------------------------------- #
# kunnu
# --------------------------------------------------------------------------- #
KUNNU_INFO_HTML = """
<html><body>
  <div class="book-intro">
    <div class="book-img"><img src="/images/2014/10/fanren.jpg"></div>
    <div class="book-describe">
      <h1>凡人修仙传</h1>
      <p>作者：忘语</p>
      <p>类型：奇幻修真</p>
      <p>状态：已完结</p>
      <p>最近更新：2015年08月09日</p>
    </div>
  </div>
  <div class="describe-html"><p>一个普通山村小子。</p></div>
  <div id="content-list">
    <div class="title"><h3>正文</h3></div>
    <div class="book-list"><ul>
      <li><a href="/fanren/1.htm" title="第1章">第1章</a></li>
    </ul></div>
  </div>
</body></html>
"""


def test_kunnu_parser_missing_intro_returns_none(cfg: ParserConfig) -> None:
    parser = KunnuParser(cfg)

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None
    assert parser.parse_book_info(["<html><body><h1>x</h1></body></html>"]) is None
    assert parser.parse_chapter_content([""], "1") is None


def test_kunnu_parser_relative_cover_and_metadata(cfg: ParserConfig) -> None:
    info = KunnuParser(cfg).parse_book_info([KUNNU_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == ("https://www.kunnu.com/images/2014/10/fanren.jpg")
    assert info["book_name"] == "凡人修仙传"
    assert info["author"] == "忘语"
    assert info["serial_status"] == "已完结"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "1"


# --------------------------------------------------------------------------- #
# linovel
# --------------------------------------------------------------------------- #
LINOVEL_SEARCH_HTML = """
<html><body>
  <div class="rank-book-list">
    <a href="/book/103712.html?from=search#hit" class="search-book">
      <div class="book-draw">
        <div class="book-cover"><img src="//rin.linovel.net/c.jpg"></div>
        <div class="book-box"><div class="book-info">
          <div class="book-name">测试书</div>
          <div class="book-extra">作者名 丨 2026-01-01</div>
        </div></div>
      </div>
    </a>
  </div>
</body></html>
"""

LINOVEL_INFO_HTML = """
<html><head>
  <meta property="og:title" content="测试书">
  <meta property="og:image" content="/cover/103712.jpg">
  <meta property="og:novel:status" content="连载中">
</head><body>
  <div class="sidebar"><div class="novelist"><div class="name">
    <a>作者名</a></div></div></div>
  <div class="section-list">
    <div class="section" data-index-name="v1">
      <h2 class="volume-title">第一卷</h2>
      <div class="volume-cover"><img src="/cover/vol1.jpg"></div>
      <div class="chapter-list">
        <a href="/book/103712/1.html?from=catalog">第1章</a>
        <a href="javascript:;">目录</a>
      </div>
    </div>
  </div>
</body></html>
"""


def test_linovel_search_absolutizes_cover_and_cleans_id() -> None:
    result = _parse(LinovelSearcher, LINOVEL_SEARCH_HTML)[0]

    assert result["book_id"] == "103712"
    assert result["book_url"] == (
        "https://www.linovel.net/book/103712.html?from=search#hit"
    )
    assert result["cover_url"] == "https://rin.linovel.net/c.jpg"
    assert result["author"] == "作者名"
    assert result["update_date"] == "2026-01-01"


def test_linovel_parser_relative_cover_and_base_url(
    cfg: ParserConfig,
) -> None:
    info = LinovelParser(cfg).parse_book_info([LINOVEL_INFO_HTML])

    assert info is not None
    assert LinovelParser.BASE_URL == "https://www.linovel.net/"
    assert info["cover_url"] == "https://www.linovel.net/cover/103712.jpg"
    volume = info["volumes"][0]
    assert volume["volume_cover"] == "https://www.linovel.net/cover/vol1.jpg"
    assert [c["chapterId"] for c in volume["chapters"]] == ["1"]


def test_linovel_parser_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = LinovelParser(cfg)

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["  "]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert _parse(LinovelSearcher, "") == []


# --------------------------------------------------------------------------- #
# laoyaoxs
# --------------------------------------------------------------------------- #
LAOYAOXS_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="测试书">
  <meta property="og:novel:author" content="作者">
  <meta property="og:novel:category" content="玄幻">
  <meta property="og:image" content="//cdn.example.com/cover.jpg">
  <meta property="og:novel:update_time" content="2026-01-01 00:00:00">
</head><body>
  <p class="intro">简介</p>
</body></html>
"""

LAOYAOXS_CATALOG_HTML = """
<html><body>
  <div class="read"><dl id="newlist">
    <dd><a href="/list/7449/123.html?from=catalog" title="第1章">第1章</a></dd>
  </dl></div>
</body></html>
"""


def test_laoyaoxs_parser_chapter_id_is_last_segment(
    cfg: ParserConfig,
) -> None:
    info = LaoyaoxsParser(cfg).parse_book_info(
        [LAOYAOXS_INFO_HTML, LAOYAOXS_CATALOG_HTML]
    )

    assert info is not None
    assert info["cover_url"] == "https://cdn.example.com/cover.jpg"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "123"
    assert chapter["url"] == "/list/7449/123.html?from=catalog"
    assert _parse(LaoyaoxsSearcher, "") == []


# --------------------------------------------------------------------------- #
# blank-body hardening across the whole group
# --------------------------------------------------------------------------- #
GROUP_PARSERS = [
    I25zwParser,
    Ixdzs8Parser,
    Jpxs123Parser,
    KadokadoParser,
    KtshuParser,
    KunnuParser,
    LaoyaoxsParser,
    LewennParser,
    LinovelParser,
    LinovelibParser,
]


@pytest.mark.parametrize("parser_cls", GROUP_PARSERS, ids=lambda c: c.site_name)
def test_group_f_parsers_never_raise_on_blank_pages(
    cfg: ParserConfig, parser_cls: type[Any]
) -> None:
    parser = parser_cls(cfg)

    for pages in ([], [""], ["   "], ["", ""], ["   ", "   "]):
        assert parser.parse_book_info(pages) is None
        assert parser.parse_chapter_content(pages, "1") is None


GROUP_SEARCHERS = [
    I25zwSearcher,
    Ixdzs8Searcher,
    Jpxs123Searcher,
    KadokadoSearcher,
    KunnuSearcher,
    LaoyaoxsSearcher,
    LinovelSearcher,
]
# ``ktshu`` inherits ``ManggNetSearcher._parse_html`` verbatim (a shared
# template invariant pinned by test_searchers_degraded_sites.py), so its
# blank-body behaviour is owned by that module rather than by this slot.


@pytest.mark.parametrize("searcher_cls", GROUP_SEARCHERS, ids=lambda c: c.site_name)
def test_group_f_searchers_never_raise_on_blank_html(
    searcher_cls: type[Any],
) -> None:
    for payload in ("", "   ", "\n\t"):
        assert _parse(searcher_cls, payload) == []
