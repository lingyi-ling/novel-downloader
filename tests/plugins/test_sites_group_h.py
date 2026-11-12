#!/usr/bin/env python3
"""Offline fixture tests for the group-H site plugins (rolling slot 6).

These are the remaining site modules that no earlier group exercised.  The
fixtures are trimmed synthetic copies of the markup described by each site's
``docs/supported-sites`` page, so every parser runs without network access.

The defects pinned here are the ones that would corrupt a download:

* ``guidaye`` / ``hongxiuzhao`` / ``lvsewx`` / ``mangg_com`` / ``tianyabooks``
  / ``twkan`` / ``wenku8`` / ``wxsck`` / ``xshbook`` / ``yamibo`` /
  ``zhenhunxiaoshuo`` raised ``ParserError`` on a blank or doctype-only page
  (a blocked/anti-bot response) instead of returning ``None``.
* ``guidaye`` reported a relative ``og:image`` and copied ``<script>`` text
  into the chapter body.
* ``mangg_com`` turned a missing cover into the site root and kept anchors
  without an ``href`` (empty ids in the catalogue).
* ``n69hao`` inherited an empty ``BASE_URL`` from the shared biquge3 parser, so
  root-relative covers stayed relative.
* ``lvsewx`` kept a cover without a leading slash relative and the fallback
  catalogue accepted empty ``href`` values.
* ``twkan`` built ``chapterId`` from the raw URL (``?from=`` leaked) and kept a
  relative cover.
* ``wenku8`` produced a path-shaped id when the TOC used absolute hrefs and
  left image resources relative.
* ``wxsck`` prefixed ``BASE_URL`` to a protocol-relative cover and kept empty
  catalogue links.
* ``xshbook`` kept a relative cover, emitted blank summary lines, lost the
  body of ``<br>``-only pages and let ``?from=`` leak into the search id
  (``95139-95139418-?from=...``).
* ``yamibo`` prefixed ``BASE_URL`` to an already absolute cover, produced
  ``[""]`` tags when no category was shown, took ``id=<x>&from=...`` whole as
  the chapter id and emitted empty volumes.
* ``zhenhunxiaoshuo`` dropped text nested inside ``<strong>`` in a paragraph.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.sites.blqudu.parser import BlquduParser
from novel_downloader.plugins.sites.guidaye.parser import GuidayeParser
from novel_downloader.plugins.sites.hongxiuzhao.parser import HongxiuzhaoParser
from novel_downloader.plugins.sites.lvsewx.parser import LvsewxParser
from novel_downloader.plugins.sites.mangg_com.parser import ManggComParser
from novel_downloader.plugins.sites.n69hao.parser import N69haoParser
from novel_downloader.plugins.sites.syosetu_org.parser import SyosetuOrgParser
from novel_downloader.plugins.sites.tianyabooks.parser import TianyabooksParser
from novel_downloader.plugins.sites.twkan.parser import TwkanParser
from novel_downloader.plugins.sites.wenku8.parser import Wenku8Parser
from novel_downloader.plugins.sites.wxsck.parser import WxsckParser
from novel_downloader.plugins.sites.xshbook.parser import XshbookParser
from novel_downloader.plugins.sites.xshbook.searcher import XshbookSearcher
from novel_downloader.plugins.sites.yamibo.parser import YamiboParser
from novel_downloader.plugins.sites.zhenhunxiaoshuo.parser import (
    ZhenhunxiaoshuoParser,
)
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


ALL_PARSERS = [
    BlquduParser,
    GuidayeParser,
    HongxiuzhaoParser,
    LvsewxParser,
    ManggComParser,
    N69haoParser,
    SyosetuOrgParser,
    TianyabooksParser,
    TwkanParser,
    Wenku8Parser,
    WxsckParser,
    XshbookParser,
    YamiboParser,
    ZhenhunxiaoshuoParser,
]


# --------------------------------------------------------------------------- #
# shared robustness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("parser_cls", ALL_PARSERS)
def test_blank_pages_degrade_to_none(parser_cls: type[Any], cfg: ParserConfig) -> None:
    """A blocked/empty response must not escape as ``ParserError``."""
    parser = parser_cls(cfg)

    assert parser.parse_book_info(["", "   "]) is None
    assert parser.parse_chapter_content([""], "1") is None


@pytest.mark.parametrize("parser_cls", ALL_PARSERS)
def test_doctype_only_pages_degrade_to_none(
    parser_cls: type[Any], cfg: ParserConfig
) -> None:
    parser = parser_cls(cfg)

    assert parser.parse_book_info(["<!DOCTYPE html>", "<!DOCTYPE html>"]) is None
    assert parser.parse_chapter_content(["<!DOCTYPE html>"], "1") is None


# --------------------------------------------------------------------------- #
# blqudu (shared biquge2 template)
# --------------------------------------------------------------------------- #
BLQUDU_INFO_HTML = """
<html><body>
<div id="info">
  <h1>凡人修仙传</h1>
  <p>作&nbsp;&nbsp;&nbsp;&nbsp;者：忘语</p>
  <p>状&nbsp;&nbsp;态：连载</p>
  <p>最后更新：2026-01-30</p>
</div>
<div id="sidebar"><img src="/img/137_137144.jpg"></div>
<div id="intro"><p>一个普通山村小子。</p><p>踏入修仙之路。</p></div>
<div class="listmain"><dl>
  <dt>《凡人修仙传》正文卷</dt>
  <dd><a href="/137_137144/628955328.html">第一章 山边小村</a></dd>
  <dd><a href="/137_137144/628955329.html">第二章 青牛镇</a></dd>
</dl></div>
</body></html>
"""

BLQUDU_CHAPTER_HTML = """
<div id="content" class="content">
<h1>第一章 山边小村</h1>
<p>　　二愣子睁大着双眼。</p>
<p>　　记住笔趣阁 biqudv.cc</p>
<p>　　这是一个小山村。</p>
</div>
"""


def test_blqudu_book_info(cfg: ParserConfig) -> None:
    info = BlquduParser(cfg).parse_book_info([BLQUDU_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "凡人修仙传"
    assert info["author"] == "忘语"
    assert info["serial_status"] == "连载"
    assert info["cover_url"] == "https://www.blqudu.cc/img/137_137144.jpg"
    assert info["summary"] == "一个普通山村小子。\n踏入修仙之路。"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["628955328", "628955329"]


def test_blqudu_chapter_drops_ads(cfg: ParserConfig) -> None:
    chapter = BlquduParser(cfg).parse_chapter_content(
        [BLQUDU_CHAPTER_HTML], "628955328"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 山边小村"
    assert "二愣子睁大着双眼。" in chapter["content"]
    assert "这是一个小山村。" in chapter["content"]
    assert "biqudv.cc" not in chapter["content"]


# --------------------------------------------------------------------------- #
# guidaye
# --------------------------------------------------------------------------- #
GUIDAYE_INFO_HTML = """
<html><head>
<meta property="og:novel:book_name" content="山河故人">
<meta property="og:novel:author" content="张三">
<meta property="og:image" content="/uploads/654.jpg">
<meta property="og:novel:update_time" content="2026-05-01 10:00:00">
<meta property="og:novel:category" content="历史">
<meta property="og:description" content="一个关于山河的故事。">
</head><body>
<ol id="list-ol">
  <li><a href="/kongbu/654/170737.html">第一章 归途</a></li>
  <li><a href="/kongbu/654/170738.html">第二章 远行</a></li>
  <li><a href="javascript:void(0)">广告位</a></li>
</ol>
</body></html>
"""

GUIDAYE_CHAPTER_HTML = """
<html><body><article class="article-post">
<h1 class="entry-title">第一章 归途</h1>
<p>第一段。</p>
<p><strong>第二段</strong>的结尾。</p>
<script>var ad = "不要我";</script>
</article></body></html>
"""


def test_guidaye_book_info_absolutizes_cover(cfg: ParserConfig) -> None:
    info = GuidayeParser(cfg).parse_book_info([GUIDAYE_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "山河故人"
    assert info["author"] == "张三"
    assert info["cover_url"] == "https://b.guidaye.com/uploads/654.jpg"
    assert info["tags"] == ["历史"]
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["170737", "170738"]
    assert chapters[0]["url"] == "/kongbu/654/170737.html"


def test_guidaye_chapter_keeps_inline_text_and_drops_script(
    cfg: ParserConfig,
) -> None:
    chapter = GuidayeParser(cfg).parse_chapter_content([GUIDAYE_CHAPTER_HTML], "170737")

    assert chapter is not None
    assert chapter["title"] == "第一章 归途"
    assert chapter["content"] == "第一段。\n第二段的结尾。"


# --------------------------------------------------------------------------- #
# hongxiuzhao
# --------------------------------------------------------------------------- #
HONGXIUZHAO_INFO_HTML = """
<html><body>
<div class="m-bookdetail"><h1>夜色温柔</h1></div>
<p class="author"><a href="/author/1">李四</a></p>
<a class="cover"><img src="//cdn.hongxiuzhao.net/cover/ZG6rmWO.jpg"></a>
<p class="summery">一段温柔的夜色。</p>
<section class="yd-chapter"><ul>
  <li><a href="/aBKBVz6a.html">第一章 初见</a></li>
  <li><a href="https://hongxiuzhao.net/aBKBVz6b.html?from=toc">第二章 重逢</a></li>
</ul></section>
</body></html>
"""

HONGXIUZHAO_CHAPTER_HTML = """
<div class="article-content">
<h1>第一章 初见</h1>
<p>正文第一段。</p>
<p>为防失联，请收藏永久网址。</p>
<p>正文第二段。</p>
</div>
"""


def test_hongxiuzhao_book_info(cfg: ParserConfig) -> None:
    info = HongxiuzhaoParser(cfg).parse_book_info([HONGXIUZHAO_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "夜色温柔"
    assert info["author"] == "李四"
    assert info["cover_url"] == "https://cdn.hongxiuzhao.net/cover/ZG6rmWO.jpg"
    chapters = info["volumes"][0]["chapters"]
    # The query string is not part of the id the fetcher rebuilds from.
    assert [c["chapterId"] for c in chapters] == ["aBKBVz6a", "aBKBVz6b"]


def test_hongxiuzhao_chapter_drops_ad_lines(cfg: ParserConfig) -> None:
    chapter = HongxiuzhaoParser(cfg).parse_chapter_content(
        [HONGXIUZHAO_CHAPTER_HTML], "aBKBVz6a"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 初见"
    assert chapter["content"] == "正文第一段。\n正文第二段。"


# --------------------------------------------------------------------------- #
# lvsewx
# --------------------------------------------------------------------------- #
LVSEWX_INFO_HTML = """
<html><head>
<meta property="og:novel:book_name" content="绿野仙踪">
<meta property="og:novel:author" content="王五">
<meta property="og:image" content="img/20645.jpg">
<meta property="og:novel:update_time" content="2026-02-02">
<meta property="og:novel:status" content="连载">
<meta property="og:novel:category" content="仙侠">
</head><body>
<div class="intro">简介：绿野之中的仙踪。作者：王五</div>
<div id="listmain"><dl>
  <dd><a href="/books/20/20645/7237914.html">第一章 入山</a></dd>
  <dd><a href="javascript:;">目录占位</a></dd>
</dl></div>
</body></html>
"""

LVSEWX_CHAPTER_HTML = """
<html><body>
<div class="content"><h1>第一章 入山</h1></div>
<div id="content">
  第一段文字。
  <script>var ad = 1;</script>
  <p>第二段文字。</p>
  www.lvscwx.cc
</div>
</body></html>
"""


def test_lvsewx_book_info_fallback_catalogue(cfg: ParserConfig) -> None:
    info = LvsewxParser(cfg).parse_book_info([LVSEWX_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "绿野仙踪"
    assert info["author"] == "王五"
    assert info["cover_url"] == "https://www.lvsewx.cc/img/20645.jpg"
    assert info["tags"] == ["仙侠"]
    chapters = info["volumes"][0]["chapters"]
    # The javascript placeholder must not become an empty chapter entry.
    assert [c["chapterId"] for c in chapters] == ["7237914"]


def test_lvsewx_chapter_skips_script_and_ads(cfg: ParserConfig) -> None:
    chapter = LvsewxParser(cfg).parse_chapter_content([LVSEWX_CHAPTER_HTML], "7237914")

    assert chapter is not None
    assert chapter["title"] == "第一章 入山"
    assert "第一段文字。" in chapter["content"]
    assert "第二段文字。" in chapter["content"]
    assert "lvscwx.cc" not in chapter["content"]
    assert "var ad" not in chapter["content"]


# --------------------------------------------------------------------------- #
# mangg_com
# --------------------------------------------------------------------------- #
MANGG_COM_INFO_HTML = """
<html><body>
<div id="info">
  <h1>追书人</h1>
  <p>作&nbsp;者：赵六</p>
  <p>状&nbsp;态：连载</p>
  <p>最后更新：2026-03-03</p>
</div>
<div id="intro">简介正文。</div>
<div id="list"><dl>
  <dd><a href="/id57715/632689.html">第一章 开篇</a></dd>
  <dd><a>缺少链接</a></dd>
</dl></div>
</body></html>
"""

MANGG_COM_CHAPTER_HTML = """
<html><body>
<div class="bookname"><h1>第一章 开篇</h1></div>
<div id="content">
  正文第一行。
  <p>正文第二行。</p>
  <script>var x = 1;</script>
</div>
</body></html>
"""


def test_mangg_com_missing_cover_stays_empty(cfg: ParserConfig) -> None:
    info = ManggComParser(cfg).parse_book_info([MANGG_COM_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == ""
    assert info["author"] == "赵六"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["632689"]


def test_mangg_com_chapter_content(cfg: ParserConfig) -> None:
    chapter = ManggComParser(cfg).parse_chapter_content(
        [MANGG_COM_CHAPTER_HTML], "632689"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 开篇"
    assert chapter["content"] == "正文第一行。\n正文第二行。"


# --------------------------------------------------------------------------- #
# n69hao (shared biquge3 template)
# --------------------------------------------------------------------------- #
N69HAO_INFO_HTML = """
<html><head>
<meta property="og:novel:book_name" content="六九书话">
<meta property="og:novel:author" content="孙七">
<meta property="og:image" content="/img/48.jpg">
<meta property="og:novel:update_time" content="2026-04-04">
<meta property="og:novel:status" content="完结">
<meta property="og:novel:category" content="都市">
<meta property="og:description" content="都市里的故事。">
</head><body>
<div id="list"><dl>
  <dt>最新章节</dt><dd><a href="/48/38642.html">第二章 发展</a></dd>
  <dt>全部章节</dt><dd><a href="/48/38641.html">第一章 起点</a></dd>
</dl></div>
</body></html>
"""


def test_n69hao_cover_is_absolute(cfg: ParserConfig) -> None:
    info = N69haoParser(cfg).parse_book_info([N69HAO_INFO_HTML])

    assert info is not None
    assert info["cover_url"] == "https://www.69hao.com/img/48.jpg"
    assert info["tags"] == ["都市"]
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["38641"]


# --------------------------------------------------------------------------- #
# syosetu_org
# --------------------------------------------------------------------------- #
SYOSETU_ORG_INFO_HTML = """
<html><body>
<div class="ss">
  <span itemprop="name">転生した話</span>
  <span itemprop="author"><a href="/novel/user/?id=1">作者名</a></span>
  <a class="alert_color" href="/novel/tag/1/">残酷な描写あり</a>
  <span itemprop="keywords"><a href="/novel/tag/2/">ファンタジー</a></span>
</div>
<div class="ss"><p>あらすじ本文。</p></div>
<div class="ss"><table>
  <tr><td><strong>第一章</strong></td></tr>
  <tr><td><a href="./1.html">第一話</a></td></tr>
  <tr><td><a href="/novel/292891/2.html?from=list">第二話</a></td></tr>
</table></div>
</body></html>
"""

SYOSETU_ORG_CHAPTER_HTML = """
<html><body>
<span style="font-size: 120%;">第一話</span>
<div id="maegaki">前書きです。</div>
<div id="honbun"><p>本文一。</p><p><strong>本文二</strong>。</p></div>
<div id="atogaki">後書きです。</div>
</body></html>
"""


def test_syosetu_org_book_info(cfg: ParserConfig) -> None:
    info = SyosetuOrgParser(cfg).parse_book_info([SYOSETU_ORG_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "転生した話"
    assert info["author"] == "作者名"
    assert info["tags"] == ["残酷な描写あり", "ファンタジー"]
    assert info["summary"] == "あらすじ本文。"
    volumes = info["volumes"]
    assert len(volumes) == 1
    assert volumes[0]["volume_name"] == "第一章"
    assert [c["chapterId"] for c in volumes[0]["chapters"]] == ["1", "2"]


def test_syosetu_org_chapter_content(cfg: ParserConfig) -> None:
    chapter = SyosetuOrgParser(cfg).parse_chapter_content(
        [SYOSETU_ORG_CHAPTER_HTML], "1"
    )

    assert chapter is not None
    assert chapter["title"] == "第一話"
    assert chapter["content"] == "前書きです。\n本文一。\n本文二。\n後書きです。"


def test_syosetu_org_chapter_without_p_wrappers(cfg: ParserConfig) -> None:
    html = '<div id="honbun">一行目<br>二行目</div>'

    chapter = SyosetuOrgParser(cfg).parse_chapter_content([html], "2")

    assert chapter is not None
    assert chapter["content"] == "一行目\n二行目"


# --------------------------------------------------------------------------- #
# tianyabooks
# --------------------------------------------------------------------------- #
TIANYABOOKS_INFO_HTML = """
<html><body>
<div class="catalog">
  <h1>长生界</h1>
  <h2><a href="/author/1">周八</a></h2>
  <div class="description"><p>长生之路。</p><p>由此开始。</p></div>
  <dl>
    <dt>第一卷</dt>
    <dd><a href="59453.html">第一章 开端</a></dd>
    <dd><a href="javascript:;">无效链接</a></dd>
    <dd><a href="59454.html">第二章 远行</a></dd>
  </dl>
</div>
</body></html>
"""

TIANYABOOKS_CHAPTER_HTML = """
<html><body>
<div class="article">
  <h2>第一章 开端</h2>
  <p>第一段。</p>
  <p>第二段。</p>
</div>
</body></html>
"""


def test_tianyabooks_book_info(cfg: ParserConfig) -> None:
    info = TianyabooksParser(cfg).parse_book_info([TIANYABOOKS_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "长生界"
    assert info["author"] == "周八"
    assert info["summary"] == "长生之路。\n由此开始。"
    volumes = info["volumes"]
    assert volumes[0]["volume_name"] == "第一卷"
    assert [c["chapterId"] for c in volumes[0]["chapters"]] == ["59453", "59454"]


def test_tianyabooks_chapter_content(cfg: ParserConfig) -> None:
    chapter = TianyabooksParser(cfg).parse_chapter_content(
        [TIANYABOOKS_CHAPTER_HTML], "59453"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 开端"
    assert chapter["content"] == "第一段。\n第二段。"


# --------------------------------------------------------------------------- #
# twkan
# --------------------------------------------------------------------------- #
TWKAN_INFO_HTML = """
<html><body>
<div class="booknav2">
  <h1><a href="/book/78813.html">台灣故事</a></h1>
  <p>作者：<a href="/author/1">陈九</a></p>
  <p>123.45万字 | 連載</p>
  <p>更新：2026-05-05</p>
</div>
<div class="bookimg2"><img src="/img/78813.jpg"></div>
<div class="navtxt"><p>一段台灣故事。</p></div>
</body></html>
"""

TWKAN_CATALOG_HTML = """
<html><body><ul>
  <li><a href="/txt/78813/47726772">第一章 起點</a></li>
  <li><a href="/txt/78813/47726773?from=list">第二章 遠行</a></li>
</ul></body></html>
"""

TWKAN_CHAPTER_HTML = """
<html><body>
<div class="txtnav"><h1>第一章 起點</h1></div>
<div id="txtcontent0">
  正文第一段。
  <div class="txtad">台灣小說網 twkan.com</div>
  <br>正文第二段。
</div>
</body></html>
"""


def test_twkan_book_info(cfg: ParserConfig) -> None:
    info = TwkanParser(cfg).parse_book_info([TWKAN_INFO_HTML, TWKAN_CATALOG_HTML])

    assert info is not None
    assert info["book_name"] == "台灣故事"
    assert info["author"] == "陈九"
    assert info["word_count"] == "123.45万字"
    assert info["serial_status"] == "連載"
    assert info["cover_url"] == "https://twkan.com/img/78813.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["47726772", "47726773"]


def test_twkan_chapter_skips_ad_blocks(cfg: ParserConfig) -> None:
    chapter = TwkanParser(cfg).parse_chapter_content([TWKAN_CHAPTER_HTML], "47726772")

    assert chapter is not None
    assert chapter["title"] == "第一章 起點"
    assert "正文第一段。" in chapter["content"]
    assert "正文第二段。" in chapter["content"]
    assert "twkan.com" not in chapter["content"]


# --------------------------------------------------------------------------- #
# wenku8
# --------------------------------------------------------------------------- #
WENKU8_INFO_HTML = """
<html><body><table>
  <tr><td><b>轻小说标题</b></td></tr>
</table>
<table><tr>
  <td>小说作者：李十</td>
  <td>文章状态：连载</td>
  <td>全文长度：123456字</td>
  <td>最后更新：2026-06-06</td>
</tr></table>
<img src="../image/2835.jpg">
<span>作品Tags：<b>作品Tags：科幻 冒险</b></span>
<span>内容简介</span><span>轻小说的简介。</span>
</body></html>
"""

WENKU8_CATALOG_HTML = """
<html><body><table class="css">
  <tr><td class="vcss">第一卷</td></tr>
  <tr><td class="ccss"><a href="/novel/2/2835/113354.htm">第一章</a></td></tr>
  <tr><td class="ccss"><a href="113355.htm">第二章</a></td></tr>
</table></body></html>
"""

WENKU8_CHAPTER_HTML = """
<html><body>
<div id="title">第一章</div>
<div id="content">
  <p>正文第一段。</p>
  <div class="divimage">
    <a href="../image/001.jpg"><img src="../image/001.jpg"></a>
  </div>
  <p>正文第二段。</p>
</div>
</body></html>
"""


def test_wenku8_book_info(cfg: ParserConfig) -> None:
    info = Wenku8Parser(cfg).parse_book_info([WENKU8_INFO_HTML, WENKU8_CATALOG_HTML])

    assert info is not None
    assert info["book_name"] == "轻小说标题"
    assert info["author"] == "李十"
    assert info["cover_url"] == "https://www.wenku8.net/image/2835.jpg"
    assert info["tags"] == ["科幻", "冒险"]
    volumes = info["volumes"]
    assert volumes[0]["volume_name"] == "第一卷"
    # ``/novel/2/2835/113354.htm`` must yield ``113354``, not a path.
    assert [c["chapterId"] for c in volumes[0]["chapters"]] == ["113354", "113355"]


def test_wenku8_resources_are_absolute(cfg: ParserConfig) -> None:
    chapter = Wenku8Parser(cfg).parse_chapter_content([WENKU8_CHAPTER_HTML], "113354")

    assert chapter is not None
    assert chapter["title"] == "第一章"
    assert chapter["content"] == "正文第一段。\n正文第二段。"
    resources = chapter["extra"]["resources"]
    assert [r["url"] for r in resources] == ["https://www.wenku8.net/image/001.jpg"]


# --------------------------------------------------------------------------- #
# wxsck
# --------------------------------------------------------------------------- #
WXSCK_INFO_HTML = """
<html><head>
<meta property="og:novel:book_name" content="万相之城">
<meta property="og:novel:author" content="周十一">
<meta property="og:novel:status" content="连载">
<meta property="og:novel:update_time" content="2026-07-07">
<meta property="og:image" content="//cdn.wxsck.com/cover/20297.jpg">
<meta property="og:novel:category" content="奇幻">
<meta property="og:description" content="万相之城的简介。">
</head><body>
<div class="book-detail">详情文本。</div>
<div id="all-chapter">
  <a href="/book/20297/6660334.html">第一章 进城</a>
  <a>缺少链接</a>
</div>
</body></html>
"""

WXSCK_CHAPTER_HTML = """
<html><body>
<h1 class="cont-title">第一章 进城</h1>
<div id="cont-body"><p>第一页第一段。</p><p>第一页第二段。</p></div>
</body></html>
"""


def test_wxsck_book_info(cfg: ParserConfig) -> None:
    info = WxsckParser(cfg).parse_book_info([WXSCK_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "万相之城"
    assert info["author"] == "周十一"
    # A protocol-relative cover must not become ``https://wxsck.com//cdn...``.
    assert info["cover_url"] == "https://cdn.wxsck.com/cover/20297.jpg"
    assert info["summary"] == "详情文本。"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["6660334"]


def test_wxsck_chapter_pages(cfg: ParserConfig) -> None:
    chapter = WxsckParser(cfg).parse_chapter_content(
        [WXSCK_CHAPTER_HTML, WXSCK_CHAPTER_HTML], "6660334"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 进城"
    assert chapter["content"] == (
        "第一页第一段。\n第一页第二段。\n第一页第一段。\n第一页第二段。"
    )


# --------------------------------------------------------------------------- #
# xshbook
# --------------------------------------------------------------------------- #
XSHBOOK_INFO_HTML = """
<html><head>
<meta property="og:novel:update_time" content="2026-08-08">
</head><body>
<div id="info"><h1>小说虎的故事</h1><p>作&nbsp;&nbsp;者:吴十二</p></div>
<div id="intro"><p>简介第一段。</p><p></p><p>简介第二段。</p></div>
<div id="fmimg"><img src="/img/95139.jpg"></div>
<div class="con_top"><a href="/">首页</a><a href="/xuanhuan/">玄幻</a></div>
<div id="list"><dl>
  <dt>正文</dt>
  <dd><a href="/95139/95139418/407988281.html">第一章 出发</a></dd>
  <dd><a href="/95139/95139418/407988282.html">第二章 抵达</a></dd>
</dl></div>
</body></html>
"""

XSHBOOK_CHAPTER_HTML = """
<html><body>
<div class="bookname"><h1>第一章 出发</h1></div>
<div id="content">
  <p>正文一。本文搜：xshbook.com 免费阅读</p>
  <p>正文二。</p>
  <p>温馨提示</p>
</div>
</body></html>
"""


def test_xshbook_book_info(cfg: ParserConfig) -> None:
    info = XshbookParser(cfg).parse_book_info([XSHBOOK_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "小说虎的故事"
    assert info["author"] == "吴十二"
    assert info["cover_url"] == "https://www.xshbook.com/img/95139.jpg"
    # Empty <p> elements must not produce blank lines.
    assert info["summary"] == "简介第一段。\n简介第二段。"
    assert info["tags"] == ["玄幻"]
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["407988281", "407988282"]


def test_xshbook_chapter_strips_inline_ad(cfg: ParserConfig) -> None:
    chapter = XshbookParser(cfg).parse_chapter_content(
        [XSHBOOK_CHAPTER_HTML], "407988281"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章 出发"
    assert chapter["content"] == "正文一。\n正文二。"


def test_xshbook_chapter_br_only_body(cfg: ParserConfig) -> None:
    html = '<div id="content">第一行<br>第二行<br>第三行</div>'

    chapter = XshbookParser(cfg).parse_chapter_content([html], "1")

    assert chapter is not None
    assert chapter["content"] == "第一行\n第二行\n第三行"


def test_xshbook_searcher_strips_query_from_book_id() -> None:
    page = """
    <div class="so_list"><div class="hot">
      <div class="item"><dl>
        <dt><a href="https://www.xshbook.com/95139/95139418/?from=search">小说虎的故事</a>
        <span>吴十二</span></dt>
      </dl><div class="image"><img src="/img/95139.jpg"></div></div>
      <div class="item"><dl>
        <dt><a href="https://www.xshbook.com/95139/95139419/">第二本</a>
        <span>郑十三</span></dt>
      </dl><div class="image"><img src="/img/95140.jpg"></div></div>
      <div class="item"><dl>
        <dt><a href="https://www.xshbook.com/95139/95139420/">第三本</a>
        <span>王十四</span></dt>
      </dl><div class="image"><img src="/img/95141.jpg"></div></div>
    </div></div>
    """

    results = XshbookSearcher(None)._parse_html(page, limit=2)  # type: ignore[arg-type]

    assert [r["book_id"] for r in results] == ["95139-95139418", "95139-95139419"]
    assert len(results) == 2
    assert results[0]["cover_url"] == "https://www.xshbook.com/img/95139.jpg"


# --------------------------------------------------------------------------- #
# yamibo
# --------------------------------------------------------------------------- #
YAMIBO_INFO_HTML = """
<html><body>
<h3 class="col-md-12">百合的故事</h3>
<h5 class="text-warning">作者：冯十五</h5>
<img class="img-responsive" src="https://cdn.yamibo.com/cover/262117.jpg">
<p>更新时间：2026-09-09</p>
<p>作品状态：连载</p>
<p>作品分类：百合</p>
<div id="w0-collapse1"><div>这是一段简介。</div></div>
<div class="panel-info panel-default">
  <div class="panel-heading"><a>第一卷</a></div>
  <div class="panel-body">
    <a href="/novel/view-chapter?id=38772952">第一章</a>
    <a href="/novel/view-chapter?id=38772953&amp;from=list">第二章</a>
  </div>
</div>
<div class="panel-info panel-default">
  <div class="panel-heading"><a>空卷</a></div>
  <div class="panel-body"></div>
</div>
</body></html>
"""

YAMIBO_INFO_NO_CATEGORY_HTML = """
<html><body>
<h3 class="col-md-12">没有分类</h3>
<div class="panel-body">
  <a href="/novel/view-chapter?id=1">第一章</a>
</div>
</body></html>
"""

YAMIBO_CHAPTER_HTML = """
<html><body>
<section class="col-md-9"><h3>第一章</h3></section>
<div id="w0-collapse1"><p>正文第一段。</p><p>正文第二段。</p></div>
</body></html>
"""


def test_yamibo_book_info(cfg: ParserConfig) -> None:
    info = YamiboParser(cfg).parse_book_info([YAMIBO_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "百合的故事"
    assert info["author"] == "作者：冯十五"
    # An absolute cover must not be prefixed with BASE_URL again.
    assert info["cover_url"] == "https://cdn.yamibo.com/cover/262117.jpg"
    assert info["tags"] == ["百合"]
    volumes = info["volumes"]
    # The empty second volume is not a downloadable volume.
    assert [v["volume_name"] for v in volumes] == ["第一卷"]
    chapters = volumes[0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["38772952", "38772953"]


def test_yamibo_tags_empty_when_category_missing(cfg: ParserConfig) -> None:
    info = YamiboParser(cfg).parse_book_info([YAMIBO_INFO_NO_CATEGORY_HTML])

    assert info is not None
    assert info["tags"] == []
    assert info["cover_url"] == ""
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["1"]


def test_yamibo_chapter_content(cfg: ParserConfig) -> None:
    chapter = YamiboParser(cfg).parse_chapter_content([YAMIBO_CHAPTER_HTML], "38772952")

    assert chapter is not None
    assert chapter["title"] == "第一章"
    assert chapter["content"] == "正文第一段。\n正文第二段。"


# --------------------------------------------------------------------------- #
# zhenhunxiaoshuo
# --------------------------------------------------------------------------- #
ZHENHUN_INFO_HTML = """
<html><body>
<h1 class="focusbox-title">镇魂</h1>
<div class="focusbox-text"><p class="text">镇魂的故事。</p></div>
<div class="excerpts"><article>
  <a href="/5419.html">第一章</a>
  <a href="/5420.html">第二章</a>
</article></div>
</body></html>
"""

ZHENHUN_CHAPTER_HTML = """
<html><body>
<header class="article-header"><h1 class="article-title">第一章</h1></header>
<article class="article-content">
  <p>第一段。</p>
  <p><strong>加粗的</strong>第二段。</p>
</article>
</body></html>
"""


def test_zhenhunxiaoshuo_book_info(cfg: ParserConfig) -> None:
    info = ZhenhunxiaoshuoParser(cfg).parse_book_info([ZHENHUN_INFO_HTML])

    assert info is not None
    assert info["book_name"] == "镇魂"
    assert info["summary"] == "镇魂的故事。"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["5419", "5420"]


def test_zhenhunxiaoshuo_chapter_keeps_nested_text(cfg: ParserConfig) -> None:
    chapter = ZhenhunxiaoshuoParser(cfg).parse_chapter_content(
        [ZHENHUN_CHAPTER_HTML], "5419"
    )

    assert chapter is not None
    assert chapter["title"] == "第一章"
    assert chapter["content"] == "第一段。\n加粗的第二段。"
