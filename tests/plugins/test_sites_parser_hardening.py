#!/usr/bin/env python3
"""
Regression tests locking the crash fixes from the site-plugin audit
(offline fuzz: missing fields / empty tags / null JSON values / blank pages).

Each test feeds a degraded-but-plausible page to a sampled site parser and
asserts the parser degrades to ``None``/default instead of raising.
"""

import base64
import contextlib
import json
from importlib import import_module
from typing import Any

import pytest

from novel_downloader.plugins.common.parser.biquge1 import Biquge1Parser
from novel_downloader.plugins.sites.biquge5.parser import Biquge5Parser
from novel_downloader.plugins.sites.biquge345.parser import Biquge345Parser
from novel_downloader.plugins.sites.biquguo.parser import BiquguoParser
from novel_downloader.plugins.sites.bxwx9.parser import Bxwx9Parser
from novel_downloader.plugins.sites.ciluke.parser import CilukeParser
from novel_downloader.plugins.sites.ciyuanji.parser import CiyuanjiParser
from novel_downloader.plugins.sites.fsshu.parser import FsshuParser
from novel_downloader.plugins.sites.hetushu.parser import HetushuParser
from novel_downloader.plugins.sites.ktshu.parser import KtshuParser
from novel_downloader.plugins.sites.laoyaoxs.parser import LaoyaoxsParser
from novel_downloader.plugins.sites.linovelib.parser import LinovelibParser
from novel_downloader.plugins.sites.mangg_net.parser import ManggNetParser
from novel_downloader.plugins.sites.n37yue.parser import N37yueParser
from novel_downloader.plugins.sites.qidian.parser import QidianParser
from novel_downloader.plugins.sites.qqbook.parser import QqbookParser
from novel_downloader.plugins.sites.shaoniandream.parser import ShaoniandreamParser
from novel_downloader.plugins.sites.shencou.parser import ShencouParser
from novel_downloader.plugins.sites.shuhaige.parser import ShuhaigeParser
from novel_downloader.plugins.sites.trxs.parser import TrxsParser
from novel_downloader.schemas import ParserConfig


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


# --------------------------------------------------------------------------
# minimal golden pages (structure only; values are irrelevant)
# --------------------------------------------------------------------------
QD_INFO = """
<html><body>
<h1 id="bookName">测试书名</h1>
<a class="writer-name">测试作者</a>
<a id="bookImg" data-bid="12345"></a>
<span class="update-time">更新时间：2026-01-01</span>
<p class="book-attribute"><span>连载</span></p>
<p class="count"><em>100万字</em></p>
<div id="allCatalog"><div class="catalog-volume">
<h3 class="volume-name">正文卷</h3>
<ul class="volume-chapters">
<li><a class="chapter-name" href="/book/12345/1.html">第一章</a></li>
</ul>
</div></div>
</body></html>
"""


def _qd_chapter(chapter_info: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "pageContext": {"pageProps": {"pageData": {"chapterInfo": chapter_info}}}
    }
    return (
        '<html><body><script id="vite-plugin-ssr_pageContext">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


QD_CHAPTER_OK = _qd_chapter(
    {
        "chapterName": "第一章",
        "content": "<p>第一段。</p><p>第二段。</p>",
        "chapterId": "1",
        "vipStatus": 0,
        "isBuy": 1,
        "cES": 0,
        "eFW": 0,
        "fEnS": 0,
        "authorSay": "作者的话",
        "modifyTime": 0,
        "actualWords": 12,
    }
)

QQ_INFO = """
<html><head>
<meta property="og:novel:book_name" content="测试书名">
<meta property="og:novel:author" content="测试作者">
<meta property="og:novel:read_url" content="https://x/book/99">
</head><body></body></html>
"""

HT_INFO = """
<html><body>
<div class="book_info finish"><h2>测试书名</h2>
<div><span>作者：</span><a>测试作者</a></div>
</div>
</body></html>
"""

HT_CHAPTER = """
<html><body>
<div id="content">
<h2 class="h2">第一章 标题</h2>
<div>第一段。</div>
<div>第二段。</div>
</div>
</body></html>
"""

SN_INFO = """
<html><body>
<div class="bookdetail-name"><span class="title">测试书名</span><i>连载</i></div>
<span class="penName"><a>测试作者</a></span>
</body></html>
"""

SN_CHAPTER_BASE = {
    "status": 1,
    "data": {
        "title": "第一章",
        "imgPrefix": "https://x/",
        "encryt_keys": [],
        "show_content": [],
        "chapterpic": [],
    },
}

SC_INFO = """
<html><body><table><tr><td><span><a>测试书名小说</a></span></td></tr></table></body></html>
"""

SC_CATALOG = """
<html><body>
<div class="zjbox"><h2>第一卷</h2></div>
<div class="zjlist4"><ol><li><a href="203740.html">第一章</a></li></ol></div>
</body></html>
"""

CY_BOOK_OK = """
<html><body><script id="__NEXT_DATA__">"""
CY_BOOK_OK += json.dumps(
    {
        "props": {
            "pageProps": {
                "book": {"bookName": "测试书名", "authorName": "测试作者"},
                "bookChapter": {
                    "chapterList": [
                        {
                            "volumeId": 1,
                            "title": "第一卷",
                            "chapterName": "第一章",
                            "bookId": 5,
                            "chapterId": 10,
                            "sortNum": 1,
                        }
                    ]
                },
            }
        }
    }
)
CY_BOOK_OK += "</script></body></html>"

CY_CHAPTER_OK = """
<html><body><script id="__NEXT_DATA__">"""
CY_CHAPTER_OK += json.dumps(
    {
        "props": {
            "pageProps": {
                "chapterContent": {
                    "content": "ign",
                    "chapterName": "第一章",
                    "chapterId": 10,
                    "imgList": [{"paragraphIndex": 0, "imgUrl": "https://x/1.jpg"}],
                    "wordList": [],
                }
            }
        }
    }
)
CY_CHAPTER_OK += "</script></body></html>"


def _cy_page(page_props: dict[str, Any]) -> str:
    return (
        '<html><body><script id="__NEXT_DATA__">'
        + json.dumps({"props": {"pageProps": page_props}})
        + "</script></body></html>"
    )


# --------------------------------------------------------------------------
# qidian
# --------------------------------------------------------------------------
def test_qidian_golden_book_info(cfg: ParserConfig) -> None:
    parser = QidianParser(cfg)
    info = parser.parse_book_info([QD_INFO])
    assert info is not None
    assert info["book_name"] == "测试书名"
    assert info["cover_url"].endswith("/12345/600.webp")


def test_qidian_book_info_without_book_img(cfg: ParserConfig) -> None:
    """#bookImg / data-bid missing must not raise IndexError."""
    page = QD_INFO.replace('<a id="bookImg" data-bid="12345"></a>', "")
    parser = QidianParser(cfg)
    info = parser.parse_book_info([page])
    assert info is not None
    assert info["cover_url"] == ""


def test_qidian_blank_page_returns_none(cfg: ParserConfig) -> None:
    parser = QidianParser(cfg)
    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None


def test_qidian_chapter_null_fields_do_not_crash(cfg: ParserConfig) -> None:
    """content/authorSay/cES as JSON null must not raise."""
    parser = QidianParser(cfg)
    page = _qd_chapter(
        {
            "chapterName": "第一章",
            "content": None,
            "authorSay": None,
            "cES": None,
            "chapterId": "1",
            "vipStatus": 0,
            "isBuy": 1,
        }
    )
    assert parser.parse_chapter_content([page], "1") is None

    page2 = _qd_chapter(
        {
            "chapterName": "第一章",
            "content": "<p>第一段。</p>",
            "authorSay": "",
            "cES": "",
            "chapterId": "1",
            "vipStatus": 0,
            "isBuy": 1,
        }
    )
    # cES="" is not encryption: normal parse path must still work
    result = parser.parse_chapter_content([page2], "1")
    assert result is not None
    assert result["title"] == "第一章"


def test_qidian_chapter_golden(cfg: ParserConfig) -> None:
    parser = QidianParser(cfg)
    result = parser.parse_chapter_content([QD_CHAPTER_OK], "1")
    assert result is not None
    # _parse_normal splits on "<p>" only; trailing markup is pre-existing behavior
    assert "第一段。" in result["content"]
    assert "第二段。" in result["content"]


# --------------------------------------------------------------------------
# qqbook
# --------------------------------------------------------------------------
def test_qqbook_catalog_not_json_returns_none(cfg: ParserConfig) -> None:
    parser = QqbookParser(cfg)
    for bad in ("", "   ", "not json", "<html></html>"):
        assert parser.parse_book_info([QQ_INFO, bad]) is None


@pytest.mark.parametrize("bad", ["null", "[]", '"str"'])
def test_qqbook_catalog_not_object_returns_none(cfg: ParserConfig, bad: str) -> None:
    parser = QqbookParser(cfg)
    assert parser.parse_book_info([QQ_INFO, bad]) is None


def test_qqbook_catalog_skips_non_dict_items(cfg: ParserConfig) -> None:
    catalog = json.dumps(
        {"data": [None, "junk", {"cid": "1", "chapterName": "第一章", "free": True}]}
    )
    parser = QqbookParser(cfg)
    info = parser.parse_book_info([QQ_INFO, catalog])
    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert len(chapters) == 1
    assert chapters[0]["chapterId"] == "1"


def test_qqbook_blank_info_page_returns_none(cfg: ParserConfig) -> None:
    parser = QqbookParser(cfg)
    assert parser.parse_book_info(["", '{"data": []}']) is None


# --------------------------------------------------------------------------
# hetushu
# --------------------------------------------------------------------------
def test_hetushu_catalog_not_json_returns_none(cfg: ParserConfig) -> None:
    parser = HetushuParser(cfg)
    for bad in ("", "not json", "null", "[]", '"str"'):
        assert parser.parse_book_info([HT_INFO, bad]) is None


def test_hetushu_catalog_short_rows_are_skipped(cfg: ParserConfig) -> None:
    catalog = json.dumps([["dt"], ["dd"], ["dd", "第一章", "1001"]])
    parser = HetushuParser(cfg)
    info = parser.parse_book_info([HT_INFO, catalog])
    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["title"] for c in chapters] == ["第一章"]


def test_hetushu_catalog_null_row_fields_do_not_crash(cfg: ParserConfig) -> None:
    catalog = json.dumps([["dt", None], ["dd", None, None]])
    parser = HetushuParser(cfg)
    info = parser.parse_book_info([HT_INFO, catalog])
    assert info is not None


def test_hetushu_chapter_bad_order_data_returns_none(cfg: ParserConfig) -> None:
    parser = HetushuParser(cfg)
    for bad in ("!!not-b64!!", base64.b64encode(b"").decode(), "eA=="):
        assert parser.parse_chapter_content([HT_CHAPTER, bad], "1") is None


def test_hetushu_chapter_order_out_of_range_does_not_crash(cfg: ParserConfig) -> None:
    parser = HetushuParser(cfg)
    # order 5 exceeds paragraph count
    order = base64.b64encode(b"5AB%0").decode()
    result = parser.parse_chapter_content([HT_CHAPTER, order], "1")
    assert result is None or isinstance(result["content"], str)


def test_hetushu_chapter_more_orders_than_paragraphs(cfg: ParserConfig) -> None:
    parser = HetushuParser(cfg)
    order = base64.b64encode(b"1AB%0CD%2EF%3").decode()
    result = parser.parse_chapter_content([HT_CHAPTER, order], "1")
    assert result is None or isinstance(result["content"], str)


def test_hetushu_chapter_blank_html_page_returns_none(cfg: ParserConfig) -> None:
    parser = HetushuParser(cfg)
    order = base64.b64encode(b"1AB%0").decode()
    assert parser.parse_chapter_content(["", order], "1") is None
    assert parser.parse_chapter_content(["   ", order], "1") is None


def test_hetushu_golden_roundtrip(cfg: ParserConfig) -> None:
    parser = HetushuParser(cfg)
    catalog = json.dumps([["dt", "正文"], ["dd", "第一章", "1001"]])
    info = parser.parse_book_info([HT_INFO, catalog])
    assert info is not None
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "1001"

    order = base64.b64encode(b"1AB%0").decode()
    result = parser.parse_chapter_content([HT_CHAPTER, order], "1001")
    assert result is not None
    assert result["content"] == "第二段。\n第一段。"


# --------------------------------------------------------------------------
# shaoniandream
# --------------------------------------------------------------------------
def test_shaoniandream_book_catalog_degraded(cfg: ParserConfig) -> None:
    parser = ShaoniandreamParser(cfg)
    for bad in ("", "not json", "null", "[]", '"str"', "{}"):
        assert parser.parse_book_info([SN_INFO, bad]) is None


def test_shaoniandream_book_null_readdir(cfg: ParserConfig) -> None:
    parser = ShaoniandreamParser(cfg)
    catalog = json.dumps({"data": {"readdir": None}})
    assert parser.parse_book_info([SN_INFO, catalog]) is None

    catalog2 = json.dumps({"data": None})
    assert parser.parse_book_info([SN_INFO, catalog2]) is None

    catalog3 = json.dumps(
        {"data": {"readdir": [None, {"title": "卷", "list": [None, {"id": 1}]}]}}
    )
    info = parser.parse_book_info([SN_INFO, catalog3])
    assert info is not None
    assert info["volumes"][0]["volume_name"] == "卷"
    assert len(info["volumes"][0]["chapters"]) == 1


def test_shaoniandream_book_class_null_does_not_crash(cfg: ParserConfig) -> None:
    parser = ShaoniandreamParser(cfg)
    catalog = json.dumps(
        {"data": {"readdir": [{"title": "卷", "list": [{"id": 1, "class": None}]}]}}
    )
    info = parser.parse_book_info([SN_INFO, catalog])
    assert info is not None
    assert info["volumes"][0]["chapters"][0]["accessible"] is True


def test_shaoniandream_chapter_missing_crypto_returns_none(cfg: ParserConfig) -> None:
    parser = ShaoniandreamParser(cfg)
    payload = dict(SN_CHAPTER_BASE)
    assert parser.parse_chapter_content([json.dumps(payload)], "1") is None

    payload["data"] = dict(SN_CHAPTER_BASE["data"], encryt_keys=["only-one"])
    assert parser.parse_chapter_content([json.dumps(payload)], "1") is None

    payload["data"] = dict(SN_CHAPTER_BASE["data"], encryt_keys=["x", "y"])
    assert parser.parse_chapter_content([json.dumps(payload)], "1") is None


def test_shaoniandream_chapter_not_json_returns_none(cfg: ParserConfig) -> None:
    parser = ShaoniandreamParser(cfg)
    for bad in ("", "not json", "[]", '"str"'):
        assert parser.parse_chapter_content([bad], "1") is None


def test_shaoniandream_chapter_invalid_status_still_raises(cfg: ParserConfig) -> None:
    """status != 1 deliberately raises; missing data degrades to None."""
    parser = ShaoniandreamParser(cfg)
    with pytest.raises(ValueError):
        parser.parse_chapter_content([json.dumps({"status": 0})], "1")

    # valid status but data missing -> None, not KeyError
    assert parser.parse_chapter_content([json.dumps({"status": 1})], "1") is None


def test_shaoniandream_golden_chapter(cfg: ParserConfig) -> None:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    key = b"0123456789abcdef"
    iv = b"abcdef9876543210"
    cipher = AES.new(key, AES.MODE_CBC, iv)
    enc = base64.b64encode(cipher.encrypt(pad("第一段。".encode(), 16))).decode()
    payload = {
        "status": 1,
        "data": {
            "title": "第一章",
            "imgPrefix": "https://x/",
            "encryt_keys": [
                base64.b64encode(key).decode(),
                base64.b64encode(iv).decode(),
            ],
            "show_content": [{"content": enc}],
            "chapterpic": [{"url": "1.jpg"}],
        },
    }
    parser = ShaoniandreamParser(cfg)
    result = parser.parse_chapter_content([json.dumps(payload)], "1")
    assert result is not None
    assert result["title"] == "第一章"
    assert result["content"] == "第一段。"


# --------------------------------------------------------------------------
# shencou
# --------------------------------------------------------------------------
def test_shencou_catalog_without_h2_text(cfg: ParserConfig) -> None:
    """zjbox without h2 text must not raise IndexError."""
    catalog = (
        '<html><body><div class="zjbox"></div>'
        '<div class="zjlist4"><ol><li><a>第一章</a></li></ol></div></body></html>'
    )
    parser = ShencouParser(cfg)
    info = parser.parse_book_info([SC_INFO, catalog])
    assert info is not None
    assert info["volumes"][0]["chapters"][0]["chapterId"] == ""


def test_shencou_catalog_link_without_href(cfg: ParserConfig) -> None:
    catalog = (
        '<html><body><div class="zjbox"><h2>卷</h2></div>'
        '<div class="zjlist4"><ol><li><a>第一章</a></li></ol></div></body></html>'
    )
    parser = ShencouParser(cfg)
    info = parser.parse_book_info([SC_INFO, catalog])
    assert info is not None
    assert info["volumes"][0]["chapters"][0]["url"] == ""


@pytest.mark.parametrize("pages", [["", ""], ["   ", "   "]])
def test_shencou_blank_pages_return_none(cfg: ParserConfig, pages: list) -> None:
    parser = ShencouParser(cfg)
    assert parser.parse_book_info(pages) is None
    assert parser.parse_chapter_content([pages[0]], "1") is None


# --------------------------------------------------------------------------
# ciyuanji
# --------------------------------------------------------------------------
def test_ciyuanji_props_chain_nulls_return_none(cfg: ParserConfig) -> None:
    parser = CiyuanjiParser(cfg)
    for page in (
        _cy_page({}),
        _cy_page({"book": None}),
        _cy_page({"bookChapter": "junk"}),
        _cy_page({"chapterContent": None}),
    ):
        assert parser.parse_book_info([page]) is None


def test_ciyuanji_tag_list_null_and_mixed(cfg: ParserConfig) -> None:
    page = _cy_page(
        {
            "book": {"bookName": "测试书名", "tagList": None},
            "bookChapter": {
                "chapterList": [{"volumeId": 1, "chapterName": "第一章", "sortNum": 1}]
            },
        }
    )
    parser = CiyuanjiParser(cfg)
    info = parser.parse_book_info([page])
    assert info is not None
    assert info["tags"] == []

    page2 = _cy_page(
        {
            "book": {"bookName": "测试书名", "tagList": [None, {"tagName": "奇幻"}]},
            "bookChapter": {
                "chapterList": [{"volumeId": 1, "chapterName": "第一章", "sortNum": 1}]
            },
        }
    )
    info2 = parser.parse_book_info([page2])
    assert info2 is not None
    assert info2["tags"] == ["奇幻"]


def test_ciyuanji_null_sort_keys_do_not_crash(cfg: ParserConfig) -> None:
    page = _cy_page(
        {
            "bookChapter": {
                "chapterList": [
                    {
                        "volumeId": 1,
                        "chapterName": "第一章",
                        "sortNum": None,
                        "volumeSortNum": None,
                    }
                ]
            }
        }
    )
    parser = CiyuanjiParser(cfg)
    info = parser.parse_book_info([page])
    assert info is not None
    assert info["volumes"][0]["chapters"][0]["title"] == "第一章"


def test_ciyuanji_resources_bad_entries_skipped() -> None:
    """content "ign" fails DES decryption; that path is out of scope here,
    so drive _build_resources directly for the resource hardening."""
    resources = CiyuanjiParser._build_resources(
        {
            "imgList": [
                None,
                {"imgUrl": "https://x/1.jpg"},
                {"paragraphIndex": "", "imgUrl": "https://x/2.jpg"},
                {"paragraphIndex": 1, "imgUrl": "https://x/3.jpg", "width": "auto"},
            ],
            "wordList": [None, {"paragraphIndex": 2, "imgUrl": "https://x/4.jpg"}],
        }
    )
    assert [r["url"] for r in resources] == ["https://x/3.jpg", "https://x/4.jpg"]
    assert "width" not in resources[0]


def test_ciyuanji_golden_book(cfg: ParserConfig) -> None:
    parser = CiyuanjiParser(cfg)
    info = parser.parse_book_info([CY_BOOK_OK])
    assert info is not None
    assert info["book_name"] == "测试书名"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "10"


def test_ciyuanji_blank_pages_return_none(cfg: ParserConfig) -> None:
    parser = CiyuanjiParser(cfg)
    assert parser.parse_chapter_content([""], "10") is None
    assert parser.parse_book_info([""]) is None


# --------------------------------------------------------------------------
# blank-page hardening for the defensive biquge-family parsers
# --------------------------------------------------------------------------
SHG_INFO = """
<html><body>
<div id="info"><h1>测试书名</h1><p><a>测试作者</a></p></div>
<div id="list"><dl><dt>正文</dt><dd><a href="/b/1/1.html">第一章</a></dd></dl></div>
</body></html>
"""

BQ_INFO = """
<html><body>
<div class="right_border"><h1>测试书名</h1></div>
<div class="border"><ul class="info"><li><a href="/b/1/1.html">第一章</a></li></ul>
</div>
</body></html>
"""

TRX_INFO = """
<html><body>
<div class="infos"><h1>测试书名</h1></div>
<div class="book_list"><ul><li><a href="/t/1/1.html">第一章</a></li></ul></div>
</body></html>
"""

LN_INFO = """
<html><head>
<meta property="og:novel:book_name" content="测试书名">
</head><body></body></html>
"""

LY_INFO = """
<html><head>
<meta property="og:novel:book_name" content="测试书名">
</head><body></body></html>
"""

LY_CATALOG = """
<html><body>
<div class="read">
<dl id="newlist"><dd><a href="1.html" title="第一章">第一章</a></dd></dl>
</div>
</body></html>
"""


@pytest.mark.parametrize(
    "parser_cls",
    [ShuhaigeParser, Biquge345Parser, TrxsParser, LinovelibParser, LaoyaoxsParser],
    ids=["shuhaige", "biquge345", "trxs", "linovelib", "laoyaoxs"],
)
def test_blank_pages_return_none(cfg: ParserConfig, parser_cls: Any) -> None:
    parser = parser_cls(cfg)
    assert parser.parse_book_info([""]) is None
    assert parser.parse_book_info(["   "]) is None
    assert parser.parse_book_info(["", ""]) is None
    assert parser.parse_chapter_content([""], "1") is None
    assert parser.parse_chapter_content(["   "], "1") is None


def test_golden_book_info_for_defensive_sites(cfg: ParserConfig) -> None:
    """Golden pages still parse after the blank-page guards."""
    assert ShuhaigeParser(cfg).parse_book_info([SHG_INFO]) is not None
    assert Biquge345Parser(cfg).parse_book_info([BQ_INFO]) is not None
    assert TrxsParser(cfg).parse_book_info([TRX_INFO]) is not None
    assert LinovelibParser(cfg).parse_book_info([LN_INFO, LN_INFO]) is not None
    assert LaoyaoxsParser(cfg).parse_book_info([LY_INFO, LY_CATALOG]) is not None
    assert ShencouParser(cfg).parse_book_info([SC_INFO, SC_CATALOG]) is not None


# --------------------------------------------------------------------------
# biquge1 family: empty document shells crash the common chapter parser
# --------------------------------------------------------------------------
# ``Biquge1Parser._strip_nested_html`` removes the html/body wrappers, so a
# shell page like ``<html></html>`` is non-blank as fetched but empty after
# stripping, and ``html.fromstring("")`` raised ParserError. The guard added
# in ``parse_chapter_content`` must skip shell pages for every subclass.
BQ1_FAMILY = [
    Biquge5Parser,
    BiquguoParser,
    Bxwx9Parser,
    CilukeParser,
    FsshuParser,
    KtshuParser,
    ManggNetParser,
    N37yueParser,
]
BQ1_FAMILY_IDS = [
    "biquge5",
    "biquguo",
    "bxwx9",
    "ciluke",
    "fsshu",
    "ktshu",
    "mangg_net",
    "n37yue",
]

BQ1_SHELL_PAGES = [
    "<html></html>",
    "<html><head></head><body></body></html>",
    "<html>   </html>",
]

BQ1_BOOK = """
<html><head>
<meta property="og:novel:book_name" content="测试书名">
<meta property="og:novel:author" content="测试作者">
</head><body>
<div class="book_list2"><a href="/book/1/1.html" title="第一章">第一章</a></div>
</body></html>
"""

BQ1_CHAPTER = """
<html><head><meta charset="utf-8"></head><body>
<h1>第一章</h1>
<article><p>第一段。</p><p>第二段。</p></article>
</body></html>
"""


@pytest.mark.parametrize("parser_cls", BQ1_FAMILY, ids=BQ1_FAMILY_IDS)
def test_biquge1_family_shell_pages_return_none(
    cfg: ParserConfig, parser_cls: Any
) -> None:
    """Document shells must degrade to None, never raise ParserError."""
    parser = parser_cls(cfg)
    for shell in BQ1_SHELL_PAGES:
        assert parser.parse_chapter_content([shell], "1") is None
        assert parser.parse_book_info([shell]) is None
    # every page empty after stripping -> still None, no raise
    assert parser.parse_chapter_content(BQ1_SHELL_PAGES, "1") is None


@pytest.mark.parametrize("parser_cls", BQ1_FAMILY, ids=BQ1_FAMILY_IDS)
def test_biquge1_family_shell_page_skipped_when_mixed(
    cfg: ParserConfig, parser_cls: Any
) -> None:
    """A shell page among good pages is skipped, not fatal."""
    parser = parser_cls(cfg)
    result = parser.parse_chapter_content([BQ1_SHELL_PAGES[0], BQ1_CHAPTER], "1")
    assert result is not None
    assert result["title"] == "第一章"
    assert result["content"] == "第一段。\n第二段。"


@pytest.mark.parametrize("parser_cls", BQ1_FAMILY, ids=BQ1_FAMILY_IDS)
def test_biquge1_family_golden_book_info(cfg: ParserConfig, parser_cls: Any) -> None:
    """Golden catalogue page still parses after the shell-page guard."""
    parser = parser_cls(cfg)
    info = parser.parse_book_info([BQ1_BOOK])
    assert info is not None
    assert info["book_name"] == "测试书名"
    assert info["author"] == "测试作者"
    chapters = info["volumes"][0]["chapters"]
    assert [c["title"] for c in chapters] == ["第一章"]
    assert chapters[0]["chapterId"] == "1"


@pytest.mark.parametrize("parser_cls", BQ1_FAMILY, ids=BQ1_FAMILY_IDS)
def test_biquge1_family_golden_chapter(cfg: ParserConfig, parser_cls: Any) -> None:
    """Golden chapter page still parses after the shell-page guard."""
    parser = parser_cls(cfg)
    result = parser.parse_chapter_content([BQ1_CHAPTER], "1")
    assert result is not None
    assert result["title"] == "第一章"
    assert result["content"] == "第一段。\n第二段。"


def test_all_biquge1_subclass_sites_heal_on_shell_pages(
    cfg: ParserConfig,
) -> None:
    """Every Biquge1Parser subclass in the sites package heals via the common
    fix, including subclasses added after this regression test was written.

    Imports every ``sites.<site>.parser`` module (the registry's own loading
    mechanism), collects Biquge1Parser subclasses, then feeds each one a
    document shell. The audited site names are asserted to be present so a
    silently broken enumeration cannot pass vacuously.
    """
    import importlib.resources as res

    from novel_downloader.plugins import sites as sites_pkg

    for name in res.contents(sites_pkg):
        if name.startswith("_"):
            continue
        with contextlib.suppress(ModuleNotFoundError):
            import_module(f"{sites_pkg.__name__}.{name}.parser")

    found: set[type[Biquge1Parser]] = set()

    def collect(cls: type[Biquge1Parser]) -> None:
        for sub in cls.__subclasses__():
            if sub not in found:
                found.add(sub)
                collect(sub)

    collect(Biquge1Parser)

    site_names = {cls.site_name for cls in found}
    assert site_names >= set(BQ1_FAMILY_IDS)
    for cls in sorted(found, key=lambda c: c.site_name):
        parser = cls(cfg)
        assert parser.parse_chapter_content(["<html></html>"], "1") is None
        assert parser.parse_book_info(["<html></html>"]) is None
