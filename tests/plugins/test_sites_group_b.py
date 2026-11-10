#!/usr/bin/env python3
"""Fixture tests for the B-group site plugins.

Everything here is offline: the pages are trimmed copies of the markup the
live sites returned during the 2026-11 survey, and only the parsing/ID
helpers are exercised.  Each fixture pins one defect that showed up when
following a search hit through to a download:

* the ``mangg_net`` family derived the book id from the raw href, so an
  absolute or protocol-relative link produced a URL-shaped id;
* ``yibige`` only understood its ``javascript:sovote(...)`` wrapper and
  turned SEO ``book:tag`` copies of the title/author into tags;
* ``ciweimao`` kept a query string in the book id and stored the latest
  chapter title inside ``update_time``.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

import pytest

from novel_downloader.plugins.common.parser._utils import chapter_id_from_href
from novel_downloader.plugins.rule.model import parse_rule
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.plugins.sites.biquge5.parser import Biquge5Parser
from novel_downloader.plugins.sites.biquge5.searcher import Biquge5Searcher
from novel_downloader.plugins.sites.biquguo.parser import BiquguoParser
from novel_downloader.plugins.sites.biquguo.searcher import BiquguoSearcher
from novel_downloader.plugins.sites.bxwx9.parser import Bxwx9Parser
from novel_downloader.plugins.sites.bxwx9.searcher import Bxwx9Searcher
from novel_downloader.plugins.sites.ciweimao.parser import CiweimaoParser
from novel_downloader.plugins.sites.ciweimao.searcher import CiweimaoSearcher
from novel_downloader.plugins.sites.fanqienovel.parser import FanqienovelParser
from novel_downloader.plugins.sites.fanqienovel.searcher import FanqienovelSearcher
from novel_downloader.plugins.sites.fsshu.parser import FsshuParser
from novel_downloader.plugins.sites.fsshu.searcher import FsshuSearcher
from novel_downloader.plugins.sites.mangg_net.parser import ManggNetParser
from novel_downloader.plugins.sites.mangg_net.searcher import ManggNetSearcher
from novel_downloader.plugins.sites.n37yue.parser import N37yueParser
from novel_downloader.plugins.sites.n37yue.searcher import N37yueSearcher
from novel_downloader.plugins.sites.yibige.parser import YibigeParser
from novel_downloader.plugins.sites.yibige.searcher import YibigeSearcher
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


def _searcher(cls: type, session: Any = None) -> Any:
    return cls(session if session is not None else object())


# --------------------------------------------------------------------------- #
# mangg_net family: book ids must not depend on how the href is written
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("cls", "href", "expected"),
    [
        (
            ManggNetSearcher,
            "https://www.mangg.net/id84814/?from=list",
            "id84814",
        ),
        (
            Biquge5Searcher,
            "//www.biquge5.com/84_84873/",
            "84_84873",
        ),
        (
            BiquguoSearcher,
            "https://www.biquguo.com/84/84829/#comments",
            "84-84829",
        ),
        (
            FsshuSearcher,
            "https://www.fsshu.com/biquge/95_95624/",
            "95_95624",
        ),
        (
            Bxwx9Searcher,
            "https://www.bxwx9.org/b/70/70316/",
            "70-70316",
        ),
        (
            N37yueSearcher,
            "//www.37yue.com/84/84936/",
            "84-84936",
        ),
    ],
)
def test_mangg_family_book_id_from_absolute_hrefs(
    cls: type, href: str, expected: str
) -> None:
    assert cls._url_to_id(href) == expected  # type: ignore[attr-defined]


_MANGG_WITH_BROKEN_ROW = """
<html><body><div class="row">
  <div class="col-12 col-md-6">
    <dl>
      <dt><a><img src="/images/broken.jpg"></a></dt>
      <dd><h3><a>没有链接的行</a></h3></dd>
    </dl>
  </div>
  <div class="col-12 col-md-6">
    <dl>
      <dt><a href="https://www.mangg.net/id21201/">
        <img src="/images/21/21201/21201s.jpg"></a></dt>
      <dd><h3><a href="https://www.mangg.net/id21201/">[网游]斗破苍穹</a></h3></dd>
      <dd class="book_other">作者：<span>测试作者</span></dd>
      <dd class="book_other">更新时间：09-17 04:08:53</dd>
      <dd class="book_other">
        最新章节：<a href="/id21201/644621.html">第一章</a>
      </dd>
    </dl>
  </div>
</div></body></html>
"""


def test_mangg_limit_counts_parsed_rows_not_raw_rows() -> None:
    searcher = _searcher(ManggNetSearcher)

    rows = searcher._parse_html(_MANGG_WITH_BROKEN_ROW, limit=1)

    # The first card has no href and must be skipped; it must not consume the
    # caller's limit and hide the one valid result behind it.
    assert [r["book_id"] for r in rows] == ["id21201"]
    assert rows[0]["priority"] == ManggNetSearcher.priority


# --------------------------------------------------------------------------- #
# yibige: search-row links
# --------------------------------------------------------------------------- #
_YIBIGE_SEARCH = """
<html><body>
<table class="grid">
  <tr id="nr">
    <td class="odd">
      <a href="javascript:sovote(3477108279670,'/3477108279670/');">书A</a>
    </td>
    <td class="odd">作者A</td>
    <td class="odd">2026-11-08 23:48</td>
  </tr>
  <tr id="nr">
    <td class="odd"><a href="/1138338272783/">书B</a></td>
    <td class="odd">作者B</td>
    <td class="odd">2026-11-10 14:26</td>
  </tr>
  <tr id="nr">
    <td class="odd">
      <a href="https://tw.yibige.org/4554747865634/?from=list#top">书C</a>
    </td>
    <td class="odd">作者C</td>
    <td class="odd">2026-11-09 22:17</td>
  </tr>
  <tr id="nr">
    <td class="odd"><a href="/book/5109529404172/index.html">书D</a></td>
    <td class="odd">作者D</td>
    <td class="odd">2026-11-08 15:21</td>
  </tr>
  <tr id="nr">
    <td class="odd"><a href="javascript:addbookcase(9999);">加入书架</a></td>
    <td class="odd">-</td>
    <td class="odd">-</td>
  </tr>
</table>
</body></html>
"""


def test_yibige_search_accepts_sovote_plain_absolute_and_nested_paths() -> None:
    searcher = _searcher(YibigeSearcher)

    rows = searcher._parse_html(_YIBIGE_SEARCH)

    assert [r["book_id"] for r in rows] == [
        "3477108279670",
        "1138338272783",
        "4554747865634",
        "5109529404172",
    ]
    assert rows[0]["book_url"] == "https://www.yibige.org/3477108279670/"
    assert rows[1]["book_url"] == "https://www.yibige.org/1138338272783/"
    # The absolute row is canonicalised to the configured (simplified) host,
    # with the query string and fragment dropped.
    assert rows[2]["book_url"] == "https://www.yibige.org/4554747865634/"
    assert rows[3]["book_url"] == "https://www.yibige.org/book/5109529404172/index.html"
    assert rows[0]["title"] == "书A"
    assert rows[1]["author"] == "作者B"
    assert rows[3]["update_date"] == "2026-11-08 15:21"
    # A "javascript:addbookcase(...)" row is not a book link.
    assert all(r["book_id"].isdigit() for r in rows)


def test_yibige_search_limit_skips_non_book_rows() -> None:
    searcher = _searcher(YibigeSearcher)

    rows = searcher._parse_html(_YIBIGE_SEARCH, limit=2)

    assert [r["book_id"] for r in rows] == ["3477108279670", "1138338272783"]


_YIBIGE_INFO = """
<html><head>
<meta property="og:novel:book_name" content="测试书名">
<meta property="og:novel:author" content="测试作者">
<meta property="og:novel:category" content="玄幻小说">
<meta property="og:novel:update_time" content="2026-01-02T03:04:05">
<meta property="og:image" content="https://www.yibige.org/cover.jpg">
<meta name="description" content="简介">
<meta property="book:tag" content="测试书名">
<meta property="book:tag" content="测试作者">
<meta property="book:tag" content="玄幻小说">
<meta property="book:tag" content="热血">
</head><body>
<div id="info"><h1>测试书名</h1></div>
<div id="intro"><p>一段简介。</p></div>
</body></html>
"""

_YIBIGE_CATALOG = """
<html><body>
<div id="list"><dl>
  <dd><a href="/1234/5678.html?from=catalog">第一章 开始</a></dd>
  <dd><a href="/1234/5679.html#top">第二章 继续</a></dd>
</dl></div>
</body></html>
"""


def test_yibige_book_info_drops_title_and_author_from_tags(
    cfg: ParserConfig,
) -> None:
    info = YibigeParser(cfg).parse_book_info([_YIBIGE_INFO, _YIBIGE_CATALOG])

    assert info is not None
    assert info["tags"] == ["玄幻小说", "热血"]
    assert info["update_time"] == "2026-01-02 03:04:05"
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["5678", "5679"]


# --------------------------------------------------------------------------- #
# ciweimao: search-row links and book-page update time
# --------------------------------------------------------------------------- #
_CIWEIMAO_SEARCH = """
<html><body><ul>
  <li data-book-id="100420637">
    <a class="cover lazy"
       href="https://www.ciweimao.com/book/100420637?from=list#comments">
      <img src="//e1.kuangxiangit.com/uploads/a.jpg" /></a>
    <div class="cnt">
      <p class="tit"><a href="https://www.ciweimao.com/book/100420637"
                        title="书A">书A</a></p>
      <p>小说作者：<a href="/reader/1">作者A</a></p>
      <p>最近更新：2026-11-10 05:53:29 / 第10章 安排</p>
    </div>
  </li>
  <li data-book-id="100290823">
    <div class="cnt">
      <p class="tit"><a href="/book/100290823/" title="书B">书B</a></p>
      <p>小说作者：<a href="/reader/2">作者B</a></p>
      <p>最近更新：2026-01-04 18:40:03 / 新书</p>
    </div>
  </li>
</ul></body></html>
"""


def test_ciweimao_search_strips_query_from_book_id_and_url() -> None:
    searcher = _searcher(CiweimaoSearcher)

    rows = searcher._parse_html(_CIWEIMAO_SEARCH)

    assert [r["book_id"] for r in rows] == ["100420637", "100290823"]
    assert rows[0]["book_url"] == "https://www.ciweimao.com/book/100420637"
    assert rows[0]["cover_url"] == "https://e1.kuangxiangit.com/uploads/a.jpg"
    assert rows[0]["update_date"] == "2026-11-10 05:53:29"
    assert rows[0]["latest_chapter"] == "第10章 安排"
    # The second card has no cover anchor; the title link is enough.
    assert rows[1]["book_url"] == "https://www.ciweimao.com/book/100290823"
    assert rows[1]["cover_url"] == ""


_CIWEIMAO_INFO = """
<html><head>
<meta property="og:novel:book_name" content="书A">
<meta property="og:novel:author" content="作者A">
<meta property="og:image" content="https://e1.kuangxiangit.com/a.jpg">
<meta property="og:description" content="简介">
<meta property="og:novel:category" content="玄幻奇幻">
</head><body>
<p class="update-time">最后更新：第六章 再临沙漠 [ 2025-04-09 17:01:28 ]</p>
<p class="update-state">连载中·本站首发</p>
<p class="book-grade">总点击：<b>1万</b> 总收藏：<b>2</b> 总字数：<b>20465</b></p>
<p class="label-box">
  <span class="label-warning"><a>同人</a></span>
</p>
</body></html>
"""

_CIWEIMAO_CATALOG = """
<html><body>
<div class="book-chapter-box">
  <h4 class="sub-tit">第一卷</h4>
  <ul class="book-chapter-list">
    <li><a href="https://www.ciweimao.com/chapter/113111634?from=1">第一章</a></li>
    <li><a href="/chapter/113114832"><i class="icon-lock"></i>第二章</a></li>
  </ul>
</div>
</body></html>
"""


def test_ciweimao_book_info_update_time_is_the_timestamp(
    cfg: ParserConfig,
) -> None:
    info = CiweimaoParser(cfg).parse_book_info([_CIWEIMAO_INFO, _CIWEIMAO_CATALOG])

    assert info is not None
    assert info["book_name"] == "书A"
    assert info["update_time"] == "2025-04-09 17:01:28"
    assert info["word_count"] == "20465"
    assert info["tags"] == ["同人", "玄幻奇幻"]
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["113111634", "113114832"]
    assert [c["accessible"] for c in chapters] == [True, False]


# --------------------------------------------------------------------------- #
# fanqienovel: the plain-text JSON parser must not lose rows to null entries
# --------------------------------------------------------------------------- #
_FANQIE_SEARCH_JSON = {
    "data": {
        "search_book_data_list": [
            None,
            {
                "book_id": "123456",
                "book_name": "书A",
                "author": "作者A",
                "thumb_url": "//p.example/cover.jpg",
                "last_chapter_time": "1700000000",
            },
            {"book_id": "654321", "book_name": "书B"},
        ]
    }
}


def test_fanqie_search_limit_counts_valid_rows() -> None:
    searcher = _searcher(FanqienovelSearcher)

    rows = searcher._parse_html(json.dumps(_FANQIE_SEARCH_JSON), limit=1)

    assert [r["book_id"] for r in rows] == ["123456"]
    assert rows[0]["book_url"] == "https://fanqienovel.com/page/123456"
    assert rows[0]["cover_url"] == "https://p.example/cover.jpg"
    assert rows[0]["priority"] == FanqienovelSearcher.priority


# --------------------------------------------------------------------------- #
# mangg_net family: book-page covers must be absolute even without og:url
# --------------------------------------------------------------------------- #
_MANGG_BOOK = """
<html><head>
  <meta property="og:novel:book_name" content="测试书名">
  <meta property="og:image" content="/covers/9_9194.jpg">
</head><body>
  <div class="book_list2">
    <a href="/9_9194/100.html">第一章</a>
    <a href="/9_9194/101.html">第二章</a>
  </div>
</body></html>
"""

_MANGG_BASE_URLS = [
    (Biquge5Parser, "https://www.biquge5.com"),
    (BiquguoParser, "https://www.biquguo.com"),
    (FsshuParser, "https://www.fsshu.com"),
    (Bxwx9Parser, "https://www.bxwx9.org"),
    (N37yueParser, "https://www.37yue.com"),
    (ManggNetParser, "https://www.mangg.net"),
]


@pytest.mark.parametrize(
    ("parser_cls", "origin"),
    _MANGG_BASE_URLS,
    ids=[c.__name__ for c, _ in _MANGG_BASE_URLS],
)
def test_mangg_family_cover_is_absolute_without_og_url(
    cfg: ParserConfig, parser_cls: type, origin: str
) -> None:
    """A root-relative ``og:image`` on a page with no ``og:url``/canonical.

    This is the same page shape the live sites serve; resolving the cover
    against the page's own metadata fails there, so the parser's BASE_URL is
    the fallback.  The cover fetch needs an absolute URL.
    """
    info = parser_cls(cfg).parse_book_info([_MANGG_BOOK])

    assert info is not None
    assert info["cover_url"] == f"{origin}/covers/9_9194.jpg"


_MANGG_CATALOG_PAGE_2 = """
<html><body>
  <div class="book_list2">
    <a href="/9_9194/101.html">第二章 重复</a>
    <a href="/9_9194/102.html">第三章</a>
    <a href="/9_9194/103.html">  </a>
    <a href="/9_9194/103.html">第三章 重复</a>
  </div>
</body></html>
"""


def test_mangg_family_catalog_keeps_order_and_dedupes_across_pages(
    cfg: ParserConfig,
) -> None:
    """Catalog order, repeated ids and empty titles on a two-page catalog."""
    info = Biquge5Parser(cfg).parse_book_info([_MANGG_BOOK, _MANGG_CATALOG_PAGE_2])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    # Page order is preserved; 第二章 repeats on page 2 (dropped); 第三章 has
    # no anchor text and keeps its id, so the chapter is still downloadable.
    assert [c["chapterId"] for c in chapters] == ["100", "101", "102", "103"]
    assert [c["title"] for c in chapters] == ["第一章", "第二章", "第三章", ""]


# --------------------------------------------------------------------------- #
# yibige: catalog ids/urls and cover normalisation on the book page
# --------------------------------------------------------------------------- #
_YIBIGE_BOOK = """
<html><head>
  <meta property="og:novel:book_name" content="测试书名">
  <meta property="og:novel:author" content="测试作者">
  <meta property="og:image" content="//img.example/cover.jpg">
  <meta property="og:url" content="https://tw.yibige.org/1234/">
</head><body><div id="info"><h1>测试书名</h1></div></body></html>
"""

_YIBIGE_CATALOG_SHAPES = """
<html><body>
<div id="list"><dl>
  <dd><a href="/1234/5678.html?from=catalog">第一章 相对</a></dd>
  <dd><a href="//tw.yibige.org/1234/5678.html#top">第一章 重复</a></dd>
  <dd><a href="https://tw.yibige.org/1234/5679.html">第二章 绝对</a></dd>
  <dd><a href="javascript:addbookcase(1);">加入书架</a></dd>
  <dd><a href="/1234/">返回目录</a></dd>
  <dd><a href="/1234/5680.html">第三章</a></dd>
</dl></div>
</body></html>
"""


def test_yibige_catalog_normalises_ids_and_urls(cfg: ParserConfig) -> None:
    info = YibigeParser(cfg).parse_book_info([_YIBIGE_BOOK, _YIBIGE_CATALOG_SHAPES])

    assert info is not None
    # Protocol-relative cover is upgraded; the book page's og:url is the base.
    assert info["cover_url"] == "https://img.example/cover.jpg"

    chapters = info["volumes"][0]["chapters"]
    # The duplicate id, the ``javascript:`` helper and the non-chapter
    # ``/1234/`` link are dropped; order is preserved.
    assert [c["chapterId"] for c in chapters] == ["5678", "5679", "5680"]
    assert [c["title"] for c in chapters] == [
        "第一章 相对",
        "第二章 绝对",
        "第三章",
    ]
    # Every stored link is absolute on the host that served the page, with
    # query strings and fragments gone.
    assert [c["url"] for c in chapters] == [
        "https://tw.yibige.org/1234/5678.html",
        "https://tw.yibige.org/1234/5679.html",
        "https://tw.yibige.org/1234/5680.html",
    ]


# --------------------------------------------------------------------------- #
# ciweimao: catalog volumes, duplicate ids, lock markers and cover
# --------------------------------------------------------------------------- #
_CIWEIMAO_VOLUMES = """
<html><body>
<div class="book-chapter-box">
  <h4 class="sub-tit">第一卷</h4>
  <ul class="book-chapter-list">
    <li><a href="/chapter/1?from=1">第一章</a></li>
    <li><a href="/chapter/2"><i class="icon-lock"></i>第二章</a></li>
  </ul>
</div>
<div class="book-chapter-box">
  <h4 class="sub-tit">第二卷</h4>
  <ul class="book-chapter-list">
    <li><a href="/chapter/2">第二章 重复</a></li>
    <li><a href="/chapter/3">第三章</a></li>
    <li><a href="/chapter/">空章节</a></li>
    <li><a href="javascript:void(0)">展开</a></li>
  </ul>
</div>
<div class="book-chapter-box">
  <h4 class="sub-tit">空卷</h4>
</div>
</body></html>
"""


def test_ciweimao_catalog_dedupes_across_volumes_and_drops_bogus_links(
    cfg: ParserConfig,
) -> None:
    info = CiweimaoParser(cfg).parse_book_info([_CIWEIMAO_INFO, _CIWEIMAO_VOLUMES])

    assert info is not None
    assert info["cover_url"] == "https://e1.kuangxiangit.com/a.jpg"

    volumes = info["volumes"]
    # The third box has no chapter links and carries no volume information.
    assert [v["volume_name"] for v in volumes] == ["第一卷", "第二卷"]
    assert [[c["chapterId"] for c in v["chapters"]] for v in volumes] == [
        ["1", "2"],
        ["3"],
    ]
    # The lock marker of the first occurrence wins; the duplicate is dropped.
    assert [c["accessible"] for c in volumes[0]["chapters"]] == [True, False]
    assert volumes[1]["chapters"][0]["accessible"] is True
    # Chapter links are canonical and absolute.
    assert [c["url"] for c in volumes[0]["chapters"]] == [
        "https://www.ciweimao.com/chapter/1",
        "https://www.ciweimao.com/chapter/2",
    ]


# --------------------------------------------------------------------------- #
# fanqienovel: book page volumes, order, lock flags, duplicate/empty ids
# --------------------------------------------------------------------------- #
def _fanqie_state(page: dict[str, Any]) -> str:
    payload = json.dumps({"page": page}, ensure_ascii=False)
    return (
        "<html><body><script>window.__INITIAL_STATE__ = "
        f"{payload};</script></body></html>"
    )


_FANQIE_BOOK_STATE = {
    "bookName": "书A",
    "authorName": "作者A",
    "thumbUrl": "//p.example/cover.jpg",
    "volumeNameList": ["第一卷", "第二卷"],
    "chapterListWithVolume": [
        [
            {"itemId": "3", "title": "第三章", "realChapterOrder": "3"},
            {
                "itemId": "1",
                "title": "第一章",
                "realChapterOrder": "1",
                "isChapterLock": True,
            },
            {"itemId": "2", "title": "第二章", "realChapterOrder": "2", "needPay": 10},
            {"itemId": "", "title": "没有 id"},
        ],
        [
            {"itemId": "2", "title": "第二章 重复", "realChapterOrder": "2"},
            {"itemId": "4", "title": ""},
        ],
    ],
}


def test_fanqie_book_info_sorts_dedupes_and_drops_empty_ids(cfg: ParserConfig) -> None:
    info = FanqienovelParser(cfg).parse_book_info([_fanqie_state(_FANQIE_BOOK_STATE)])

    assert info is not None
    assert info["cover_url"] == "https://p.example/cover.jpg"

    volumes = info["volumes"]
    assert [v["volume_name"] for v in volumes] == ["第一卷", "第二卷"]
    first, second = volumes
    # Sorted by realChapterOrder; the id-less entry cannot be fetched.
    assert [c["chapterId"] for c in first["chapters"]] == ["1", "2", "3"]
    # A locked chapter is still listed but marked unreadable via the web page.
    assert [c["accessible"] for c in first["chapters"]] == [False, False, True]
    # The second volume keeps only its unique chapter; an empty title is kept
    # because the chapter itself is valid.
    assert [c["chapterId"] for c in second["chapters"]] == ["4"]
    assert second["chapters"][0]["title"] == ""
    assert second["chapters"][0]["accessible"] is True


# --------------------------------------------------------------------------- #
# biquge5r rule source: same host, same ids/urls as the biquge5 plugin
# --------------------------------------------------------------------------- #
def _builtin_biquge5r() -> Any:
    text = (
        files("novel_downloader.plugins.rule.builtin")
        .joinpath("biquge5r.json")
        .read_text(encoding="utf-8-sig")
    )
    return parse_rule(json.loads(text), source="biquge5r.json")


BIQUGE5R_RULE = _builtin_biquge5r()


def _rule_searcher(rule: Any) -> Any:
    cls = type(
        "Biquge5rRuleSearcher",
        (RuleSearcher,),
        {
            "_rule": rule,
            "site_name": rule.site_key,
            "BASE_URL": rule.base_url,
        },
    )
    return cls(None)


def _rule_parser(rule: Any, cfg: ParserConfig) -> Any:
    cls = type(
        "Biquge5rRuleParser",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    return cls(cfg)


_BIQUGE5_SEARCH = """
<html><body><div class="row">
  <div class="col-12 col-md-6">
    <dl>
      <dt><a href="/84_84873/?from=list"><img src="//cdn.example/84.jpg"></a></dt>
      <dd><h3><a href="/84_84873/?from=list">[玄幻]书A</a></h3></dd>
      <dd class="book_other">作者：<span>作者A</span></dd>
      <dd class="book_other">更新时间：09-17 04:08:53</dd>
      <dd class="book_other">最新章节：<a href="/84_84873/9.html">第九章</a></dd>
    </dl>
  </div>
  <div class="col-12 col-md-6">
    <dl>
      <dt><a><img src="/images/broken.jpg"></a></dt>
      <dd><h3><a href="//www.biquge5.com/84_84874/">书B</a></h3></dd>
      <dd class="book_other">作者：<span>作者B</span></dd>
      <dd class="book_other">更新时间：09-18 01:02:03</dd>
      <dd class="book_other">最新章节：<a href="/84_84874/1.html">第一章</a></dd>
    </dl>
  </div>
</div></body></html>
"""


@pytest.mark.parametrize(
    "href",
    [
        "https://www.biquge5.com/84_84873/",
        "//www.biquge5.com/84_84873/",
        "/84_84873/",
        "/84_84873",
        "/84_84873/?from=list",
    ],
)
def test_biquge5r_resolve_book_id_matches_the_plugin(href: str) -> None:
    """The shipped rule's ``resolve.book`` must yield the plugin's book id."""
    path = urlsplit(href).path
    resolve = BIQUGE5R_RULE.resolve
    assert resolve is not None and resolve.book is not None
    match = re.search(resolve.book, path)
    assert match is not None
    assert match.group(1) == Biquge5Searcher._url_to_id(href)


_CHAPTER_HREF_SHAPES = [
    "https://www.biquge5.com/84_84873/155832.html",
    "//www.biquge5.com/84_84873/155832.html",
    "/84_84873/155832.html",
    "/84_84873/155832.html?from=list",
    "/84_84873/155832.html#top",
    "155832.html",
    "/84_84873/155832_2.html",
]


def test_biquge5r_chapter_ids_match_the_plugin() -> None:
    """The rule's catalog ``id_regex`` mirrors ``chapter_id_from_href``."""
    chapters = BIQUGE5R_RULE.book.chapters
    assert chapters.id_regex is not None
    pattern = re.compile(chapters.id_regex)
    for href in _CHAPTER_HREF_SHAPES:
        rule_id = RuleParser._chapter_id(href, pattern, chapters.id_group)
        assert rule_id == chapter_id_from_href(href), href


def test_biquge5r_search_ids_and_urls_match_the_plugin() -> None:
    rule_rows = _rule_searcher(BIQUGE5R_RULE)._parse_html(_BIQUGE5_SEARCH)
    plugin_rows = Biquge5Searcher(None)._parse_html(_BIQUGE5_SEARCH)

    assert [r["book_id"] for r in rule_rows] == [r["book_id"] for r in plugin_rows]
    assert [r["book_url"] for r in rule_rows] == [r["book_url"] for r in plugin_rows]
    for rule_row, plugin_row in zip(rule_rows, plugin_rows, strict=True):
        for field in ("title", "author", "cover_url", "latest_chapter", "update_date"):
            assert rule_row[field] == plugin_row[field], (field, rule_row, plugin_row)
    # The second card only links its title (no <dt> href); both sources still
    # find it, and the protocol-relative cover is absolute.
    assert rule_rows[0]["cover_url"] == "https://cdn.example/84.jpg"
    assert rule_rows[1]["book_url"] == "https://www.biquge5.com/84_84874/"


_BIQUGE5_BOOK_PAGE = """
<html><head>
  <meta property="og:novel:book_name" content="书A">
  <meta property="og:url" content="https://www.biquge5.com/84_84873/">
</head><body>
  <div class="book_list2">
    <a href="/84_84873/1.html">第一章</a>
    <a href="//www.biquge5.com/84_84873/2.html?from=catalog#top">第二章</a>
    <a href="/84_84873/1.html">第一章 重复</a>
    <a href="/84_84873/3.html">  </a>
  </div>
</body></html>
"""


def test_biquge5r_catalog_matches_the_plugin(cfg: ParserConfig) -> None:
    """Same chapters, same order, same ids as the native biquge5 parser."""
    rule_info = _rule_parser(BIQUGE5R_RULE, cfg).parse_book_info([_BIQUGE5_BOOK_PAGE])
    plugin_info = Biquge5Parser(cfg).parse_book_info([_BIQUGE5_BOOK_PAGE])

    assert rule_info is not None and plugin_info is not None
    rule_chapters = rule_info["volumes"][0]["chapters"]
    plugin_chapters = plugin_info["volumes"][0]["chapters"]

    assert [c["chapterId"] for c in rule_chapters] == ["1", "2", "3"]
    assert [c["chapterId"] for c in rule_chapters] == [
        c["chapterId"] for c in plugin_chapters
    ]
    assert [c["title"] for c in rule_chapters] == [c["title"] for c in plugin_chapters]
    # The rule stores absolute chapter links; the query string/fragment is gone.
    assert [c["url"] for c in rule_chapters] == [
        "https://www.biquge5.com/84_84873/1.html",
        "https://www.biquge5.com/84_84873/2.html",
        "https://www.biquge5.com/84_84873/3.html",
    ]


def test_biquge5r_book_and_chapter_url_templates_match_the_fetcher() -> None:
    """Rule and plugin build identical detail/chapter/pagination URLs."""
    from novel_downloader.plugins.rule.engine import render
    from novel_downloader.plugins.sites.biquge5.fetcher import Biquge5Fetcher

    rule = BIQUGE5R_RULE
    assert rule.book is not None and rule.chapter is not None
    book_id, chapter_id = "84_84873", "155832"

    assert render(rule.book.url, base_url=rule.base_url, book_id=book_id) == (
        Biquge5Fetcher.BASE_URL + Biquge5Fetcher.relative_info_url(book_id, 1)
    )
    assert render(
        rule.book.pages.url, base_url=rule.base_url, book_id=book_id, page=2
    ) == (Biquge5Fetcher.BASE_URL + Biquge5Fetcher.relative_info_url(book_id, 2))
    assert render(
        rule.chapter.url,
        base_url=rule.base_url,
        book_id=book_id,
        chapter_id=chapter_id,
    ) == Biquge5Fetcher.BASE_URL + Biquge5Fetcher.relative_chapter_url(
        book_id, chapter_id, 1
    )
    assert render(
        rule.chapter.pages.url,
        base_url=rule.base_url,
        book_id=book_id,
        chapter_id=chapter_id,
        page=2,
    ) == Biquge5Fetcher.BASE_URL + Biquge5Fetcher.relative_chapter_url(
        book_id, chapter_id, 2
    )
