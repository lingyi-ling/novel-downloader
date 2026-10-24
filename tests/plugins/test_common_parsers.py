#!/usr/bin/env python3
"""Offline tests for the biquge-style common parsers.

Every fixture is an inline HTML fragment modelled on the real site templates
(og: meta pages, ``#info``/``.listmain`` pages, ``#content > #booktxt > p``
chapter pages and ``#novelcontent`` mobile pages).  No network access is used.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
    dedupe_chapters,
    resolve_url,
    strip_label,
    strip_page_marker,
)
from novel_downloader.plugins.common.parser.biquge1 import Biquge1Parser
from novel_downloader.plugins.common.parser.biquge2 import Biquge2Parser
from novel_downloader.plugins.common.parser.biquge3 import Biquge3Parser
from novel_downloader.plugins.common.parser.biquge4 import Biquge4Parser
from novel_downloader.schemas import ParserConfig


class _Biquge1(Biquge1Parser):
    site_name = "biquge1"


class _Biquge2(Biquge2Parser):
    site_name = "biquge2"
    BASE_URL = "https://www.example.com"
    ADS = {r"biquge\.com", "本站广告"}


class _Biquge3(Biquge3Parser):
    site_name = "biquge3"


class _Biquge4(Biquge4Parser):
    site_name = "biquge4"
    BASE_URL = "https://m.example.com"


_ALL_PARSERS = [_Biquge1, _Biquge2, _Biquge3, _Biquge4]


def _config() -> ParserConfig:
    return ParserConfig(enable_ocr=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def test_chapter_id_from_href_strips_query_and_suffix() -> None:
    assert chapter_id_from_href("/book/1/98765.html") == "98765"
    assert chapter_id_from_href("/book/1/98765.html?from=list") == "98765"
    assert chapter_id_from_href("/book/1/98765.html#top") == "98765"
    assert chapter_id_from_href("/book/1/") == "1"
    assert chapter_id_from_href("/") == ""
    assert chapter_id_from_href("") == ""


def test_resolve_url_handles_every_relative_shape() -> None:
    class _Tree:
        def __init__(self, og_url: str = "") -> None:
            self._og_url = og_url

        def xpath(self, query: str) -> list[str]:
            if "og:url" in query and self._og_url:
                return [self._og_url]
            return []

    tree = _Tree("//www.example.com/book/1/")
    assert resolve_url(tree, "https://cdn/x.jpg") == "https://cdn/x.jpg"
    assert resolve_url(tree, "//cdn/x.jpg") == "https://cdn/x.jpg"
    assert resolve_url(tree, "/img/x.jpg") == "https://www.example.com/img/x.jpg"
    assert resolve_url(tree, "") == ""
    # No base at all must never yield a malformed "https:/img/x.jpg".
    assert resolve_url(_Tree(), "/img/x.jpg") == "/img/x.jpg"


def test_strip_page_marker_handles_full_and_half_width() -> None:
    assert strip_page_marker("第一章 （1/2）") == "第一章"
    assert strip_page_marker("第一章 (1 / 6)") == "第一章"
    assert strip_page_marker("第一章（上）") == "第一章（上）"


def test_strip_label_tolerates_padding_inside_the_label() -> None:
    assert strip_label("作\xa0\xa0\xa0\xa0者：猫腻", "作者") == "猫腻"
    assert strip_label("状\u3000态: 完结", "状态") == "完结"
    assert strip_label("最后更新：2026-01-01", "最后更新") == "2026-01-01"
    assert strip_label("没有标签", "作者") == "没有标签"


def test_dedupe_chapters_keeps_first_occurrence() -> None:
    chapters = [
        {"title": "第一章", "url": "/1.html", "chapterId": "1"},
        {"title": "重复", "url": "/1.html", "chapterId": "1"},
        {"title": "第二章", "url": "/2.html", "chapterId": "2"},
    ]

    assert dedupe_chapters(chapters) == [chapters[0], chapters[2]]


# --------------------------------------------------------------------------- #
# shared failure shapes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("parser_cls", _ALL_PARSERS)
@pytest.mark.parametrize("pages", [[], [""], ["   "], ["\n\t"]])
def test_blank_pages_return_none(parser_cls: Any, pages: list[str]) -> None:
    parser = parser_cls(_config())

    assert parser.parse_book_info(pages) is None
    assert parser.parse_chapter_content(pages, chapter_id="1") is None


@pytest.mark.parametrize("parser_cls", _ALL_PARSERS)
def test_no_chapter_list_returns_none(parser_cls: Any) -> None:
    parser = parser_cls(_config())

    assert parser.parse_book_info(["<html><body><h1>空目录</h1></body></html>"]) is None


# --------------------------------------------------------------------------- #
# biquge1 (og: meta + .book_list2 + <article>)
# --------------------------------------------------------------------------- #
_BIQUGE1_INFO = """
<html><head>
  <meta property="og:novel:book_name" content="书名 &amp; 卷一"/>
  <meta property="og:novel:author" content="作者甲"/>
  <meta property="og:image" content="//cdn.example.com/cover.jpg"/>
  <meta property="og:novel:update_time" content="2026-01-02 03:04:05"/>
  <meta property="og:novel:status" content="连载"/>
  <meta property="og:novel:category" content="玄幻"/>
  <meta property="og:description" content="简介&nbsp;第一段"/>
  <meta property="og:url" content="//www.example.com/book/1/"/>
</head><body>
  <div class="book_list book_list2">
    <a href="/book/1/10.html"><span>第一章</span></a>
  </div>
</body></html>
"""


def test_biquge1_book_info_full() -> None:
    parser = _Biquge1(_config())

    info = parser.parse_book_info([_BIQUGE1_INFO])

    assert info is not None
    assert info["book_name"] == "书名 & 卷一"
    assert info["author"] == "作者甲"
    assert info["cover_url"] == "https://cdn.example.com/cover.jpg"
    assert info["update_time"] == "2026-01-02 03:04:05"
    assert info["serial_status"] == "连载"
    assert info["tags"] == ["玄幻"]
    assert info["summary"] == "简介第一段"
    chapter = info["volumes"][0]["chapters"][0]
    assert chapter == {"title": "第一章", "url": "/book/1/10.html", "chapterId": "10"}


def test_biquge1_book_info_combines_paginated_catalogues() -> None:
    parser = _Biquge1(_config())
    page_two = """
    <html><body><div class="book_list2">
      <a href="/book/1/10.html">第一章重复</a>
      <a href="/book/1/11.html">第二章</a>
    </div></body></html>
    """

    info = parser.parse_book_info([_BIQUGE1_INFO, page_two])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["10", "11"]


def test_biquge1_book_info_missing_metadata_and_summary_fallback() -> None:
    parser = _Biquge1(_config())
    page = """
    <html><body>
      <div id="intro_pc">简介第一行<br/>简介第二行</div>
      <div class="book_list2"><a href="/b/1.html">第一章</a></div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert info["book_name"] == ""
    assert info["author"] == ""
    assert info["cover_url"] == ""
    assert info["tags"] == []
    assert info["summary"] == "简介第一行\n简介第二行"


def test_biquge1_cover_resolves_root_relative_against_page_url() -> None:
    parser = _Biquge1(_config())
    page = """
    <html><head>
      <meta property="og:image" content="/img/cover.jpg"/>
      <meta property="og:url" content="https://www.example.com/book/1/"/>
    </head><body>
      <div class="book_list2"><a href="/b/1.html">第一章</a></div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert info["cover_url"] == "https://www.example.com/img/cover.jpg"


_BIQUGE1_CHAPTER = """
<html><head><title>第一章</title></head><body>
  <h1>《书名》第一章 起风了 — 《书名》</h1>
  <article>
    第一段 &amp; &#x4E2D; 文<br/>
    第二段<span>第(1/2)页</span>
    <script>alert('script')</script>
    乱码\ufffd\u200b保留
  </article>
</body></html>
"""


def test_biquge1_chapter_content_and_title_cleanup() -> None:
    parser = _Biquge1(_config())

    chapter = parser.parse_chapter_content([_BIQUGE1_CHAPTER], chapter_id="10")

    assert chapter is not None
    assert chapter["title"] == "第一章 起风了"
    assert chapter["content"].splitlines() == [
        "第一段 & 中 文",
        "第二段",
        "乱码\ufffd\u200b保留",
    ]


def test_biquge1_chapter_nested_ad_lines_are_removed() -> None:
    parser = _Biquge1(_config())
    page = """
    <html><body>
      <article>正文一<div class="ad">第(2/2)页</div>正文二</article>
    </body></html>
    """

    chapter = parser.parse_chapter_content([page], chapter_id="1")

    assert chapter is not None
    assert chapter["content"] == "正文一\n正文二"


def test_biquge1_chapter_combines_paginated_pages() -> None:
    parser = _Biquge1(_config())
    second = "<html><body><article>续页正文</article></body></html>"

    chapter = parser.parse_chapter_content([_BIQUGE1_CHAPTER, second], chapter_id="10")

    assert chapter is not None
    assert chapter["content"].endswith("续页正文")


def test_biquge1_chapter_without_article_returns_none() -> None:
    parser = _Biquge1(_config())

    assert (
        parser.parse_chapter_content(
            ["<html><body><h1>空章节</h1></body></html>"], chapter_id="1"
        )
        is None
    )


# --------------------------------------------------------------------------- #
# biquge2 (#info + .listmain + #content)
# --------------------------------------------------------------------------- #
_BIQUGE2_INFO = """
<html><body>
  <div id="info">
    <h1>书名乙</h1>
    <p>作&nbsp;&nbsp;者：作者乙</p>
    <p>状&nbsp;态：完结</p>
    <p>最后更新：2026-02-03</p>
  </div>
  <div id="sidebar"><img src="//cdn.example.com/b2.jpg"/></div>
  <div id="intro"><p>简介一</p><p>简介二</p></div>
  <div class="listmain"><dl>
    <dt>《书名乙》最新章节</dt>
    <dd><a href="/b/9.html">最新</a></dd>
    <dt>《书名乙》正文卷</dt>
    <dd><a href="/b/1.html"><span>第一章</span></a></dd>
    <dd><a href="/b/2.html">第二章</a></dd>
    <dd>无链接的说明</dd>
    <dt>《书名乙》正文卷二</dt>
    <dd><a href="/b/3.html">第三章</a></dd>
    <dd><a href="javascript:;">广告</a></dd>
    <dd><a href="">空链接</a></dd>
  </dl></div>
</body></html>
"""


def test_biquge2_book_info_multiple_sections() -> None:
    parser = _Biquge2(_config())

    info = parser.parse_book_info([_BIQUGE2_INFO])

    assert info is not None
    assert info["book_name"] == "书名乙"
    assert info["author"] == "作者乙"
    assert info["serial_status"] == "完结"
    assert info["update_time"] == "2026-02-03"
    assert info["cover_url"] == "https://cdn.example.com/b2.jpg"
    assert info["summary"] == "简介一\n简介二"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1", "2", "3"]
    assert chapters[0]["title"] == "第一章"
    assert chapters[0]["url"] == "https://www.example.com/b/1.html"


def test_biquge2_book_info_without_section_label_falls_back_to_all_dds() -> None:
    parser = _Biquge2(_config())
    page = """
    <html><body>
      <div id="info"><h1>书名</h1></div>
      <div class="listmain"><dl>
        <dt>最新章节</dt>
        <dd><a href="/b/1.html">第一章</a></dd>
        <dd><a href="/b/2.html">第二章</a></dd>
      </dl></div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["1", "2"]


def test_biquge2_missing_cover_does_not_become_the_site_root() -> None:
    parser = _Biquge2(_config())
    page = """
    <html><body>
      <div id="info"><h1>书名</h1></div>
      <div class="listmain"><dl>
        <dt>正文</dt><dd><a href="/b/1.html">第一章</a></dd>
      </dl></div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert info["cover_url"] == ""
    assert info["author"] == ""
    assert info["serial_status"] == ""


_BIQUGE2_CHAPTER = """
<html><body>
  <div class="content"><h1>第一章（1/2）</h1></div>
  <div id="content">
    第一段&nbsp;&nbsp;文字
    <script>bad()</script>
    <div>请访问 biquge.com</div>
    第二段\ufffd乱码
  </div>
</body></html>
"""


def test_biquge2_chapter_content_title_and_ads() -> None:
    parser = _Biquge2(_config())

    chapter = parser.parse_chapter_content([_BIQUGE2_CHAPTER], chapter_id="1")

    assert chapter is not None
    assert chapter["title"] == "第一章"
    assert chapter["content"].splitlines() == ["第一段文字", "第二段\ufffd乱码"]


def test_biquge2_chapter_without_content_returns_none() -> None:
    parser = _Biquge2(_config())
    page = "<html><body><div class='content'><h1>空</h1></div></body></html>"

    assert parser.parse_chapter_content([page], chapter_id="1") is None


# --------------------------------------------------------------------------- #
# biquge3 (og: meta + #list direct <a><dd> + #content/#booktxt)
# --------------------------------------------------------------------------- #
_BIQUGE3_INFO = """
<html><head>
  <meta property="og:novel:book_name" content="书名丙"/>
  <meta property="og:novel:author" content="作者丙"/>
  <meta property="og:image" content="/img/c3.jpg"/>
  <meta property="og:url" content="https://www.example.com/du/1/2/"/>
  <meta property="og:novel:update_time" content="2026-03-03"/>
  <meta property="og:novel:status" content="连载"/>
  <meta property="og:novel:category" content="仙侠"/>
  <meta property="og:description" content="简介丙"/>
</head><body>
  <div id="list"><dl>
    <dt>书名丙最新章节</dt>
    <a href="/du/1/2/9.html"><dd>最新</dd></a>
    <dt>书名丙目录章节</dt>
    <a href="/du/1/2/1.html"><dd><span>第一章</span></dd></a>
    <a href="/du/1/2/2.html"><dd>第二章</dd></a>
    <a href="javascript:;"><dd>广告</dd></a>
    <a href=""><dd>空链接</dd></a>
    <dt>第二卷</dt>
    <a href="/du/1/2/3.html"><dd>第三章</dd></a>
    <a href="/du/1/2/1.html"><dd>第一章重复</dd></a>
  </dl></div>
</body></html>
"""


def test_biquge3_book_info_chapters_and_cover() -> None:
    parser = _Biquge3(_config())

    info = parser.parse_book_info([_BIQUGE3_INFO])

    assert info is not None
    assert info["book_name"] == "书名丙"
    assert info["author"] == "作者丙"
    assert info["cover_url"] == "https://www.example.com/img/c3.jpg"
    assert info["tags"] == ["仙侠"]
    assert info["summary"] == "简介丙"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1", "2", "3"]
    assert chapters[0]["title"] == "第一章"


def test_biquge3_variant_with_dd_wrapped_anchors() -> None:
    parser = _Biquge3(_config())
    page = """
    <html><body>
      <div id="list"><dl>
        <dt>最新章节</dt><dd><a href="/du/9.html">最新</a></dd>
        <dt>目录章节</dt>
        <dd><a href="/du/1.html"><span>第一章</span></a></dd>
        <dd><a href="/du/2.html">第二章</a></dd>
      </dl></div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["1", "2"]


def test_biquge3_variant_with_single_dt_and_rel_chapter() -> None:
    parser = _Biquge3(_config())
    page = """
    <html><body>
      <div id="list"><dl>
        <dt>目录</dt>
        <a href="/du/1.html" rel="chapter"><dd>第一章</dd></a>
        <a href="/du/2.html" rel="chapter"><dd>第二章</dd></a>
      </dl></div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["1", "2"]


_BIQUGE3_CHAPTER = """
<html><body>
  <h1 class="bookname">第一章 起风（1/2）</h1>
  <div id="content"><div id="booktxt">
    <p>第一段 &amp; 实体</p>
    <p>第二段<script>bad()</script></p>
    <p>本章未完，点击下一页继续阅读。</p>
    <p>乱码\ufffd保留</p>
  </div></div>
</body></html>
"""


def test_biquge3_chapter_content_full_width_marker_and_ads() -> None:
    parser = _Biquge3(_config())

    chapter = parser.parse_chapter_content([_BIQUGE3_CHAPTER], chapter_id="1")

    assert chapter is not None
    assert chapter["title"] == "第一章 起风"
    assert chapter["content"].splitlines() == [
        "第一段 & 实体",
        "第二段",
        "乱码\ufffd保留",
    ]


def test_biquge3_chapter_variant_without_paragraph_tags() -> None:
    parser = _Biquge3(_config())
    page = """
    <html><body>
      <h1 class="bookname">第一章</h1>
      <div id="content">第一段<br/><br/>第二段</div>
    </body></html>
    """

    chapter = parser.parse_chapter_content([page], chapter_id="1")

    assert chapter is not None
    assert chapter["content"] == "第一段\n第二段"


def test_biquge3_chapter_combines_paginated_pages() -> None:
    parser = _Biquge3(_config())
    second = """
    <html><body>
      <h1 class="bookname">第一章（2/2）</h1>
      <div id="content"><div id="booktxt"><p>续页正文</p></div></div>
    </body></html>
    """

    chapter = parser.parse_chapter_content([_BIQUGE3_CHAPTER, second], chapter_id="1")

    assert chapter is not None
    assert chapter["title"] == "第一章 起风"
    assert chapter["content"].endswith("续页正文")


# --------------------------------------------------------------------------- #
# biquge4 (#novelcontent + .info_chapters)
# --------------------------------------------------------------------------- #
_BIQUGE4_INFO = """
<html><body>
  <div class="catalog1">
    <h1>书名丁</h1>
    <div class="tu"><img src="/img/c4.jpg"/></div>
    <p class="p1">作者：作者丁</p>
    <p class="p2">更新：2026-04-05</p>
  </div>
  <div class="jj">简介丁第一行<br/>简介丁第二行</div>
  <div class="info_chapters">
    <p class="p1">最新章节</p>
    <ul class="p2"><li><a href="/read/9.html">最新</a></li></ul>
    <a name="all"></a>
    <p class="p1">全部章节 ( 书名丁 )</p>
    <ul class="p2">
      <li><a href="/read/1.html">第一章</a></li>
      <li><a href="/read/2.html">第二章</a></li>
    </ul>
  </div>
</body></html>
"""


def test_biquge4_book_info_full_and_only_all_chapters() -> None:
    parser = _Biquge4(_config())

    info = parser.parse_book_info([_BIQUGE4_INFO])

    assert info is not None
    assert info["book_name"] == "书名丁"
    assert info["author"] == "作者丁"
    assert info["cover_url"] == "https://m.example.com/img/c4.jpg"
    assert info["update_time"] == "2026-04-05"
    assert info["summary"].startswith("简介丁第一行")
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1", "2"]


def test_biquge4_first_child_ul_does_not_crash_and_falls_back() -> None:
    parser = _Biquge4(_config())
    page = """
    <html><body>
      <div class="catalog1"><h1>书名</h1></div>
      <div class="info_chapters">
        <ul class="p2"><li><a href="/read/1.html">第一章</a></li></ul>
      </div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert [c["chapterId"] for c in info["volumes"][0]["chapters"]] == ["1"]


def test_biquge4_missing_cover_and_metadata() -> None:
    parser = _Biquge4(_config())
    page = """
    <html><body>
      <div class="catalog1"><h1>书名</h1></div>
      <div class="info_chapters">
        <p class="p1">全部章节</p>
        <ul class="p2"><li><a href="/read/1.html">第一章</a></li></ul>
      </div>
    </body></html>
    """

    info = parser.parse_book_info([page])

    assert info is not None
    assert info["book_name"] == "书名"
    assert info["author"] == ""
    assert info["cover_url"] == ""
    assert info["update_time"] == ""


_BIQUGE4_CHAPTER = """
<html><body>
  <h1>第一章 起风 (1 / 6)</h1>
  <div id="novelcontent" class="novelcontent">
    <p><font>《关闭小说畅读模式体验更好》</font></p>
    第一段&amp;实体<br/><br/>
    <div class="ad">内容未完，下一页继续阅读</div>
    第二段&#x4E2D; 文
    <p>第三段 &lt;b&gt;</p>
  </div>
</body></html>
"""


def test_biquge4_chapter_keeps_text_after_nested_ad() -> None:
    parser = _Biquge4(_config())

    chapter = parser.parse_chapter_content([_BIQUGE4_CHAPTER], chapter_id="1")

    assert chapter is not None
    assert chapter["title"] == "第一章 起风"
    assert chapter["content"].splitlines() == [
        "第一段&实体",
        "第二段中 文",
        "第三段 <b>",
    ]


@pytest.mark.parametrize(
    ("raw_title", "expected"),
    [
        ("第一章 (1 / 6)", "第一章"),
        ("第一章（2/3）", "第一章"),
        ("第一章（上）", "第一章（上）"),
    ],
)
def test_biquge4_chapter_title_marker(raw_title: str, expected: str) -> None:
    parser = _Biquge4(_config())
    page = (
        f"<html><body><h1>{raw_title}</h1>"
        "<div id='novelcontent'>正文</div></body></html>"
    )

    chapter = parser.parse_chapter_content([page], chapter_id="1")

    assert chapter is not None
    assert chapter["title"] == expected


def test_biquge4_chapter_combines_paginated_pages() -> None:
    parser = _Biquge4(_config())
    second = """
    <html><body>
      <h1>第一章 (2 / 6)</h1>
      <div id="novelcontent">续页正文</div>
    </body></html>
    """

    chapter = parser.parse_chapter_content([_BIQUGE4_CHAPTER, second], chapter_id="1")

    assert chapter is not None
    assert chapter["content"].endswith("续页正文")


def test_biquge4_chapter_without_novelcontent_returns_none() -> None:
    parser = _Biquge4(_config())

    assert (
        parser.parse_chapter_content(
            ["<html><body><h1>空章节</h1></body></html>"], chapter_id="1"
        )
        is None
    )


def test_biquge4_regex_fallback_handles_entities() -> None:
    parser = _Biquge4(_config())
    page = """
    <html><body>
      <div id="novelcontent">
        A&amp;B<br>&#x4E2D;文
      </div>
    </body></html>
    """

    paragraphs = parser._extract_paragraphs_regex(page)

    assert paragraphs == ["A&B", "中文"]
