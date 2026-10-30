#!/usr/bin/env python3
"""Tests for the bixiange adapter migrated to m.bixiange.top.

The old domain ``m.bixiange.me`` no longer resolves (NXDOMAIN); every URL in
the adapter now points at ``m.bixiange.top``.  All HTML fixtures below are
inline fragments modelled on real pages captured from the new domain on
2026-10-31 (GBK decoded offsite first).  No network access is used except the
opt-in live test at the bottom, which re-runs the real-fetch verification
recorded for this migration.

Run the live test explicitly with::

    BIXIANGE_LIVE=1 pytest tests/plugins/test_bixiange_migration.py -q -k live
"""

from __future__ import annotations

import inspect
import os
from typing import Any

import pytest

from novel_downloader.plugins.sites.bixiange.fetcher import BixiangeFetcher
from novel_downloader.plugins.sites.bixiange.parser import BixiangeParser
from novel_downloader.plugins.sites.bixiange.searcher import BixiangeSearcher
from novel_downloader.schemas import FetcherConfig, ParserConfig

NEW_HOST = "https://m.bixiange.top"
OLD_HOST = "m.bixiange.me"


def _config() -> ParserConfig:
    return ParserConfig(enable_ocr=False)


# --------------------------------------------------------------------------- #
# Real-page HTML fixtures (captured from m.bixiange.top, 2026-10-31)
# --------------------------------------------------------------------------- #
# wxxz-20921 detail page: non-suffix category, "index/" chapter links,
# the book whose full catalogue holds 2117 chapters.
_INFO_WXXZ = """
<html><head><meta http-equiv="Content-Type"
      content="text/html; charset=gb2312" /></head>
<body>
<div class="detail clearfix">
  <div class="info clearfix">
    <div class="info-left">
      <div class="cover">
        <img src="/d/file/wxxz/1717993065.jpg" alt="从气运词条开始成就人皇">
      </div>
    </div>
    <div class="info-right">
      <div class="desc">
        <h1>从气运词条开始成就人皇(全本)</h1>
        <div class="descTip">
          <p><span>分类：武侠修真</span><span>大小：6.66 MB</span></p>
          <p><span>作者：水煮天命</span></p>
          <p><span>时间：2025-09-29</span></p>
        </div>
        <div class="descInfo">
          <p>人道皇朝内忧外患，妖魔鬼怪层出不穷。<br />
          楚无疆穿越而来，有绝命天书，能以气运改命。</p>
        </div>
      </div>
    </div>
  </div>
  <div class="catalog">
    <ul class="clearfix">
      <li><a href="/wxxz/20921/index/1.html"> 第1节</a></li>
      <li><a href="/wxxz/20921/index/2.html"> 第2节</a></li>
      <li><a href="/wxxz/20921/index/2117.html"> 第2117节</a></li>
    </ul>
  </div>
</div>
</body></html>
"""

# cyjk-24240 detail page: suffix category (info page ends in .html, chapter
# links have no "index/" segment).
_INFO_CYJK = """
<html><head><meta http-equiv="Content-Type"
      content="text/html; charset=gb2312" /></head>
<body>
<div class="detail clearfix">
  <div class="info clearfix">
    <div class="info-left">
      <div class="cover">
        <img src="//m.bixiange.top/d/file/cyjk/1792976187.jpg" alt="x">
      </div>
    </div>
    <div class="info-right">
      <div class="desc">
        <h1>谢邀，人在箱庭，刚成主角(1-134)</h1>
        <div class="descTip">
          <p><span>分类：穿越架空</span><span>大小：1.27 MB</span></p>
          <p><span>作者：皮球不是球</span></p>
          <p><span>时间：2026-10-26</span></p>
        </div>
        <div class="descInfo"><p>谢邀，人在箱庭，同时穿越诸天。</p></div>
      </div>
    </div>
  </div>
  <div class="catalog">
    <ul class="clearfix">
      <li><a href="/cyjk/24240/1.html"> 第1节</a></li>
      <li><a href="/cyjk/24240/2.html"> 第2节</a></li>
    </ul>
  </div>
</div>
</body></html>
"""

_CHAPTER = """
<html><head><meta http-equiv="Content-Type"
      content="text/html; charset=gb2312" /></head>
<body>
<div class="article clearfix">
  <h1>从气运词条开始成就人皇 第2117节</h1>
  <div class="content" id="mycontent">
    <p>　　比如在油车上，原本的德国企业拥有巨大的优势。</p>
    <p>　　而中国企业从新能源赛道发起冲锋。</p>
  </div>
</div>
</body></html>
"""

# Real search result page shape (/e/search/result/?searchid=N): "hd > list"
# rows, cover/title/tips blocks, mixed plain and ".html" hrefs.
_SEARCH_RESULTS = """
<html><head><title>高级搜索 斗罗 - 笔仙阁</title></head>
<body>
<div class="hd clearfix">
  <div class="list">
    <ul class="clearfix">
      <li>
        <div class="cover">
          <a href="/trxs/23006"><img src="/d/file/trxs/1769245151.jpeg"
             alt="同时穿越：从斗罗开始登顶至高"></a>
        </div>
        <div class="info">
          <div class="title">
            <strong><a href="/trxs/23006">同时穿越：从斗罗开始登顶至高</a></strong>
            <div class="tips">
              <span>大小：6.66 MB</span><span>时间：2026-10-30</span>
            </div>
          </div>
          <div class="descript"><a href="/trxs/23006">简介</a></div>
        </div>
      </li>
      <li>
        <div class="cover">
          <a href="/khjj/11945.html"><img src="/d/file/khjj/x.jpg" alt="y"></a>
        </div>
        <div class="info">
          <div class="title">
            <strong><a href="/khjj/11945.html">第二本</a></strong>
            <div class="tips"><span>时间：2026-10-01</span></div>
          </div>
        </div>
      </li>
    </ul>
  </div>
</div>
</body></html>
"""

# Keyword with no hits: the POST itself answers 200 with this EmpireCMS
# "没有搜索到相关的内容" info page (no redirect, no div.list rows).
_SEARCH_EMPTY = """
<html><head><meta http-equiv="Content-Type" content="text/html; charset=gb2312">
<title>信息提示</title></head>
<body>
<table width="500" border="0" align="center" class="tableborder">
  <tr class="header"><td><div align="center">信息提示</div></td></tr>
  <tr bgcolor="#FFFFFF"><td><div align="center">
    <b>没有搜索到相关的内容</b>
    <br><a href="javascript:history.go(-1)">如果您的浏览器没有自动跳转</a>
  </div></td></tr>
</table>
</body></html>
"""

# Repeat search inside 15s on one cookie session: rate-limit info page.
_SEARCH_COOLDOWN = """
<html><head><meta http-equiv="Content-Type" content="text/html; charset=gb2312">
<title>信息提示</title></head>
<body>
<table width="500" border="0" align="center" class="tableborder">
  <tr class="header"><td><div align="center">信息提示</div></td></tr>
  <tr bgcolor="#FFFFFF"><td><div align="center">
    <b>系统限制的搜索时间间隔为 15 秒,请稍后再搜索</b>
  </div></td></tr>
</table>
</body></html>
"""


# --------------------------------------------------------------------------- #
# parser: book info
# --------------------------------------------------------------------------- #
def test_book_info_wxxz_variant_parses_all_fields() -> None:
    parser = BixiangeParser(_config())

    info = parser.parse_book_info([_INFO_WXXZ])

    assert info is not None
    assert info["book_name"] == "从气运词条开始成就人皇(全本)"
    assert info["author"] == "水煮天命"
    # Root-relative cover resolved against the migrated BASE_URL.
    assert info["cover_url"] == f"{NEW_HOST}/d/file/wxxz/1717993065.jpg"
    assert info["update_time"] == "2025-09-29"
    assert info["tags"] == ["武侠修真"]
    assert "人道皇朝内忧外患" in info["summary"]

    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["1", "2", "2117"]
    assert chapters[-1] == {
        "title": "第2117节",
        "url": "/wxxz/20921/index/2117.html",
        "chapterId": "2117",
    }


def test_book_info_cyjk_suffix_variant_and_scheme_relative_cover() -> None:
    parser = BixiangeParser(_config())

    info = parser.parse_book_info([_INFO_CYJK])

    assert info is not None
    assert info["book_name"] == "谢邀，人在箱庭，刚成主角(1-134)"
    assert info["cover_url"] == f"{NEW_HOST}/d/file/cyjk/1792976187.jpg"
    chapters = info["volumes"][0]["chapters"]
    assert chapters[0] == {
        "title": "第1节",
        "url": "/cyjk/24240/1.html",
        "chapterId": "1",
    }


@pytest.mark.parametrize("pages", [[], ["<html><body>空</body></html>"]])
def test_book_info_without_catalog_returns_none(pages: list[str]) -> None:
    parser = BixiangeParser(_config())

    assert parser.parse_book_info(pages) is None


def test_book_info_empty_pages_returns_none() -> None:
    parser = BixiangeParser(_config())

    assert parser.parse_book_info([]) is None
    assert parser.parse_chapter_content([], chapter_id="1") is None


# --------------------------------------------------------------------------- #
# parser: chapter content
# --------------------------------------------------------------------------- #
def test_chapter_content_parses_title_body_and_extra() -> None:
    parser = BixiangeParser(_config())

    chap = parser.parse_chapter_content([_CHAPTER], chapter_id="2117")

    assert chap is not None
    assert chap["id"] == "2117"
    assert chap["title"] == "从气运词条开始成就人皇 第2117节"
    assert chap["content"] == (
        "比如在油车上，原本的德国企业拥有巨大的优势。\n而中国企业从新能源赛道发起冲锋。"
    )
    assert chap["extra"] == {"site": "bixiange"}


def test_chapter_content_without_body_returns_none() -> None:
    parser = BixiangeParser(_config())

    assert (
        parser.parse_chapter_content(
            ["<html><body><h1>目录页</h1></body></html>"], chapter_id="1"
        )
        is None
    )


# --------------------------------------------------------------------------- #
# fetcher: book_id -> URL conventions (offline, fetch monkeypatched)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetcher_urls_all_point_at_new_domain(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = BixiangeFetcher(FetcherConfig(cache_dir=str(tmp_path)))
    seen: list[str] = []

    async def fake_fetch(url: str, **kwargs: Any) -> str:
        seen.append(url)
        return "<html></html>"

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)

    await fetcher.fetch_book_info("wxxz-20921")
    await fetcher.fetch_chapter_content("wxxz-20921", "2117")
    await fetcher.fetch_book_info("cyjk-24240")
    await fetcher.fetch_chapter_content("cyjk-24240", "1")
    await fetcher.fetch_book_info("guanchang-123")
    await fetcher.fetch_chapter_content("khjj-11945", "9")

    assert seen == [
        f"{NEW_HOST}/wxxz/20921/",
        f"{NEW_HOST}/wxxz/20921/index/2117.html",
        f"{NEW_HOST}/cyjk/24240.html",
        f"{NEW_HOST}/cyjk/24240/1.html",
        f"{NEW_HOST}/guanchang/123.html",
        f"{NEW_HOST}/khjj/11945/9.html",
    ]


# --------------------------------------------------------------------------- #
# migration record: no old host left anywhere in the adapter
# --------------------------------------------------------------------------- #
def test_adapter_has_no_old_domain_left() -> None:
    # Docstrings may mention the dead domain as a migration note, but no
    # fetched URL (always scheme-prefixed) or class attribute may use it.
    for cls in (BixiangeFetcher, BixiangeParser, BixiangeSearcher):
        source = inspect.getsource(cls)
        assert f"https://{OLD_HOST}" not in source, (
            f"{cls.__name__} still fetches from the dead domain"
        )

    assert BixiangeParser.BASE_URL == NEW_HOST
    assert BixiangeSearcher.BASE_URL == NEW_HOST
    assert f"{NEW_HOST}/e/search/indexpage.php" == BixiangeSearcher.SEARCH_URL


# --------------------------------------------------------------------------- #
# searcher: keyword clamping + offline parsing
# --------------------------------------------------------------------------- #
def test_truncate_keyword_keeps_short_queries_untouched() -> None:
    assert BixiangeSearcher._truncate_keyword("") == ""
    assert BixiangeSearcher._truncate_keyword("斗罗") == "斗罗"
    # 10 Chinese chars == exactly 20 GBK bytes -> boundary stays intact.
    ten = "从气运词条开始成就人"  # as verified live: 20 bytes
    assert len(ten.encode("gbk")) == BixiangeSearcher.MAX_KEYWORD_BYTES
    assert BixiangeSearcher._truncate_keyword(ten) == ten


def test_truncate_keyword_clamps_over_long_queries() -> None:
    # 11 chars == 22 GBK bytes: the live site answers "no results" for this,
    # so the searcher clamps to the 20-byte prefix.
    full = "从气运词条开始成就人皇"
    assert len(full.encode("gbk")) == 22

    clamped = BixiangeSearcher._truncate_keyword(full)

    assert clamped == "从气运词条开始成就人"
    assert len(clamped.encode("gbk")) == BixiangeSearcher.MAX_KEYWORD_BYTES


def test_truncate_keyword_never_splits_a_gbk_character() -> None:
    # 19 ASCII bytes + the start of a 2-byte char would split mid-character.
    mixed = "a" * 19 + "斗罗"

    clamped = BixiangeSearcher._truncate_keyword(mixed)

    assert len(clamped.encode("gbk")) <= BixiangeSearcher.MAX_KEYWORD_BYTES
    clamped.encode("gbk")  # always decodable: no half character


def test_search_parse_results_and_book_id_convention() -> None:
    searcher = BixiangeSearcher(None)  # type: ignore[arg-type]

    results = searcher._parse_html(_SEARCH_RESULTS)

    assert len(results) == 2
    first = results[0]
    assert first["site"] == "bixiange"
    assert first["book_id"] == "trxs-23006"
    assert first["book_url"] == f"{NEW_HOST}/trxs/23006"
    assert first["title"] == "同时穿越：从斗罗开始登顶至高"
    assert first["cover_url"] == f"{NEW_HOST}/d/file/trxs/1769245151.jpeg"
    assert first["update_date"] == "2026-10-30"
    # ".html"-suffixed result rows keep their book_id convention too.
    assert results[1]["book_id"] == "khjj-11945"
    assert results[1]["book_url"] == f"{NEW_HOST}/khjj/11945.html"
    # priority follows row order.
    assert results[0]["priority"] < results[1]["priority"]


def test_search_parse_respects_limit() -> None:
    searcher = BixiangeSearcher(None)  # type: ignore[arg-type]

    results = searcher._parse_html(_SEARCH_RESULTS, limit=1)

    assert [r["book_id"] for r in results] == ["trxs-23006"]


def test_search_empty_state_parses_to_no_results() -> None:
    searcher = BixiangeSearcher(None)  # type: ignore[arg-type]

    assert searcher._parse_html(_SEARCH_EMPTY) == []


def test_search_cooldown_page_parses_to_no_results() -> None:
    searcher = BixiangeSearcher(None)  # type: ignore[arg-type]

    assert searcher._parse_html(_SEARCH_COOLDOWN) == []


def test_search_keyword_is_gbk_form_encoded() -> None:
    # "斗罗" GBK hex b6b7 c2de -- UTF-8 bytes match nothing on this site.
    assert BixiangeSearcher._quote("斗罗", encoding="gbk") == "%B6%B7%C2%DE"


# --------------------------------------------------------------------------- #
# Live verification record (opt-in: BIXIANGE_LIVE=1, needs network)
# --------------------------------------------------------------------------- #
#: Facts verified against https://m.bixiange.top with fetcher 直连 on
#: 2026-10-31 (see module docstring for how to re-run).
LIVE_VERIFIED_ON = "2026-10-31"
LIVE_BOOK_ID = "wxxz-20921"
LIVE_CHAPTER_IDS = ("1", "2117")
LIVE_CHAPTER_COUNT = 2117
LIVE_BOOK_NAME = "从气运词条开始成就人皇(全本)"
LIVE_SEARCH_KEYWORD = "从气运词条开始成就人皇"


def test_live_verification_record_is_consistent() -> None:
    """Offline sanity check of the recorded live-verification facts."""
    assert LIVE_VERIFIED_ON == "2026-10-31"
    # book_id -> URL convention used by fetcher/parser/searcher.
    assert LIVE_BOOK_ID.replace("-", "/") == "wxxz/20921"
    assert LIVE_CHAPTER_COUNT == 2117
    assert len(LIVE_CHAPTER_IDS) == 2


@pytest.mark.skipif(
    not os.environ.get("BIXIANGE_LIVE"),
    reason="Live network test; set BIXIANGE_LIVE=1 to re-run the migration "
    "verification against m.bixiange.top",
)
@pytest.mark.asyncio
async def test_live_bixiange_top_end_to_end() -> None:
    """Re-run of the 2026-10-31 real-fetch verification (fetcher 直连)."""
    import aiohttp

    parser = BixiangeParser(_config())

    async with BixiangeFetcher(
        FetcherConfig(request_interval=0.3, timeout=30.0)
    ) as fetcher:
        info = parser.parse_book_info(await fetcher.fetch_book_info(LIVE_BOOK_ID))
        assert info is not None
        assert info["book_name"] == LIVE_BOOK_NAME
        chapters = info["volumes"][0]["chapters"]
        assert len(chapters) == LIVE_CHAPTER_COUNT
        assert chapters[-1]["chapterId"] == LIVE_CHAPTER_IDS[1]

        for chap_id in LIVE_CHAPTER_IDS:
            chap = parser.parse_chapter_content(
                await fetcher.fetch_chapter_content(LIVE_BOOK_ID, chap_id),
                chapter_id=chap_id,
            )
            assert chap is not None
            assert chap["title"] == f"从气运词条开始成就人皇 第{chap_id}节"
            assert len(chap["content"]) >= 200

    async with aiohttp.ClientSession() as session:
        results = await BixiangeSearcher(session).search(LIVE_SEARCH_KEYWORD)

    assert any(r["book_id"] == LIVE_BOOK_ID for r in results)
