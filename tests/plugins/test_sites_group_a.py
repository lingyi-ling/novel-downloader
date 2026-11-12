#!/usr/bin/env python3
"""Offline fixtures for the group-A site searchers.

The fixtures below are trimmed copies of the real markup/JSON observed during
the 2026-11 network probe, so the parsers are exercised without any network
access.  ``test_b520_*`` additionally pins the endpoint fix: ``www.b520.cc``
no longer serves ``/modules/article/search.php`` (404), while the alternate
domain linked from its own header (``b5200.org`` -> ``www.bqg6.xyz``) still
does and shares the same book ids.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from novel_downloader.plugins.sites.b520.fetcher import B520Fetcher
from novel_downloader.plugins.sites.b520.parser import B520Parser
from novel_downloader.plugins.sites.b520.searcher import B520Searcher
from novel_downloader.plugins.sites.biquge345.fetcher import Biquge345Fetcher
from novel_downloader.plugins.sites.biquge345.parser import Biquge345Parser
from novel_downloader.plugins.sites.biquge345.searcher import Biquge345Searcher
from novel_downloader.plugins.sites.n23qb.fetcher import N23qbFetcher
from novel_downloader.plugins.sites.n23qb.parser import N23qbParser
from novel_downloader.plugins.sites.n23qb.searcher import N23qbSearcher
from novel_downloader.plugins.sites.qidian.fetcher import QidianFetcher
from novel_downloader.plugins.sites.qidian.parser import QidianParser
from novel_downloader.plugins.sites.qidian.searcher import QidianSearcher
from novel_downloader.plugins.sites.qimao.fetcher import QimaoFetcher
from novel_downloader.plugins.sites.qimao.parser import QimaoParser
from novel_downloader.plugins.sites.qimao.searcher import QimaoSearcher
from novel_downloader.plugins.sites.qqbook.fetcher import QqbookFetcher
from novel_downloader.plugins.sites.qqbook.parser import QqbookParser
from novel_downloader.plugins.sites.qqbook.searcher import QqbookSearcher
from novel_downloader.plugins.sites.quanben5.fetcher import Quanben5Fetcher
from novel_downloader.plugins.sites.quanben5.parser import Quanben5Parser
from novel_downloader.plugins.sites.quanben5.searcher import Quanben5Searcher
from novel_downloader.plugins.sites.shuhaige.fetcher import ShuhaigeFetcher
from novel_downloader.plugins.sites.shuhaige.parser import ShuhaigeParser
from novel_downloader.plugins.sites.shuhaige.searcher import ShuhaigeSearcher
from novel_downloader.schemas import FetcherConfig, ParserConfig, SearchResult

B520_SEARCH_HTML = """
<html><body>
<table class="grid" width="100%" align="center">
<caption>搜索结果</caption>
  <tr align="center">
    <th width="20%">文章名称</th>
    <th width="40%">最新章节</th>
    <th width="15%">作者</th>
	<th width="9%">字数</th>
    <th width="10%">更新</th>
    <th width="6%">状态</th>
  </tr>
  <tr>
    <td class="odd"><a href="/0_984/">凡人修仙传</a></td>
    <td class="even"><a href="/0_984/5953296.html" target="_blank">
      忘语新书《玄界之门》</a></td>
    <td class="odd">忘语</td>
	<td class="even">17069K</td>
    <td class="odd" align="center">2016-01-30</td>
    <td class="even" align="center">已经完本</td>
  </tr>
  <tr>
    <td class="odd"><a href="/132_132026/">直播凡人修仙传</a></td>
    <td class="even"><a href="/132_132026/172796808.html" target="_blank"> 催更</a></td>
    <td class="odd">呼言道人</td>
	<td class="even">0K</td>
    <td class="odd" align="center">2020-06-24</td>
    <td class="even" align="center">连载</td>
  </tr>
</table>
</body></html>
"""

QIDIAN_SEARCH_HTML = """
<div id="result-list">
  <ul>
    <li class="res-book-item jsAutoReport" data-bid="107580">
      <div class="book-img-box"><a href="//www.qidian.com/book/107580/">
        <img class="lazy" src="//qdfepccdn.qidian.com/default_book.png"
          data-original="//bookcover.yuewen.com/qdbimg/349573/107580/150"
          alt="凡人修仙传在线阅读">
      </a></div>
      <div class="book-mid-info">
        <h3 class="book-info-title"><a title="凡人修仙传在线阅读"
          href="//www.qidian.com/book/107580/">凡人修仙传</a></h3>
        <p class="author"><a class="name" title="忘语"
          href="//my.qidian.com/author/4362771/">忘语</a></p>
        <p class="update"><a href="//www.qidian.com/chapter/107580/91969281/">
          最新更新 忘语新书《玄界之门》</a><em>·</em><span>2016-01-05</span></p>
      </div>
      <div class="book-right-info"><div class="total">
        <p><span> 767.61万</span>总字数</p><p><span> 1622.77万</span>总推荐</p>
      </div></div>
    </li>
  </ul>
</div>
"""

QIMAO_SEARCH_JSON = """
{"data": {"page_data": {"count": "2090", "page": "1", "page_size": "15"},
  "search_list": [
    {"book_id": "1996719",
     "image_link": "https://cdn.wtzw.com/bookimg/public/cover.jpg",
     "title": "凡人修仙：赠送机缘，暴击返还",
     "author": "凡人修仙",
     "read_url": "https://www.qimao.com/shuku/1996719/",
     "latest_chapter_title": "第167章 大战清风二（大结局）",
     "words_num": "36.22万字",
     "update_time": "2025-05-14 15:16:43"}
  ]}}
"""

QQBOOK_SEARCH_JSON = """
{"code": 0, "data": {"pageNo": 1, "pageSize": 20, "totals": 200,
  "bookList": [
    {"bid": 465030, "author": "忘语", "title": "凡人修仙传",
     "totalWords": 7437465, "finished": 1}
  ]}}
"""

SHUHAIGE_SEARCH_HTML = """
<html><body><div id="sitembox">
  <dl>
    <dt><a href="/117632/"><img src="https://img.shuhaige.net/117632/841.jpg"
      alt="凡人修仙传" height="150" width="120"></a></dt>
    <dd><h3><a href="/117632/">凡人修仙传</a></h3></dd>
    <dd class="book_other">作者：<span>忘语</span>状态：<span>已完结</span>
      分类：<span>奇幻</span>字数：<span>1875万字</span></dd>
    <dd class="book_des">一个普通山村小子……</dd>
    <dd class="book_other">最新章节：<a href="/117632/539883.html">感言</a>
      更新时间：<span>2023-06-16 00:48</span></dd>
  </dl>
</div></body></html>
"""

QUANBEN5_SEARCH_JSONP = (
    r"""search({"id":0,"content":"<div class=\"pic_txt_list\">\r\n\t"""
    r"""<div class=\"pic\"><img src=\"https:\/\/img.c0m.io\/quanben5.com\/"""
    r"""upload\/book_images\/1\/106.jpg\" alt=\"\" \/><\/div>\r\n\t<h3>"""
    r"""<a href=\"\/n\/fanrenxiuxianchuan\/\"><span class=\"name\"><b>"""
    r"""凡人修仙传<\/b><\/span><\/a><\/h3>\r\n\t<p class=\"info\">作者: """
    r"""<span class=\"author\">忘语<\/span> (完结)<\/p>\r\n\t<p """
    r"""class=\"description\"><span class=\"read_ico\">简<\/span>"""
    r"""一个普通山村小子……<\/p>\r\n<\/div>"});"""
)

BIQUGE345_SEARCH_HTML = """
<html><body><ul class="search">
  <li><span class="lei"><a href="/sort/1_1/">[玄幻魔法]</a></span>
    <span class="name"><a href="/book/926042/"
      title="凡人修仙传之鸿蒙道种文章列表">凡人修仙传之鸿蒙道种</a></span>
    <span class="jie"><a href="/chapter/926042/131314021.html">
      第二百五十七章 重回天南</a></span>
    <span class="zuo"><a href="/author/%E5%A4%A9%E9%97%AE%E6%84%8F%E8%BD%A9">
      天问意轩</a></span><span class="time">06-27</span></li>
</ul></body></html>
"""


def _parse(
    cls: type[Any], html_str: str, limit: int | None = None
) -> list[SearchResult]:
    # ``_parse_html`` never touches ``self.session``; skip constructing the
    # aiohttp session so these tests stay offline.
    return cls(None)._parse_html(html_str, limit)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# b520: the search endpoint moved off www.b520.cc
# --------------------------------------------------------------------------- #
def test_b520_search_endpoint_uses_working_domain() -> None:
    assert B520Searcher.BASE_URL == "https://www.bqg6.xyz/"
    assert B520Searcher.SEARCH_URL == (
        "https://www.bqg6.xyz/modules/article/search.php"
    )
    assert B520Fetcher.book_info_url("0_984") == "https://www.bqg6.xyz/0_984/"
    assert (
        B520Fetcher.chapter_url("0_984", "5953296")
        == "https://www.bqg6.xyz/0_984/5953296.html"
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.charset = "gbk"

    async def read(self) -> bytes:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _FakeResponseCtx:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse(self._body)

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self, body: bytes) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self._body = body

    def get(self, url: str, **kwargs: Any) -> _FakeResponseCtx:
        self.calls.append({"url": url, **kwargs})
        return _FakeResponseCtx(self._body)


def test_b520_fetch_uses_search_endpoint_and_searchkey() -> None:
    session = _FakeSession(B520_SEARCH_HTML.encode("gbk"))
    searcher = B520Searcher(session)  # type: ignore[arg-type]

    html_str = asyncio.run(searcher._fetch_html("凡人修仙传"))

    assert "凡人修仙传" in html_str
    assert session.calls[0]["url"] == B520Searcher.SEARCH_URL
    assert session.calls[0]["params"] == {"searchkey": "凡人修仙传"}
    assert session.calls[0]["headers"]["Referer"] == B520Searcher.BASE_URL


def test_b520_parse_search_results() -> None:
    results = _parse(B520Searcher, B520_SEARCH_HTML)

    assert len(results) == 2
    first = results[0]
    assert first["site"] == "b520"
    assert first["book_id"] == "0_984"
    assert first["book_url"] == "https://www.bqg6.xyz/0_984/"
    assert first["title"] == "凡人修仙传"
    assert first["author"] == "忘语"
    assert first["latest_chapter"] == "忘语新书《玄界之门》"
    assert first["update_date"] == "2016-01-30"
    assert first["word_count"] == "17069K"
    assert first["priority"] == B520Searcher.priority

    assert results[1]["book_id"] == "132_132026"
    assert results[1]["author"] == "呼言道人"

    assert len(_parse(B520Searcher, B520_SEARCH_HTML, limit=1)) == 1


# --------------------------------------------------------------------------- #
# qidian
# --------------------------------------------------------------------------- #
def test_qidian_parse_search_results() -> None:
    results = _parse(QidianSearcher, QIDIAN_SEARCH_HTML)

    assert len(results) == 1
    result = results[0]
    assert result["site"] == "qidian"
    assert result["book_id"] == "107580"
    assert result["book_url"] == "https://www.qidian.com/book/107580/"
    assert result["cover_url"] == (
        "https://bookcover.yuewen.com/qdbimg/349573/107580/150"
    )
    assert result["title"] == "凡人修仙传"
    assert result["author"] == "忘语"
    assert result["latest_chapter"] == "忘语新书《玄界之门》"
    assert result["update_date"] == "2016-01-05"
    assert result["word_count"] == "767.61万"


def test_qidian_cover_falls_back_to_src() -> None:
    html_str = QIDIAN_SEARCH_HTML.replace(
        'data-original="//bookcover.yuewen.com/qdbimg/349573/107580/150"', ""
    )
    results = _parse(QidianSearcher, html_str)

    assert results[0]["cover_url"] == ("https://qdfepccdn.qidian.com/default_book.png")


# --------------------------------------------------------------------------- #
# qimao
# --------------------------------------------------------------------------- #
def test_qimao_parse_search_results() -> None:
    results = _parse(QimaoSearcher, QIMAO_SEARCH_JSON)

    assert len(results) == 1
    result = results[0]
    assert result["site"] == "qimao"
    assert result["book_id"] == "1996719"
    assert result["book_url"] == "https://www.qimao.com/shuku/1996719/"
    assert result["cover_url"] == "https://cdn.wtzw.com/bookimg/public/cover.jpg"
    assert result["title"] == "凡人修仙：赠送机缘，暴击返还"
    assert result["author"] == "凡人修仙"
    assert result["latest_chapter"] == "第167章 大战清风二（大结局）"
    assert result["update_date"] == "2025-05-14 15:16:43"
    assert result["word_count"] == "36.22万字"


def test_qimao_rejects_non_json() -> None:
    assert _parse(QimaoSearcher, "<html>blocked</html>") == []


# --------------------------------------------------------------------------- #
# qqbook
# --------------------------------------------------------------------------- #
def test_qqbook_parse_search_results() -> None:
    results = _parse(QqbookSearcher, QQBOOK_SEARCH_JSON)

    assert len(results) == 1
    result = results[0]
    assert result["site"] == "qqbook"
    assert result["book_id"] == "465030"
    assert result["book_url"] == "https://book.qq.com/book-detail/465030"
    assert result["title"] == "凡人修仙传"
    assert result["author"] == "忘语"
    assert result["word_count"] == "743.7万字"


def test_qqbook_rejects_error_code() -> None:
    assert _parse(QqbookSearcher, '{"code": 1, "data": {}}') == []


# --------------------------------------------------------------------------- #
# shuhaige
# --------------------------------------------------------------------------- #
def test_shuhaige_parse_search_results() -> None:
    results = _parse(ShuhaigeSearcher, SHUHAIGE_SEARCH_HTML)

    assert len(results) == 1
    result = results[0]
    assert result["site"] == "shuhaige"
    assert result["book_id"] == "117632"
    assert result["book_url"] == "https://www.shuhaige.net/117632/"
    assert result["cover_url"] == "https://img.shuhaige.net/117632/841.jpg"
    assert result["title"] == "凡人修仙传"
    assert result["author"] == "忘语"
    assert result["latest_chapter"] == "感言"
    assert result["update_date"] == "2023-06-16 00:48"
    assert result["word_count"] == "1875万字"


# --------------------------------------------------------------------------- #
# quanben5
# --------------------------------------------------------------------------- #
def test_quanben5_parse_search_results() -> None:
    results = _parse(Quanben5Searcher, QUANBEN5_SEARCH_JSONP)

    assert len(results) == 1
    result = results[0]
    assert result["site"] == "quanben5"
    assert result["book_id"] == "fanrenxiuxianchuan"
    assert result["book_url"] == "https://quanben5.com/n/fanrenxiuxianchuan/"
    assert result["cover_url"] == (
        "https://img.c0m.io/quanben5.com/upload/book_images/1/106.jpg"
    )
    assert result["title"] == "凡人修仙传"
    assert result["author"] == "忘语"


def test_quanben5_unwraps_jsonp_and_rejects_garbage() -> None:
    assert _parse(Quanben5Searcher, "not json") == []


# --------------------------------------------------------------------------- #
# biquge345
# --------------------------------------------------------------------------- #
def test_biquge345_parse_search_results() -> None:
    results = _parse(Biquge345Searcher, BIQUGE345_SEARCH_HTML)

    assert len(results) == 1
    result = results[0]
    assert result["site"] == "biquge345"
    assert result["book_id"] == "926042"
    assert result["book_url"] == "https://www.biquge345.com/book/926042/"
    assert result["title"] == "凡人修仙传之鸿蒙道种"
    assert result["author"] == "天问意轩"
    assert result["latest_chapter"] == "第二百五十七章 重回天南"
    assert result["update_date"] == "06-27"


# --------------------------------------------------------------------------- #
# n23qb: search pages are Cloudflare-challenged, so only the detail-page shape
# is covered here (fixture built from the parser's documented markup).
# --------------------------------------------------------------------------- #
def test_n23qb_parse_detail_page() -> None:
    detail_html = """
    <html><head>
      <meta property="og:url" content="https://www.23qb.com/book/9268/">
    </head><body>
      <div class="novel-cover"><img data-src="/cover/9268.jpg"></div>
      <h1 class="page-title">凡人修仙传</h1>
      <a href="/author/wangyu/" title="忘语">忘语</a>
      <span>767万字</span>
      <div class="module-heading newchapter"><time>更新时间：2024-01-02</time>
      </div>
      <div class="module-row-info"><a class="module-row-text" title="第一章">
        第一章</a></div>
    </body></html>
    """
    results = _parse(N23qbSearcher, detail_html)

    assert len(results) == 1
    result = results[0]
    assert result["site"] == "n23qb"
    assert result["book_id"] == "9268"
    assert result["book_url"] == "https://www.23qb.com/book/9268/"
    assert result["title"] == "凡人修仙传"
    assert result["author"] == "忘语"


def test_group_a_parsers_tolerate_empty_document() -> None:
    # ``BaseSearcher.search`` returns before parsing when the body is empty,
    # so the reachable case is a document with no result nodes.
    for cls in (
        QimaoSearcher,
        QqbookSearcher,
        Quanben5Searcher,
        ShuhaigeSearcher,
        Biquge345Searcher,
    ):
        assert _parse(cls, "<html><body></body></html>") == []


# =========================================================================== #
# Book page / catalog / chapter parsing (trimmed copies of the 2026-11 live
# markup, so no network is needed to replay these shapes).
# =========================================================================== #
@pytest.fixture()
def parser_cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


B520_BOOK_HTML = """
<html><body>
<div class="con_top">
  <a href="/">笔趣阁</a> &gt; <a href="/xiuzhenxiaoshuo/">修真小说</a>
</div>
<div id="info">
  <h1>凡人修仙传</h1>
  <p>作&nbsp;&nbsp;&nbsp;&nbsp;者：忘语</p>
  <p>动作：<a href="javascript:download('凡人修仙传');">TXT下载</a></p>
  <p>最后更新：2016-01-30</p>
</div>
<div id="intro"><p>一个普通山村小子……</p></div>
<div id="fmimg">
  <img src="http://r.m.bqg6.xyz/files/article/image/0/984/984s.jpg">
</div>
<div id="list"><dl>
  <dt><b>《凡人修仙传》最新章节</b>（缓存提示）</dt>
  <dd><a href='/0_984/5953296.html'>忘语新书《玄界之门》</a></dd>
  <dd><a href='/0_984/5915976.html'>新书《玄界之门》正式发布了哦！</a></dd>
  <dt>《凡人修仙传》正文</dt>
  <dd><a href="/0_984/746463.html">第一章 山边小村</a></dd>
  <dd><a href="/0_984/746464.html">第二章 青牛镇</a></dd>
</dl></div>
</body></html>
"""

B520_CHAPTER_HTML = """
<html><body>
<div class="bookname"> <h1> 忘语新书《玄界之门》</h1> </div>
<div id="content" class="c0"><p>　　序章</p><p>　　浩瀚宇宙某个偏僻星域。</p>
<div id='gc1' class='gcontent1'><script type='text/javascript'>ggauto();</script></div>
<p>　　未完待续。</p></div>
</body></html>
"""


def test_b520_book_info_skips_latest_chapter_block(
    parser_cfg: ParserConfig,
) -> None:
    info = B520Parser(parser_cfg).parse_book_info([B520_BOOK_HTML])

    assert info is not None
    assert info["book_name"] == "凡人修仙传"
    assert info["author"] == "忘语"
    assert info["update_time"] == "2016-01-30"
    assert info["tags"] == ["修真小说"]
    chapters = info["volumes"][0]["chapters"]
    # Only the 正文 block; the preceding 最新章节 block is a preview and would
    # otherwise duplicate the tail of the book.
    assert [c["chapterId"] for c in chapters] == ["746463", "746464"]
    assert chapters[0]["title"] == "第一章 山边小村"


def test_b520_chapter_content_ignores_inline_ad_container(
    parser_cfg: ParserConfig,
) -> None:
    chapter = B520Parser(parser_cfg).parse_chapter_content(
        [B520_CHAPTER_HTML], "5953296"
    )

    assert chapter is not None
    assert chapter["title"] == "忘语新书《玄界之门》"
    assert chapter["content"] == "序章\n浩瀚宇宙某个偏僻星域。\n未完待续。"


SHUHAIGE_BOOK_HTML = """
<html><body>
<div class="con_top"><a href="https://www.shuhaige.net">书海阁小说网</a>
  &gt;&gt; 凡人修仙传全文阅读列表</div>
<div id="info"><h1>凡人修仙传</h1>
  <p>作&nbsp;&nbsp;者：<a href="/author/忘语/">忘语</a></p>
  <p>动&nbsp;&nbsp;作：加入书架</p>
  <p>最后更新：2023-06-16 00:48:16</p>
  <p>最新更新：<a href="/117632/539883.html">感言</a></p></div>
<div id="intro"><p>一个普通山村小子……</p></div>
<div id="fmimg"><img src="https://img.shuhaige.net/117632/841.jpg"></div>
<div id="list"><dl>
  <dt> 《凡人修仙传》的结局（提示：已启用缓存技术） </dt>
  <dd><a href="/117632/539883.html">感言</a></dd>
  <dt><b>《凡人修仙传》正文</b><a href="/txt_117632.html">txt</a></dt>
  <dd><a href="/117632/537331.html">第一章 山边小村</a></dd>
  <dd><a href="/117632/537332.html">第二章 青牛镇</a></dd>
</dl></div>
</body></html>
"""

SHUHAIGE_CHAPTER_PAGES = [
    '<div class="bookname"><h1>第一章 山边小村</h1></div>'
    '<div id="content"><p>第一段。</p></div>',
    '<div id="content"><p>第二段。</p><p>点击下一页继续阅读</p></div>',
]


def test_shuhaige_book_info_skips_ending_block(
    parser_cfg: ParserConfig,
) -> None:
    info = ShuhaigeParser(parser_cfg).parse_book_info([SHUHAIGE_BOOK_HTML])

    assert info is not None
    assert info["book_name"] == "凡人修仙传"
    assert info["author"] == "忘语"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["537331", "537332"]


def test_shuhaige_paginated_chapter_joins_pages_in_order(
    parser_cfg: ParserConfig,
) -> None:
    chapter = ShuhaigeParser(parser_cfg).parse_chapter_content(
        SHUHAIGE_CHAPTER_PAGES, "537331"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 山边小村"
    assert chapter["content"] == "第一段。\n第二段。"


BIQUGE345_BOOK_HTML = """
<html><body>
<div class="right_border"><h1>凡人修仙传之鸿蒙道种</h1>
  <div class="zhutu"><img src="/files/article/image/240/240541/240541s.jpg"></div>
  <div class="xinxi">
    <span class="x1">作者：<a href="/author/天问意轩">天问意轩</a></span>
    <span class="x1">类型：玄幻魔法</span>
    <span class="x2">更新时间：2026-06-27 10:06:52</span>
    <div class="x3">简介：穿越凡人……</div>
  </div></div>
<div class="border"><h2>全部章节</h2><ul class="info">
  <li><a href="/chapter/926042/881234811.html"
    title="第一章 天灵根与伪灵根">第一章 天灵根与伪灵根</a></li>
  <li><a href="/chapter/926042/091234811.html"
    title="第二章 黄枫谷清波洞">第二章 黄枫谷清波洞</a></li>
</ul></div>
</body></html>
"""

BIQUGE345_CHAPTER_HTML = """
<html><body>
<div id="neirong"><h1>第一章 天灵根与伪灵根</h1>
<div id="txt" class="txt">
<p style="font-weight:bold";>一秒记住【笔趣阁小说网】biquge345.com，更新快！</p><br>
「水火土木四灵根，落选。」<br/>升仙大会上，随着南宫婉面无表情的公布结果。
</div></div>
</body></html>
"""


def test_biquge345_book_info(parser_cfg: ParserConfig) -> None:
    info = Biquge345Parser(parser_cfg).parse_book_info([BIQUGE345_BOOK_HTML])

    assert info is not None
    assert info["book_name"] == "凡人修仙传之鸿蒙道种"
    assert info["author"] == "天问意轩"
    assert info["cover_url"].startswith("https://www.biquge345.com/")
    assert info["tags"] == ["玄幻魔法"]
    assert info["update_time"] == "2026-06-27 10:06:52"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["881234811", "091234811"]


def test_biquge345_chapter_content_drops_ad_line(
    parser_cfg: ParserConfig,
) -> None:
    chapter = Biquge345Parser(parser_cfg).parse_chapter_content(
        [BIQUGE345_CHAPTER_HTML], "881234811"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 天灵根与伪灵根"
    lines = chapter["content"].split("\n")
    assert "biquge345" not in chapter["content"]
    assert lines == [
        "「水火土木四灵根，落选。」",
        "升仙大会上，随着南宫婉面无表情的公布结果。",
    ]


QUANBEN5_BOOK_HTML = """
<html><body>
<div class="box"><h2 class="title"><span>书籍</span></h2>
  <div class="pic_txt_list">
    <div class="pic"><img
      src="https://img.c0m.io/quanben5.com/upload/book_images/1/106.jpg"></div>
    <h3><span>凡人修仙传</span></h3>
    <p class="info">作者: <span class="author">忘语</span></p>
    <p class="info">类别: <span>仙侠</span></p>
    <p class="info">状态: <span>完结</span></p>
  </div>
  <p class="description">一个普通山村小子……</p>
</div>
<div class="box"><h2 class="title"><span>正文</span></h2><ul class="list">
  <li class="c3"><a href="/n/fanrenxiuxianchuan/3682.html">
    <span>《凡人必备手册》第二版</span></a></li>
  <li class="c3"><a href="/n/fanrenxiuxianchuan/spell.html?from=catalog">
    <span>尾声</span></a></li>
</ul></div>
</body></html>
"""

QUANBEN5_CHAPTER_HTML = """
<html><body><div class="content"><h1 class="title1">第一章</h1>
<div id="content"><span id="ad"></span><p>第一段。</p><p>第二段。</p></div>
</div></body></html>
"""


def test_quanben5_book_info_and_chapter_ids(parser_cfg: ParserConfig) -> None:
    info = Quanben5Parser(parser_cfg).parse_book_info([QUANBEN5_BOOK_HTML])

    assert info is not None
    assert (info["book_name"], info["author"], info["tags"]) == (
        "凡人修仙传",
        "忘语",
        ["仙侠"],
    )
    chapters = info["volumes"][0]["chapters"]
    # ``spell`` would become ``spe`` under the old rstrip(".html"), and the
    # query string would be glued onto the id.
    assert [c["chapterId"] for c in chapters] == ["3682", "spell"]
    assert info["serial_status"] == "完结"


def test_quanben5_chapter_content(parser_cfg: ParserConfig) -> None:
    chapter = Quanben5Parser(parser_cfg).parse_chapter_content(
        [QUANBEN5_CHAPTER_HTML], "3682"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章"
    assert chapter["content"] == "第一段。\n第二段。"


N23QB_BOOK_HTML = """
<html><head><meta property="og:url"
  content="https://www.23qb.com/book/9268/"></head><body>
<div class="novel-cover"><img data-src="/cover/9268.jpg"></div>
<h1 class="page-title">凡人修仙传</h1>
<a href="/author/wangyu/" title="忘语">忘语</a>
<a class="tag-link" href="/sort/">连载</a>
<span>767万字</span>
<div class="novel-info-item novel-info-content"><span>简介文字</span></div>
</body></html>
"""

N23QB_CATALOG_HTML = """
<html><body><div class="box">
  <h2 class="module-title">正文</h2>
  <div class="module-row-info"><a class="module-row-text"
    href="/book/9268/7908999.html"><span>第一章 山边小村</span></a></div>
  <div class="module-row-info"><a class="module-row-text"
    href="javascript:cid(0)"><span>VIP 章节</span></a></div>
</div></body></html>
"""


def test_n23qb_book_info_and_locked_chapter(
    parser_cfg: ParserConfig,
) -> None:
    info = N23qbParser(parser_cfg).parse_book_info(
        [N23QB_BOOK_HTML, N23QB_CATALOG_HTML]
    )

    assert info is not None
    assert info["book_name"] == "凡人修仙传"
    assert info["author"] == "忘语"
    assert info["serial_status"] == "连载"
    assert info["cover_url"] == "https://www.23qb.com/cover/9268.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["7908999", ""]
    assert chapters[1]["url"] == ""


@pytest.mark.parametrize("parser_cls", [B520Parser, Quanben5Parser, N23qbParser])
def test_blank_pages_do_not_crash_book_or_chapter_parsers(
    parser_cfg: ParserConfig, parser_cls: Any
) -> None:
    # A blank 200 body must degrade to None: lxml raises "Document is empty"
    # when it is handed "" directly.
    parser = parser_cls(parser_cfg)
    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["", "   "]) is None
    assert parser.parse_chapter_content(["", "  "], "1") is None


QIDIAN_CATALOG_HTML = """
<html><body>
<div id="allCatalog"><div class="catalog-volume">
  <h3 class="volume-name">正文卷·共1章</h3>
  <ul class="volume-chapters"><li>
    <a class="chapter-name" href="/chapter/12345/99/?from=catalog">第一章</a>
    <em class="chapter-locked"></em>
  </li></ul>
</div></div>
</body></html>
"""


def _qidian_chapter_page(
    content: str, title: str = "第一章", chapter_id: str = "1"
) -> str:
    payload = {
        "pageContext": {
            "pageProps": {
                "pageData": {
                    "chapterInfo": {
                        "chapterName": title,
                        "content": content,
                        "chapterId": chapter_id,
                        "vipStatus": 0,
                        "isBuy": 1,
                        "cES": 0,
                        "eFW": 0,
                        "fEnS": 0,
                    }
                }
            }
        }
    }
    return (
        '<html><body><script id="vite-plugin-ssr_pageContext">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


def test_qidian_catalog_chapter_id_ignores_query(
    parser_cfg: ParserConfig,
) -> None:
    info = QidianParser(parser_cfg).parse_book_info([QIDIAN_CATALOG_HTML])

    assert info is not None
    assert info["volumes"][0]["volume_name"] == "正文卷"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter["chapterId"] == "99"
    assert chapter["accessible"] is False


def test_qidian_plain_chapter_has_no_paragraph_markup(
    parser_cfg: ParserConfig,
) -> None:
    page = _qidian_chapter_page("<p>第一段。</p><p>第二段。</p>")
    chapter = QidianParser(parser_cfg).parse_chapter_content([page], "1")

    assert chapter is not None
    assert chapter["content"] == "第一段。\n第二段。"


def test_qidian_normal_parse_keeps_entities_and_line_breaks(
    parser_cfg: ParserConfig,
) -> None:
    parser = QidianParser(parser_cfg)
    text = parser._parse_normal('<p>a<span class="x">b</span>c &amp; d<br/>e</p>')

    assert text == "abc & d\ne"


QIMAO_READER_HTML = r"""
<html><body>
<div class="chapter-detail-wrap-info">
  <h2 class="chapter-title" data-v-1>第1章 获得机缘返还系统</h2>
</div>
<script>window.__NUXT__={chapterData:"\u003Cp\u003E小腹翻江倒海。\u003C\u002Fp\u003E\u003Cp\u003E睁开眼。\u003C\u002Fp\u003E"}</script>
</body></html>
"""


def test_qimao_chapter_title_falls_back_to_reader_page(
    parser_cfg: ParserConfig,
) -> None:
    # A resumed download reuses cached book info and never calls
    # ``parse_book_info``, so the catalog-derived title map is empty.
    parser = QimaoParser(parser_cfg)
    chapter_id = "reader-only-424242"
    parser._chapter_titles.pop(chapter_id, None)

    chapter = parser.parse_chapter_content([QIMAO_READER_HTML], chapter_id)

    assert chapter is not None
    assert chapter["title"] == "第1章 获得机缘返还系统"
    assert chapter["content"] == "小腹翻江倒海。\n睁开眼。"


QQBOOK_QUERY_URL_INFO = """
<html><head>
<meta property="og:novel:read_url"
  content="https://book.qq.com/book-detail/41089201/?from=share">
<meta property="og:novel:book_name" content="凡人修仙传">
</head><body></body></html>
"""

QQBOOK_ONE_CHAPTER = json.dumps(
    {
        "code": 0,
        "data": [{"cid": 1, "chapterName": "第1章", "free": 1, "purchased": 0}],
    }
)


def test_qqbook_book_id_ignores_trailing_slash_and_query(
    parser_cfg: ParserConfig,
) -> None:
    info = QqbookParser(parser_cfg).parse_book_info(
        [QQBOOK_QUERY_URL_INFO, QQBOOK_ONE_CHAPTER]
    )

    assert info is not None
    assert info["volumes"][0]["chapters"][0]["url"] == "/book-read/41089201/1"


# --------------------------------------------------------------------------- #
# book_url / book_id must agree for relative, absolute and query-bearing links
# --------------------------------------------------------------------------- #
def test_b520_search_book_id_with_absolute_query_link() -> None:
    page = """
    <table class="grid"><tr><th>文章名称</th></tr>
      <tr><td><a href="https://www.bqg6.xyz/0_984/?from=search">凡人修仙传</a></td>
      <td></td><td>忘语</td><td>17069K</td><td>2016-01-30</td></tr>
    </table>
    """
    result = _parse(B520Searcher, page)[0]

    assert result["book_id"] == "0_984"
    assert result["book_url"] == "https://www.bqg6.xyz/0_984/?from=search"


def test_biquge345_search_book_id_with_absolute_query_link() -> None:
    page = """
    <ul class="search"><li>
      <span class="name"><a
        href="https://www.biquge345.com/book/926042/?from=search">书</a></span>
    </li></ul>
    """
    result = _parse(Biquge345Searcher, page)[0]

    assert result["book_id"] == "926042"
    assert result["book_url"] == "https://www.biquge345.com/book/926042/?from=search"


def test_quanben5_search_book_id_with_absolute_query_link() -> None:
    content = (
        '<div class="pic_txt_list"><h3><a '
        'href="https://quanben5.com/n/douposanqian/?from=search">'
        '<span class="name">书</span></a></h3>'
        '<p class="info">作者: <span class="author">作者</span></p></div>'
    )
    payload = json.dumps({"content": content}, ensure_ascii=False)

    result = _parse(Quanben5Searcher, f"search({payload});")[0]

    assert result["book_id"] == "douposanqian"
    assert result["book_url"] == ("https://quanben5.com/n/douposanqian/?from=search")


def test_shuhaige_search_book_id_with_absolute_query_link() -> None:
    page = """
    <div id="sitembox"><dl>
      <dt><a href="https://www.shuhaige.net/117632/?from=search">
        <img src="https://img.shuhaige.net/117632/841.jpg" alt="书"></a></dt>
      <dd><h3><a href="https://www.shuhaige.net/117632/?from=search">书</a></h3></dd>
    </dl></div>
    """
    result = _parse(ShuhaigeSearcher, page)[0]

    assert result["book_id"] == "117632"
    assert result["book_url"] == "https://www.shuhaige.net/117632/?from=search"


def test_n23qb_search_list_book_id_with_absolute_query_link() -> None:
    page = """
    <div class="module-search-item"><div class="novel-info-header"><h3>
      <a href="https://www.23qb.com/book/9138/?from=search">书</a>
    </h3></div></div>
    """
    result = _parse(N23qbSearcher, page)[0]

    assert result["book_id"] == "9138"
    assert result["book_url"] == "https://www.23qb.com/book/9138/?from=search"


def test_n23qb_detail_book_id_with_query_in_og_url() -> None:
    page = """
    <html><head><meta property="og:url"
      content="https://www.23qb.com/book/9268/?from=share"></head><body>
      <h1 class="page-title">凡人修仙传</h1>
    </body></html>
    """
    result = _parse(N23qbSearcher, page)[0]

    assert result["book_id"] == "9268"
    assert result["book_url"] == "https://www.23qb.com/book/9268/?from=share"


def test_qimao_relative_read_url_is_absolutized() -> None:
    payload = json.dumps(
        {
            "data": {
                "search_list": [
                    {
                        "book_id": "1784900",
                        "title": "书",
                        "read_url": "/shuku/1784900/?from=search",
                    }
                ]
            }
        },
        ensure_ascii=False,
    )
    result = _parse(QimaoSearcher, payload)[0]

    assert result["book_id"] == "1784900"
    assert result["book_url"] == "https://www.qimao.com/shuku/1784900/?from=search"


# =========================================================================== #
# Offline end-to-end chains: search -> book info + catalog -> first chapters.
#
# Each test replays one internally-consistent fixture set through the real
# search parser, fetcher URL building and book/chapter parsers.  ``fetch`` is
# monkeypatched with a URL -> fixture table, so no network is touched while the
# URL each stage asks for is still asserted.
# =========================================================================== #
_ANTI_BOT_SHELL = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><h1>Checking your browser before accessing</h1>"
    "<p>Enable JavaScript and cookies to continue.</p></body></html>"
)
_HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
_HTML_ENTITY_RE = re.compile(
    r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9a-fA-F]{1,6});"
)


def _install_routes(
    monkeypatch: pytest.MonkeyPatch, fetcher: Any, routes: dict[str, str]
) -> list[str]:
    """Serve ``fetcher.fetch`` from *routes* and return the requested URLs."""
    seen: list[str] = []

    async def fake_fetch(url: str, **kwargs: Any) -> str:
        seen.append(url)
        if url not in routes:
            raise AssertionError(f"unexpected fetch URL: {url}")
        return routes[url]

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    return seen


def _catalog_ids(info: Any) -> list[str]:
    return [c["chapterId"] for v in info["volumes"] for c in v["chapters"]]


def _assert_clean_chapter(chapter: Any, chapter_id: str) -> None:
    assert chapter is not None
    assert chapter["id"] == chapter_id
    assert chapter["title"].strip()
    content = chapter["content"]
    assert content.strip()
    # Neither raw markup nor surviving HTML entities may reach the exporter.
    assert not _HTML_TAG_RE.search(content), content
    assert not _HTML_ENTITY_RE.search(content), content


def _assert_unique(ids: list[str]) -> None:
    non_empty = [cid for cid in ids if cid]
    assert len(non_empty) == len(set(non_empty))


def _fetcher(cls: type[Any], tmp_path: Any) -> Any:
    return cls(FetcherConfig(cache_dir=str(tmp_path)))


# --------------------------------------------------------------------------- #
# b520
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_b520_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(B520Searcher, B520_SEARCH_HTML)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://www.bqg6.xyz/{book_id}/"

    fetcher = _fetcher(B520Fetcher, tmp_path)
    parser = B520Parser(parser_cfg)
    book_url = f"https://www.bqg6.xyz/{book_id}/"
    routes = {book_url: B520_BOOK_HTML}
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    ids = _catalog_ids(info)
    assert ids == ["746463", "746464"]
    _assert_unique(ids)

    chapter_urls = []
    for cid in ids:
        url = f"https://www.bqg6.xyz/{book_id}/{cid}.html"
        chapter_urls.append(url)
        routes[url] = B520_CHAPTER_HTML

    for cid in ids:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [book_url, *chapter_urls]

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL]) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "1") is None


# --------------------------------------------------------------------------- #
# qidian
# --------------------------------------------------------------------------- #
QIDIAN_CHAIN_BOOK_HTML = """
<html><body>
<h1 id="bookName">凡人修仙传</h1>
<a class="writer-name">忘语</a>
<a id="bookImg" data-bid="107580"></a>
<span class="update-time">更新时间：2026-01-01</span>
<p class="book-attribute"><span>连载</span></p>
<p class="count"><em>767.61万字</em></p>
<div id="allCatalog"><div class="catalog-volume">
  <h3 class="volume-name">正文卷·共3章</h3>
  <ul class="volume-chapters">
    <li><a class="chapter-name"
      href="/chapter/107580/9001/?from=catalog">第一章</a></li>
    <li><a class="chapter-name"
      href="/chapter/107580/9002/?from=catalog">第二章</a></li>
    <li><a class="chapter-name"
      href="/chapter/107580/9003/?from=catalog">第三章</a></li>
  </ul>
</div></div>
</body></html>
"""


@pytest.mark.asyncio
async def test_qidian_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(QidianSearcher, QIDIAN_SEARCH_HTML)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://www.qidian.com/book/{book_id}/"

    fetcher = _fetcher(QidianFetcher, tmp_path)
    parser = QidianParser(parser_cfg)
    book_url = f"https://www.qidian.com/book/{book_id}/"
    routes = {book_url: QIDIAN_CHAIN_BOOK_HTML}
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    assert info["cover_url"].endswith(f"/{book_id}/600.webp")
    ids = _catalog_ids(info)
    assert ids == ["9001", "9002", "9003"]
    _assert_unique(ids)

    chapter_urls = []
    for cid in ids[:2]:
        url = f"https://www.qidian.com/chapter/{book_id}/{cid}/"
        chapter_urls.append(url)
        routes[url] = _qidian_chapter_page(
            "<p>第一段。</p><p>第二段。</p>", title=f"第{cid}章", chapter_id=cid
        )

    for cid in ids[:2]:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [book_url, *chapter_urls]

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL]) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "1") is None


# --------------------------------------------------------------------------- #
# qimao
# --------------------------------------------------------------------------- #
QIMAO_CHAIN_DETAIL_JSON = json.dumps(
    {
        "data": {
            "book_detail": {
                "book_id": "1996719",
                "title": "凡人修仙：赠送机缘，暴击返还",
                "author": "凡人修仙",
                "image_link": "https://cdn.wtzw.com/bookimg/public/cover.jpg",
                "update_time": "2025-05-14 15:16:43",
                "words_num": "36.22",
                "is_over": "1",
                "category_1_name": "仙侠",
                "category_2_name": "现代修真",
                "latest_chapter_title": "第2章 二",
                "score": 9.6,
            }
        }
    },
    ensure_ascii=False,
)
QIMAO_CHAIN_INTRO_JSON = json.dumps(
    {"data": {"intro": "一个普通山村小子……"}}, ensure_ascii=False
)
QIMAO_CHAIN_CATALOG_JSON = json.dumps(
    {
        "data": {
            "chapters": [
                {"id": "1001", "title": "第1章 一"},
                {"id": "1002", "title": "第2章 二"},
            ]
        }
    },
    ensure_ascii=False,
)


def _qimao_reader_page(title: str, body_html: str) -> str:
    return (
        "<html><body>"
        '<div class="chapter-detail-wrap-info">'
        f'<h2 class="chapter-title">{title}</h2></div>'
        f'<script>window.__NUXT__={{chapterData:"{body_html}"}}</script>'
        "</body></html>"
    )


@pytest.mark.asyncio
async def test_qimao_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(QimaoSearcher, QIMAO_SEARCH_JSON)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://www.qimao.com/shuku/{book_id}/"

    fetcher = _fetcher(QimaoFetcher, tmp_path)
    parser = QimaoParser(parser_cfg)
    info_urls = [
        QimaoFetcher.BOOK_DETAIL_API,
        QimaoFetcher.BOOK_INTRO_API,
        QimaoFetcher.CATALOG_API,
    ]
    routes = dict(
        zip(
            info_urls,
            [
                QIMAO_CHAIN_DETAIL_JSON,
                QIMAO_CHAIN_INTRO_JSON,
                QIMAO_CHAIN_CATALOG_JSON,
            ],
            strict=True,
        )
    )
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    assert info["extra"]["bookId"] == book_id
    ids = _catalog_ids(info)
    assert ids == ["1001", "1002"]
    _assert_unique(ids)

    chapter_urls = []
    for cid in ids:
        url = f"https://www.qimao.com/shuku/{book_id}-{cid}/"
        chapter_urls.append(url)
        routes[url] = _qimao_reader_page(
            f"第{cid}章 标题", f"<p>第{cid}段。</p><p>第二段。</p>"
        )

    for cid in ids:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [*info_urls, *chapter_urls]

    assert parser.parse_book_info(["", "", ""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL] * 3) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "1") is None


# --------------------------------------------------------------------------- #
# qqbook
# --------------------------------------------------------------------------- #
QQBOOK_CHAIN_INFO_HTML = """
<html><head>
<meta property="og:novel:book_name" content="凡人修仙传">
<meta property="og:novel:author" content="忘语">
<meta property="og:novel:read_url"
  content="https://book.qq.com/book-detail/465030">
<meta property="og:image" content="https://img.example/cover.jpg">
<meta property="og:novel:update_time" content="2026-01-01">
<meta property="og:novel:status" content="已完结">
<meta property="og:description" content="一个普通山村小子……">
</head><body>
<div class="book-tags"><a class="tag">仙侠</a></div>
</body></html>
"""

QQBOOK_CHAIN_CATALOG_JSON = json.dumps(
    {
        "code": 0,
        "data": [
            {"cid": 101, "chapterName": "第1章 一", "free": 1},
            {"cid": 102, "chapterName": "第2章 二", "free": 1},
        ],
    },
    ensure_ascii=False,
)


def _qqbook_chapter_page(title: str, cid: str, body_html: str) -> str:
    payload = {
        "data": [
            {
                "chapterTitle": title,
                "cid": cid,
                "currentContent": {
                    "content": body_html,
                    "encrypt": False,
                    "fontEncrypt": False,
                    "updateTime": "",
                    "totalWords": "",
                },
                "fkConfig": {},
            }
        ]
    }
    return (
        "<html><body><script>window.__NUXT__=(function(){ return "
        + json.dumps(payload, ensure_ascii=False)
        + " }());</script></body></html>"
    )


@pytest.mark.asyncio
async def test_qqbook_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(QqbookSearcher, QQBOOK_SEARCH_JSON)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://book.qq.com/book-detail/{book_id}"

    fetcher = _fetcher(QqbookFetcher, tmp_path)
    parser = QqbookParser(parser_cfg)
    info_url = f"https://book.qq.com/book-detail/{book_id}"
    catalog_url = f"https://book.qq.com/api/book/detail/chapters?bid={book_id}"
    routes = {info_url: QQBOOK_CHAIN_INFO_HTML, catalog_url: QQBOOK_CHAIN_CATALOG_JSON}
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    ids = _catalog_ids(info)
    assert ids == ["101", "102"]
    _assert_unique(ids)
    for chapter in info["volumes"][0]["chapters"]:
        assert chapter["url"] == f"/book-read/{book_id}/{chapter['chapterId']}"

    chapter_urls = []
    for cid in ids:
        url = f"https://book.qq.com/book-read/{book_id}/{cid}/"
        chapter_urls.append(url)
        routes[url] = _qqbook_chapter_page(
            f"第{cid}章 标题", cid, f"<p>第{cid}段。</p><p>第二段。</p>"
        )

    for cid in ids:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [info_url, catalog_url, *chapter_urls]

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL, _ANTI_BOT_SHELL]) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "101") is None


# --------------------------------------------------------------------------- #
# shuhaige
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_shuhaige_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(ShuhaigeSearcher, SHUHAIGE_SEARCH_HTML)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://www.shuhaige.net/{book_id}/"

    fetcher = _fetcher(ShuhaigeFetcher, tmp_path)
    parser = ShuhaigeParser(parser_cfg)
    book_url = f"https://www.shuhaige.net/{book_id}/"
    routes = {book_url: SHUHAIGE_BOOK_HTML}
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    ids = _catalog_ids(info)
    assert ids == ["537331", "537332"]
    _assert_unique(ids)

    chapter_urls = []
    for cid in ids:
        url = f"https://www.shuhaige.net/{book_id}/{cid}.html"
        chapter_urls.append(url)
        routes[url] = SHUHAIGE_CHAPTER_PAGES[0]

    for cid in ids:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [book_url, *chapter_urls]

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL]) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "1") is None


# --------------------------------------------------------------------------- #
# quanben5
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_quanben5_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(Quanben5Searcher, QUANBEN5_SEARCH_JSONP)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://quanben5.com/n/{book_id}/"

    fetcher = _fetcher(Quanben5Fetcher, tmp_path)
    parser = Quanben5Parser(parser_cfg)
    book_url = f"https://quanben5.com/n/{book_id}/xiaoshuo.html"
    routes = {book_url: QUANBEN5_BOOK_HTML}
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    ids = _catalog_ids(info)
    assert ids == ["3682", "spell"]
    _assert_unique(ids)

    chapter_urls = []
    for cid in ids:
        url = f"https://quanben5.com/n/{book_id}/{cid}.html"
        chapter_urls.append(url)
        routes[url] = QUANBEN5_CHAPTER_HTML

    for cid in ids:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [book_url, *chapter_urls]

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL]) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "1") is None


# --------------------------------------------------------------------------- #
# biquge345
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_biquge345_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(Biquge345Searcher, BIQUGE345_SEARCH_HTML)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://www.biquge345.com/book/{book_id}/"

    fetcher = _fetcher(Biquge345Fetcher, tmp_path)
    parser = Biquge345Parser(parser_cfg)
    book_url = f"https://www.biquge345.com/book/{book_id}/"
    routes = {book_url: BIQUGE345_BOOK_HTML}
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    ids = _catalog_ids(info)
    assert ids == ["881234811", "091234811"]
    _assert_unique(ids)

    chapter_urls = []
    for cid in ids:
        url = f"https://www.biquge345.com/chapter/{book_id}/{cid}.html"
        chapter_urls.append(url)
        routes[url] = BIQUGE345_CHAPTER_HTML

    for cid in ids:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [book_url, *chapter_urls]

    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL]) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "1") is None


# --------------------------------------------------------------------------- #
# n23qb (the public search page is Cloudflare-challenged; the detail-page
# shape is the supported entry point, per the searcher's own detection)
# --------------------------------------------------------------------------- #
N23QB_CHAIN_CATALOG_HTML = """
<html><body><div class="box">
  <h2 class="module-title">正文</h2>
  <div class="module-row-info"><a class="module-row-text"
    href="/book/9268/7908999.html"><span>第一章 山边小村</span></a></div>
  <div class="module-row-info"><a class="module-row-text"
    href="/book/9268/7909000.html?from=catalog"><span>第二章 青牛镇</span></a></div>
  <div class="module-row-info"><a class="module-row-text"
    href="javascript:cid(0)"><span>VIP 章节</span></a></div>
</div></body></html>
"""


def _n23qb_chapter_page(title: str, text: str) -> str:
    return (
        "<html><body>"
        f'<h1 class="article-title">{title}</h1>'
        f'<div class="article-content"><p>{text}</p><p>第二段。</p></div>'
        '<div class="footer"><a class="f-right" '
        'href="/book/9268/7909001.html">下一页</a></div>'
        "</body></html>"
    )


@pytest.mark.asyncio
async def test_n23qb_chain_search_info_catalog_chapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, parser_cfg: ParserConfig
) -> None:
    hit = _parse(N23qbSearcher, N23QB_BOOK_HTML)[0]
    book_id = hit["book_id"]
    assert hit["book_url"] == f"https://www.23qb.com/book/{book_id}/"

    fetcher = _fetcher(N23qbFetcher, tmp_path)
    parser = N23qbParser(parser_cfg)
    info_url = f"https://www.23qb.com/book/{book_id}/"
    catalog_url = f"https://www.23qb.com/book/{book_id}/catalog"
    routes = {
        info_url: N23QB_BOOK_HTML,
        catalog_url: N23QB_CHAIN_CATALOG_HTML,
    }
    seen = _install_routes(monkeypatch, fetcher, routes)

    info = parser.parse_book_info(await fetcher.fetch_book_info(book_id))
    assert info is not None
    all_ids = _catalog_ids(info)
    assert all_ids == ["7908999", "7909000", ""]
    _assert_unique(all_ids)

    ids = [cid for cid in all_ids if cid]
    chapter_urls = []
    for cid in ids:
        url = f"https://www.23qb.com/book/{book_id}/{cid}.html"
        chapter_urls.append(url)
        routes[url] = _n23qb_chapter_page(f"第{cid}章", f"第{cid}段。")

    for cid in ids:
        chapter = parser.parse_chapter_content(
            await fetcher.fetch_chapter_content(book_id, cid), cid
        )
        _assert_clean_chapter(chapter, cid)

    assert seen == [info_url, catalog_url, *chapter_urls]

    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_book_info([_ANTI_BOT_SHELL, _ANTI_BOT_SHELL]) is None
    assert parser.parse_chapter_content([_ANTI_BOT_SHELL], "1") is None


# =========================================================================== #
# Field quality: labels, whitespace and URL absolutisation observed on the
# 2026-11 live pages.
# =========================================================================== #
def test_qidian_book_info_strips_fullwidth_update_label(
    parser_cfg: ParserConfig,
) -> None:
    # The live label uses a full-width colon ("更新时间：2026-01-01"); only
    # replacing the ASCII form left the whole label in ``update_time``.
    info = QidianParser(parser_cfg).parse_book_info([QIDIAN_CHAIN_BOOK_HTML])

    assert info is not None
    assert info["update_time"] == "2026-01-01"


def test_qimao_search_relative_cover_is_absolutized() -> None:
    payload = json.dumps(
        {
            "data": {
                "search_list": [
                    {
                        "book_id": "1784900",
                        "title": "书",
                        "image_link": "//cdn.wtzw.com/cover.jpg",
                    }
                ]
            }
        },
        ensure_ascii=False,
    )

    result = _parse(QimaoSearcher, payload)[0]

    assert result["cover_url"] == "https://cdn.wtzw.com/cover.jpg"


QIMAO_DETAIL_JSON = json.dumps(
    {
        "data": {
            "book_detail": {
                "book_id": "1996719",
                "title": "书",
                "author": "作者",
                "words_num": "36.22",
                "image_link": "/bookimg/cover.jpg",
                "update_time": "2025-01-01",
                "is_over": "1",
                "category_1_name": "玄幻",
            }
        }
    },
    ensure_ascii=False,
)
QIMAO_INTRO_JSON = json.dumps({"data": {"intro": "简介"}}, ensure_ascii=False)
QIMAO_CATALOG_JSON = json.dumps(
    {"data": {"chapters": [{"id": "1", "title": "第1章"}]}}, ensure_ascii=False
)


def test_qimao_book_info_absolutizes_root_relative_cover(
    parser_cfg: ParserConfig,
) -> None:
    info = QimaoParser(parser_cfg).parse_book_info(
        [QIMAO_DETAIL_JSON, QIMAO_INTRO_JSON, QIMAO_CATALOG_JSON]
    )

    assert info is not None
    assert info["cover_url"] == "https://www.qimao.com/bookimg/cover.jpg"
    assert info["word_count"] == "36.22万字"


def test_biquge345_summary_drops_label_and_template_padding(
    parser_cfg: ParserConfig,
) -> None:
    page = BIQUGE345_BOOK_HTML.replace(
        "简介：穿越凡人……",
        "简介：    穿越凡人……\n--完",
    )
    info = Biquge345Parser(parser_cfg).parse_book_info([page])

    assert info is not None
    assert info["summary"] == "穿越凡人…… --完"


def test_biquge345_cover_supports_protocol_relative_url(
    parser_cfg: ParserConfig,
) -> None:
    page = BIQUGE345_BOOK_HTML.replace(
        'src="/files/article/image/240/240541/240541s.jpg"',
        'src="//cdn.example.com/240541s.jpg"',
    )
    info = Biquge345Parser(parser_cfg).parse_book_info([page])

    assert info is not None
    assert info["cover_url"] == "https://cdn.example.com/240541s.jpg"


def test_n23qb_book_info_reads_update_time(parser_cfg: ParserConfig) -> None:
    # Same node the detail-page searcher reads; the book-info parser used to
    # emit an empty update_time unconditionally.
    info_html = N23QB_BOOK_HTML.replace(
        '<div class="novel-info-item novel-info-content">',
        '<div class="module-heading newchapter"><time>更新时间：2024-01-02</time>'
        '</div><div class="novel-info-item novel-info-content">',
    )
    info = N23qbParser(parser_cfg).parse_book_info([info_html, N23QB_CATALOG_HTML])

    assert info is not None
    assert info["update_time"] == "2024-01-02"
