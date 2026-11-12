#!/usr/bin/env python3
"""Offline fixture tests for the group-G site plugins (rolling slot 5).

The fixtures are trimmed synthetic copies of the markup observed during the
2026-11 network probe.  They cover sites that no earlier group exercised and
pin the defects found while following a search hit through to a download:

* ``esjzone`` left the card cover relative and derived the chapter id only
  when the href contained the literal ``www.esjzone.cc``; blank pages raised
  ``ParserError`` in both parser entry points.
* ``faloo`` kept the query in the search book id and neither the cover nor a
  relative chapter link was absolutized.
* ``n37yq`` had no ``BASE_URL`` at all, so search/detail covers stayed
  relative; blank catalogue pages raised.
* ``n17k`` used ``rstrip(".html")`` for the id (query bytes leak) and left the
  detail cover relative.
* ``syosetu`` / ``syosetu18`` prefixed ``https:`` to every non-``http`` cover
  and image, turning ``/img/x.jpg`` into ``https:/img/x.jpg``.
* ``haiwaishubao`` printed the parsed results to stdout (a side effect) and
  kept a relative cover.
* ``lnovel`` blindly prepended ``BASE_URL`` to ``og:image``, so an absolute
  cover became ``https://lnovel.orghttps://...`` and a missing cover became
  the site root.
* ``n101kanshu`` returned ``limit + 1`` rows, used the raw relative
  ``book_url`` and kept a relative cover.
* ``ttkan`` built a path-shaped chapter id when the href carried a query.
* ``uaa`` printed the request URL and took ``href.split("id=")[-1]`` as the
  id (query garbage, or the whole path when ``id=`` was absent).
* ``westnovel`` concatenated ``BASE_URL`` and cover, producing
  ``https://www.westnovel.comuploads/w.jpg`` for a relative src.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.sites.esjzone.parser import EsjzoneParser
from novel_downloader.plugins.sites.esjzone.searcher import EsjzoneSearcher
from novel_downloader.plugins.sites.faloo.parser import FalooParser
from novel_downloader.plugins.sites.faloo.searcher import FalooSearcher
from novel_downloader.plugins.sites.haiwaishubao.parser import HaiwaishubaoParser
from novel_downloader.plugins.sites.haiwaishubao.searcher import HaiwaishubaoSearcher
from novel_downloader.plugins.sites.lnovel.parser import LnovelParser
from novel_downloader.plugins.sites.lnovel.searcher import LnovelSearcher
from novel_downloader.plugins.sites.n17k.parser import N17kParser
from novel_downloader.plugins.sites.n17k.searcher import N17kSearcher
from novel_downloader.plugins.sites.n37yq.parser import N37yqParser
from novel_downloader.plugins.sites.n37yq.searcher import N37yqSearcher
from novel_downloader.plugins.sites.n101kanshu.parser import N101kanshuParser
from novel_downloader.plugins.sites.n101kanshu.searcher import N101kanshuSearcher
from novel_downloader.plugins.sites.syosetu.parser import SyosetuParser
from novel_downloader.plugins.sites.syosetu.searcher import SyosetuSearcher
from novel_downloader.plugins.sites.syosetu18.parser import Syosetu18Parser
from novel_downloader.plugins.sites.syosetu18.searcher import Syosetu18Searcher
from novel_downloader.plugins.sites.ttkan.parser import TtkanParser
from novel_downloader.plugins.sites.ttkan.searcher import TtkanSearcher
from novel_downloader.plugins.sites.uaa.parser import UaaParser
from novel_downloader.plugins.sites.uaa.searcher import UaaSearcher
from novel_downloader.plugins.sites.westnovel.parser import WestnovelParser
from novel_downloader.plugins.sites.westnovel.searcher import WestnovelSearcher
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


def _parse(cls: type[Any], html_str: str, limit: int | None = None) -> list[Any]:
    # ``_parse_html`` never touches ``self.session``; skip constructing an
    # aiohttp session so the test stays offline.
    return cls(None)._parse_html(html_str, limit)


# --------------------------------------------------------------------------- #
# esjzone
# --------------------------------------------------------------------------- #
ESJZONE_SEARCH_HTML = """
<html><body>
  <div class="card">
    <a class="card-img-tiles">
      <div class="lazyload" data-src="/upload/cover.jpg"></div>
    </a>
    <div class="card-body">
      <h5 class="card-title">
        <a href="/detail/123.html?from=search#hit">测试书</a>
      </h5>
      <div class="card-author"><a>作者A</a></div>
      <div class="card-ep"><a>第1章</a></div>
    </div>
  </div>
</body></html>
"""

ESJZONE_INFO_HTML = """
<html><body>
  <h2 class="text-normal">测试书</h2>
  <li><strong>作者:</strong><a>作者A</a></li>
  <div class="product-gallery"><img src="/img/cover.jpg"></div>
  <div class="description"><p>简介</p></div>
  <div id="chapterList">
    <details>
      <summary>第一卷</summary>
      <a href="/forum/123/456.html?from=catalog" data-title="第1章">第1章</a>
      <a href="https://example.com/ad/9.html" data-title="广告">广告</a>
    </details>
  </div>
</body></html>
"""


def test_esjzone_search_absolutizes_urls_and_cleans_id() -> None:
    result = _parse(EsjzoneSearcher, ESJZONE_SEARCH_HTML)[0]

    assert result["book_id"] == "123"
    assert result["book_url"] == (
        "https://www.esjzone.cc/detail/123.html?from=search#hit"
    )
    assert result["cover_url"] == "https://www.esjzone.cc/upload/cover.jpg"


def test_esjzone_parser_relative_chapter_id_and_cover(
    cfg: ParserConfig,
) -> None:
    info = EsjzoneParser(cfg).parse_book_info([ESJZONE_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.esjzone.cc/img/cover.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert chapters[0]["title"] == "第1章"
    # Relative same-site href: the id is still derivable and must not be lost.
    assert chapters[0]["chapterId"] == "456"
    # Non-esjzone links stay id-less but keep their URL.
    assert chapters[1]["chapterId"] == ""
    assert chapters[1]["url"] == "https://example.com/ad/9.html"


# --------------------------------------------------------------------------- #
# faloo
# --------------------------------------------------------------------------- #
FALOO_SEARCH_HTML = """
<html><body>
  <div class="TwoBox02_02">
    <div class="TwoBox02_03"><img src="/cover/1.jpg"></div>
    <div class="TwoBox02_08">
      <h1><a href="//b.faloo.com/1440069.html?from=search">测试书</a></h1>
    </div>
    <div class="TwoBox02_09"><a>作者</a></div>
  </div>
</body></html>
"""

FALOO_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="测试书">
  <meta property="og:image" content="/img/cover.jpg">
</head><body>
  <div class="T-L-T-C-Box1"><p>简介</p></div>
  <div id="mulu">
    <div class="DivTable">
      <div class="DivTd"><a href="/1482723_53.html?from=catalog">第1章</a></div>
    </div>
  </div>
</body></html>
"""


def test_faloo_search_id_ignores_query_bytes() -> None:
    result = _parse(FalooSearcher, FALOO_SEARCH_HTML)[0]

    assert result["book_id"] == "1440069"
    assert result["book_url"] == ("https://b.faloo.com/1440069.html?from=search")
    assert result["cover_url"] == "https://b.faloo.com/cover/1.jpg"


def test_faloo_parser_absolutizes_cover(cfg: ParserConfig) -> None:
    info = FalooParser(cfg).parse_book_info([FALOO_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://b.faloo.com/img/cover.jpg"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "53"
    assert chapter["url"] == "/1482723_53.html?from=catalog"


# --------------------------------------------------------------------------- #
# n37yq
# --------------------------------------------------------------------------- #
N37YQ_SEARCH_HTML = """
<html><body>
  <div class="search-tab">
    <div class="search-result-list">
      <div class="imgbox">
        <a href="/lightnovel/3860.html?from=search">
          <img src="/images/3860.jpg"></a>
      </div>
      <h2 class="tit">
        <a href="/lightnovel/3860.html?from=search">测试书</a>
      </h2>
      <div class="bookinfo"><a>作者</a></div>
    </div>
  </div>
</body></html>
"""

N37YQ_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="测试书">
  <meta property="og:image" content="/upload/cover.jpg">
</head><body>
  <p class="intro">简介</p>
</body></html>
"""

N37YQ_CATALOG_HTML = """
<html><body>
  <ul class="chapter-list">
    <div class="volume">第一卷</div>
    <li><a href="/lightnovel/3860/1.html">第1章</a></li>
  </ul>
</body></html>
"""


def test_n37yq_search_absolutizes_relative_results() -> None:
    result = _parse(N37yqSearcher, N37YQ_SEARCH_HTML)[0]

    assert result["book_id"] == "3860"
    assert result["book_url"] == (
        "https://www.37yq.com/lightnovel/3860.html?from=search"
    )
    assert result["cover_url"] == "https://www.37yq.com/images/3860.jpg"


def test_n37yq_parser_absolutizes_cover(cfg: ParserConfig) -> None:
    info = N37yqParser(cfg).parse_book_info([N37YQ_INFO_HTML, N37YQ_CATALOG_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.37yq.com/upload/cover.jpg"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "1"


# --------------------------------------------------------------------------- #
# n17k
# --------------------------------------------------------------------------- #
N17K_SEARCH_HTML = """
<html><body>
  <div class="textlist">
    <div class="textleft">
      <a href="//www.17k.com/book/753943.html?from=search">
        <img src="/cover.jpg"></a>
    </div>
    <div class="textmiddle"><dt><a>测试书</a></dt></div>
  </div>
</body></html>
"""

N17K_INFO_HTML = """
<html><body>
  <div class="Info Sign"><h1><a>测试书</a></h1></div>
  <div id="bookCover"><img src="/cover/n17k.jpg"></div>
  <p class="intro">简介</p>
</body></html>
"""

N17K_CATALOG_HTML = """
<html><body>
  <div class="Author"><a>作者</a></div>
  <dl class="Volume">
    <dt><span class="tit">第一卷</span></dt>
    <dd><a href="/book/753943/123.html?from=catalog"><span>第1章</span></a></dd>
  </dl>
</body></html>
"""


def test_n17k_search_id_strips_query_and_extension() -> None:
    result = _parse(N17kSearcher, N17K_SEARCH_HTML)[0]

    assert result["book_id"] == "753943"
    assert result["book_url"] == ("https://www.17k.com/book/753943.html?from=search")
    assert result["cover_url"] == "https://www.17k.com/cover.jpg"


def test_n17k_parser_absolutizes_cover(cfg: ParserConfig) -> None:
    info = N17kParser(cfg).parse_book_info([N17K_INFO_HTML, N17K_CATALOG_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.17k.com/cover/n17k.jpg"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "123"


# --------------------------------------------------------------------------- #
# syosetu / syosetu18
# --------------------------------------------------------------------------- #
SYOSETU_INFO_HTML = """
<html><head>
  <meta property="og:image" content="/img/cover.jpg">
</head><body>
  <h1 class="p-novel__title">测试书</h1>
  <div class="p-novel__author"><a>作者A</a></div>
  <div id="novel_ex">简介</div>
  <div class="p-eplist">
    <div class="p-eplist__chapter-title">第一卷</div>
    <div class="p-eplist__sublist">
      <a class="p-eplist__subtitle" href="/n1234/1/?from=catalog">第1章</a>
    </div>
  </div>
</body></html>
"""

SYOSETU_CHAPTER_HTML = """
<html><body>
  <h1 class="p-novel__title">第1章</h1>
  <div class="p-novel__body">
    <div class="p-novel__text"><p>正文<img src="/img/pic.jpg"></p></div>
  </div>
</body></html>
"""


@pytest.mark.parametrize(
    ("parser_cls", "base_url"),
    [
        (SyosetuParser, "https://ncode.syosetu.com"),
        (Syosetu18Parser, "https://novel18.syosetu.com"),
    ],
    ids=lambda value: value.site_name if isinstance(value, type) else str(value),
)
def test_syosetu_parsers_absolutize_root_relative_assets(
    cfg: ParserConfig, parser_cls: type[Any], base_url: str
) -> None:
    info = parser_cls(cfg).parse_book_info([SYOSETU_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == f"{base_url}/img/cover.jpg"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "1"
    assert chapter["url"] == "/n1234/1/?from=catalog"

    content = parser_cls(cfg).parse_chapter_content([SYOSETU_CHAPTER_HTML], "1")

    assert content is not None
    assert content["content"] == "正文"
    assert [r["url"] for r in content["extra"]["resources"]] == [
        f"{base_url}/img/pic.jpg"
    ]


# --------------------------------------------------------------------------- #
# haiwaishubao
# --------------------------------------------------------------------------- #
HAIWAISHUBAO_SEARCH_HTML = """
<html><body>
  <div class="SHsectionThree-middle">
    <p>
      <a href="/book/7631/">测试书</a>
      <a href="/author/1/">作者</a>
    </p>
  </div>
</body></html>
"""

HAIWAISHUBAO_INFO_HTML = """
<html><head>
  <meta property="og:title" content="测试书">
  <meta property="og:image" content="/img/cover.jpg">
</head><body>
  <p class="author"><a>作者</a></p>
</body></html>
"""

HAIWAISHUBAO_CATALOG_HTML = """
<html><body>
  <ol class="BCsectionTwo-top">
    <li><a href="/book/7631/1.html?from=catalog">第1章</a></li>
  </ol>
</body></html>
"""


def test_haiwaishubao_search_stays_silent_and_cleans_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _parse(HaiwaishubaoSearcher, HAIWAISHUBAO_SEARCH_HTML)[0]

    assert capsys.readouterr().out == ""
    assert result["book_id"] == "7631"
    assert result["book_url"] == "https://www.haiwaishubao.com/book/7631/"


def test_haiwaishubao_parser_absolutizes_cover(cfg: ParserConfig) -> None:
    info = HaiwaishubaoParser(cfg).parse_book_info(
        [HAIWAISHUBAO_INFO_HTML, HAIWAISHUBAO_CATALOG_HTML]
    )

    assert info is not None
    assert info["cover_url"] == "https://www.haiwaishubao.com/img/cover.jpg"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "1"
    assert chapter["url"] == "/book/7631/1.html?from=catalog"


# --------------------------------------------------------------------------- #
# lnovel
# --------------------------------------------------------------------------- #
LNOVEL_INFO_HTML = """
<html><head>
  <meta property="og:image" content="https://cdn.example.com/cover.jpg">
</head><body>
  <main>
    <h1>测试书</h1>
    <h2>简介</h2>
    <p class="my-2">简介文字</p>
  </main>
  <div id="volumes">
    <div class="accordion-item">
      <a class="accordion-button">第一卷</a>
      <div class="list-group">
        <a href="/chapters-1-2?from=catalog">第1章</a>
      </div>
    </div>
  </div>
</body></html>
"""


def test_lnovel_parser_does_not_double_prefix_absolute_cover(
    cfg: ParserConfig,
) -> None:
    info = LnovelParser(cfg).parse_book_info([LNOVEL_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://cdn.example.com/cover.jpg"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "2"


def test_lnovel_parser_missing_cover_is_empty(cfg: ParserConfig) -> None:
    html = """
    <html><body>
      <main><h1>测试书</h1></main>
      <div id="volumes">
        <div class="accordion-item">
          <div class="list-group"><a href="/chapters-1-2">第1章</a></div>
        </div>
      </div>
    </body></html>
    """
    info = LnovelParser(cfg).parse_book_info([html])

    assert info is not None
    assert info["cover_url"] == ""


# --------------------------------------------------------------------------- #
# n101kanshu
# --------------------------------------------------------------------------- #
N101KANSHU_SEARCH_HTML = """
<html><body>
  <ul id="article_list_content">
    <li>
      <a class="imgbox" href="/book/120.html"><img data-src="/c/0.jpg"></a>
      <div class="newnav"><h3><a>书0</a></h3></div>
      <div class="labelbox"><label>作者</label></div>
    </li>
    <li>
      <a class="imgbox" href="/book/121.html"><img data-src="/c/1.jpg"></a>
      <div class="newnav"><h3><a>书1</a></h3></div>
      <div class="labelbox"><label>作者</label></div>
    </li>
    <li>
      <a class="imgbox" href="/book/122.html"><img data-src="/c/2.jpg"></a>
      <div class="newnav"><h3><a>书2</a></h3></div>
      <div class="labelbox"><label>作者</label></div>
    </li>
  </ul>
</body></html>
"""

N101KANSHU_INFO_HTML = """
<html><head>
  <meta property="og:novel:book_name" content="测试书">
  <meta property="og:image" content="/img/cover.jpg">
</head><body>
  <p>字数：1000 | 状态</p>
</body></html>
"""

N101KANSHU_CATALOG_HTML = """
<html><body>
  <ul><li><a href="/txt/123/456.html?from=catalog">第1章</a></li></ul>
</body></html>
"""


def test_n101kanshu_search_respects_limit_and_absolutizes_url() -> None:
    results = _parse(N101kanshuSearcher, N101KANSHU_SEARCH_HTML, 2)

    assert len(results) == 2
    assert results[0]["book_id"] == "120"
    assert results[0]["book_url"] == "https://101kanshu.com/book/120.html"
    assert results[0]["cover_url"] == "https://101kanshu.com/c/0.jpg"


def test_n101kanshu_parser_absolutizes_cover(cfg: ParserConfig) -> None:
    info = N101kanshuParser(cfg).parse_book_info(
        [N101KANSHU_INFO_HTML, N101KANSHU_CATALOG_HTML]
    )

    assert info is not None
    assert info["cover_url"] == "https://101kanshu.com/img/cover.jpg"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "456"


# --------------------------------------------------------------------------- #
# ttkan
# --------------------------------------------------------------------------- #
TTKAN_SEARCH_HTML = """
<html><body>
  <div class="novel_cell">
    <a href="/novel/chapters/6094?from=search">
      <amp-img src="/img/cover.jpg"></amp-img>
    </a>
    <h3>测试书</h3>
    <li>作者：某人</li>
  </div>
</body></html>
"""

TTKAN_INFO_HTML = """
<html><body>
  <div class="novel_info">
    <h1>测试书</h1>
    <li><span>作者：</span><a>作者</a></li>
    <amp-img src="/img/cover.jpg"></amp-img>
  </div>
  <div class="full_chapters">
    <div><a href="/novel/pagea/slug_6094.html?from=catalog">第1章</a></div>
  </div>
</body></html>
"""


def test_ttkan_search_id_strips_query() -> None:
    result = _parse(TtkanSearcher, TTKAN_SEARCH_HTML)[0]

    assert result["book_id"] == "6094"
    assert result["book_url"] == "https://www.ttkan.co/novel/chapters/6094?from=search"


def test_ttkan_parser_chapter_id_is_not_path_shaped(cfg: ParserConfig) -> None:
    info = TtkanParser(cfg).parse_book_info([TTKAN_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.ttkan.co/img/cover.jpg"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "6094"


# --------------------------------------------------------------------------- #
# uaa
# --------------------------------------------------------------------------- #
UAA_SEARCH_HTML = """
<html><body>
  <li class="novel_li_2">
    <div class="cover_box">
      <a href="/novel/intro?id=123&from=search">
        <img class="cover" src="/img/cover.jpg">
      </a>
    </div>
    <div class="title"><a>测试书</a></div>
  </li>
</body></html>
"""

UAA_INFO_HTML = """
<html><body>
  <div class="info_box">
    <h1>测试书</h1>
    <img class="cover" src="/img/cover.jpg">
  </div>
  <ul class="catalog_ul">
    <li class="menu"><a href="/novel/chapter?id=456&from=catalog">第1章</a></li>
  </ul>
</body></html>
"""


def test_uaa_search_stays_silent_and_parses_query_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _parse(UaaSearcher, UAA_SEARCH_HTML)[0]

    assert capsys.readouterr().out == ""
    assert result["book_id"] == "123"
    assert result["book_url"] == "https://www.uaa.com/novel/intro?id=123&from=search"
    assert result["cover_url"] == "https://www.uaa.com/img/cover.jpg"


def test_uaa_parser_parses_query_chapter_id(cfg: ParserConfig) -> None:
    info = UaaParser(cfg).parse_book_info([UAA_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.uaa.com/img/cover.jpg"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "456"
    assert chapter["url"] == "/novel/chapter?id=456&from=catalog"


# --------------------------------------------------------------------------- #
# westnovel
# --------------------------------------------------------------------------- #
WESTNOVEL_INFO_HTML = """
<html><body>
  <div class="btitle">
    <h1><a>测试书</a></h1>
    <em>作者：作者A</em>
  </div>
  <div class="bookinfo"><img class="img-img" src="uploads/cover.jpg"></div>
  <dl class="chapterlist">
    <dd><a href="/wuxia/x/198246.html?from=catalog">第1章</a></dd>
  </dl>
</body></html>
"""


def test_westnovel_parser_absolutizes_bare_relative_cover(
    cfg: ParserConfig,
) -> None:
    info = WestnovelParser(cfg).parse_book_info([WESTNOVEL_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.westnovel.com/uploads/cover.jpg"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "198246"


def test_westnovel_searcher_blank_html_returns_empty() -> None:
    searcher = WestnovelSearcher(None)
    searcher._query = "测试"

    assert searcher._parse_html("") == []
    assert searcher._parse_html("   ") == []


# --------------------------------------------------------------------------- #
# blank-body hardening across the whole group
# --------------------------------------------------------------------------- #
GROUP_PARSERS = [
    EsjzoneParser,
    FalooParser,
    N37yqParser,
    N17kParser,
    SyosetuParser,
    Syosetu18Parser,
    HaiwaishubaoParser,
    LnovelParser,
    N101kanshuParser,
    TtkanParser,
    UaaParser,
    WestnovelParser,
]


@pytest.mark.parametrize("parser_cls", GROUP_PARSERS, ids=lambda c: c.site_name)
def test_group_g_parsers_never_raise_on_blank_pages(
    cfg: ParserConfig, parser_cls: type[Any]
) -> None:
    parser = parser_cls(cfg)

    for pages in ([], [""], ["   "], ["", ""], ["   ", "   "]):
        assert parser.parse_book_info(pages) is None
        assert parser.parse_chapter_content(pages, "1") is None


GROUP_SEARCHERS = [
    EsjzoneSearcher,
    FalooSearcher,
    N37yqSearcher,
    N17kSearcher,
    SyosetuSearcher,
    Syosetu18Searcher,
    HaiwaishubaoSearcher,
    LnovelSearcher,
    N101kanshuSearcher,
    TtkanSearcher,
    UaaSearcher,
    WestnovelSearcher,
]


@pytest.mark.parametrize("searcher_cls", GROUP_SEARCHERS, ids=lambda c: c.site_name)
def test_group_g_searchers_never_raise_on_blank_html(
    searcher_cls: type[Any],
) -> None:
    for payload in ("", "   ", "\n\t"):
        assert _parse(searcher_cls, payload) == []
