#!/usr/bin/env python3
"""
tests.apps.test_search_ranking_golden
-------------------------------------

Deterministic, whole-result-set goldens for aggregate search ranking.

``tests/plugins/test_ranking*.py`` pin individual helpers against hand-written
rows and ``tests/apps/test_search_contract_matrix.py`` pins transport parity
(REST / embedded / CLI / desktop) for a three-row fixture.  Neither locks the
*combined* behaviour of one realistic, 50+ row result set: the tier order, the
0..1 score surface, the intent verdict, multi-word matching, every ``--sort``
key and the local filters, all of which cross the ranking -> finalise -> CLI
-> REST layers.  This module does exactly that, and nothing else, so a change
to any one layer that moves a row fails here even when each unit still passes.

The fixture uses only absolute update dates older than two years, so the
recency bucket is the constant ``0.5`` and the expected order does not drift
as wall-clock time passes.  Every expected sequence is deliberate: the
comments above each group explain which ranking rule it pins.

Two determinism properties are checked, not assumed:

* the same rows in a different arrival order produce the same output (the
  ``(site, normalized title, book_id, ...)`` tie-break is total over content);
* a fresh interpreter with a different ``PYTHONHASHSEED`` produces byte
  identical JSON, so no set/dict iteration order leaks into a result.
"""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
from contextlib import nullcontext
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from novel_downloader.apps.api import create_app
from novel_downloader.apps.cli import ui as cli_ui
from novel_downloader.plugins.ranking import (
    SORT_KEYS,
    default_order,
    detect_query_intent,
    match_tier_of,
    normalize_text,
    parse_update_date,
    parse_word_count,
    rank_score,
)
from novel_downloader.plugins.search import _finalize, query_intent_of
from tests.apps.test_search_contract_matrix import _install_ranked, _run_cli_async

TITLE = "斗破苍穹"
AUTHOR = "天蚕土豆"
MULTI = f"{TITLE} {AUTHOR}"

#: One fixed instant for score comparisons.  Every fixture date is more than
#: two years older, so the recency bucket is ``0.5`` under this clock exactly
#: as it is under the real clock when the test runs.
NOW = datetime(2024, 6, 15, 12, 0, 0)

SITES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]

#: The priority each site normally advertises (lower is better).  Individual
#: rows may override it; the tie-break fixture rows all share one value.
SITE_PRIORITY = {
    "alpha": 5,
    "beta": 30,
    "gamma": 80,
    "delta": 150,
    "epsilon": 300,
    "zeta": 700,
}


def _row(
    site: str,
    book_id: str,
    title: str,
    author: str,
    *,
    words: str = "10万字",
    update: str = "2019-01-01",
    cover: bool = False,
    latest: bool = False,
    priority: int | float | None = None,
) -> dict[str, Any]:
    """Build one raw searcher row (no derived telemetry)."""
    return {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://example.invalid/{site}/{book_id}",
        "cover_url": f"https://example.invalid/{site}/{book_id}.jpg" if cover else "",
        "title": title,
        "author": author,
        "latest_chapter": "第100章" if latest else "",
        "update_date": update,
        "word_count": words,
        "priority": SITE_PRIORITY[site] if priority is None else priority,
    }


#: 53 rows.  Book-id prefixes name the group a row exercises:
#:
#: ``t``  title query, ``a`` author query, ``w`` width/case, ``m`` multi-word,
#: ``q`` punctuation title, ``f`` local filter boundary, ``s`` score signal.
ROWS: list[dict[str, Any]] = [
    # -- title query: exact equality (tier 4) --------------------------------
    # Rich metadata must not let a prefix/contains row overtake an exact hit.
    _row(
        "alpha",
        "t01",
        TITLE,
        AUTHOR,
        words="300万字",
        update="2020-01-01",
        cover=True,
        latest=True,
    ),
    _row("beta", "t02", TITLE, "某某", words="50万字", update="2019-06-01", cover=True),
    _row("gamma", "t03", TITLE, "", words="", update="", priority=2000),
    _row("delta", "t04", TITLE + "！", "标点作者", words="8万字", update="2018-08-01"),
    _row("epsilon", "t05", TITLE, "戊", words="3万字", update="2014-01-01"),
    # -- title query: prefix (tier 3) ---------------------------------------
    _row(
        "alpha",
        "t06",
        TITLE + "外传",
        AUTHOR,
        words="100万字",
        update="2020-02-01",
        cover=True,
        latest=True,
    ),
    _row(
        "beta",
        "t07",
        TITLE + "之雷霆震碎",
        "别人",
        words="20万字",
        update="2019-01-01",
        latest=True,
    ),
    _row(
        "gamma",
        "t08",
        TITLE + "（精校版）",
        "某作者",
        words="10万字",
        update="2018-01-01",
    ),
    _row(
        "epsilon",
        "t09",
        TITLE + " 外传",
        "空格作者",
        words="7万字",
        update="2017-05-01",
    ),
    _row("alpha", "t10", TITLE + "外传之重生", "", words="2万字", update="2015-01-01"),
    # -- title query: containment (tier 2) ----------------------------------
    _row(
        "delta",
        "t11",
        "重生之" + TITLE,
        "甲",
        words="80万字",
        update="2020-03-01",
        cover=True,
        latest=True,
    ),
    _row(
        "epsilon",
        "t12",
        "异界" + TITLE + "录",
        "乙",
        words="5万字",
        update="2017-01-01",
    ),
    # -- title query: partial character coverage (tier 1) -------------------
    _row("zeta", "t13", "斗破", "丙", words="1万字", update="2016-01-01"),
    _row("alpha", "t14", "苍穹", "丁", words="2万字", update="2015-01-01"),
    # Shares one character (斗) with the keyword: partial, not containment.
    _row(
        "epsilon",
        "t15",
        "斗罗大陆",
        "唐家三少",
        words="200万字",
        update="2019-09-01",
        cover=True,
    ),
    # -- title query: no character overlap (tier 0) -------------------------
    _row(
        "beta",
        "t16",
        "三体",
        "刘慈欣",
        words="300万字",
        update="2020-04-01",
        cover=True,
        latest=True,
    ),
    _row(
        "gamma",
        "t17",
        "活着",
        "余华",
        words="20万字",
        update="2019-02-01",
        cover=True,
        latest=True,
    ),
    _row(
        "delta",
        "t18",
        "诡秘之主",
        "爱潜水的乌贼",
        words="400万字",
        update="2020-05-01",
        cover=True,
        latest=True,
    ),
    # -- author query: exact / prefix / contains / partial / none -----------
    _row(
        "alpha",
        "a01",
        "武动乾坤",
        AUTHOR,
        words="300万字",
        update="2020-01-01",
        cover=True,
        latest=True,
    ),
    _row("beta", "a02", "大主宰", AUTHOR, words="", update="", priority=2000),
    _row(
        "gamma",
        "a03",
        "吞噬星空",
        AUTHOR + "丝",
        words="50万字",
        update="2019-01-01",
        cover=True,
    ),
    _row("delta", "a04", "某书", "我是" + AUTHOR, words="20万字", update="2018-01-01"),
    _row("epsilon", "a05", "某书2", "天蚕", words="10万字", update="2017-01-01"),
    # A title that merely equals the author name: no author-field hit.
    _row(
        "zeta",
        "a06",
        AUTHOR,
        "路人甲",
        words="300万字",
        update="2020-06-01",
        cover=True,
        latest=True,
    ),
    _row("alpha", "a07", "三体", "刘慈欣", words="20万字", update="2019-01-01"),
    _row("beta", "a08", "活着", "余华", words="5万字", update="2018-01-01"),
    # Punctuation is stripped by normalization, so this is an exact hit too.
    _row("delta", "a09", "某书3", "天蚕·土豆", words="15万字", update="2018-06-01"),
    _row("gamma", "a10", AUTHOR + "外传", "某作者", words="6万字", update="2017-01-01"),
    # -- full-width / half-width / case -------------------------------------
    _row("alpha", "w01", "ＤＯＵＰＯ", "作者甲", update="2019-03-01"),
    _row("beta", "w02", "doupo", "作者乙", update="2019-03-01"),
    _row("gamma", "w03", "Doupo Legend", "作者丙", update="2019-03-01"),
    _row("delta", "w04", "The Doupo Archive", "作者丁", update="2019-03-01"),
    _row("epsilon", "w05", "Sword Art Online", "Reki", update="2019-03-01"),
    _row("zeta", "w06", "Some Title", "Ｒｅｋｉ", update="2019-03-01"),
    _row("alpha", "w07", "Another Title", "Reki", update="2019-03-01"),
    _row("gamma", "w08", "DOUPO Legend", "作者戊", update="2019-03-01"),
    # A title that is exactly the author name: a title hit, not an author hit.
    _row("zeta", "w09", "reki", "路人", update="2019-03-01"),
    _row("epsilon", "w10", "Some Other", "Rekis", update="2019-03-01"),
    # -- multi-word ("title author") matching -------------------------------
    _row(
        "alpha", "m01", TITLE, AUTHOR, words="50万字", update="2019-01-01", cover=True
    ),
    _row(
        "beta",
        "m02",
        TITLE + "（精校版）",
        AUTHOR,
        words="50万字",
        update="2019-01-01",
        cover=True,
    ),
    _row(
        "gamma",
        "m03",
        TITLE + "外传",
        "别人",
        words="50万字",
        update="2019-01-01",
        cover=True,
    ),
    _row(
        "delta",
        "m04",
        "无关书名",
        AUTHOR,
        words="50万字",
        update="2019-01-01",
        cover=True,
    ),
    _row(
        "epsilon",
        "m05",
        "三体",
        "刘慈欣",
        words="50万字",
        update="2019-01-01",
        cover=True,
    ),
    # Three content-identical rows: only (site, title, book_id) can separate
    # them, and that tie-break is ascending even for a descending key.
    _row(
        "gamma", "m06", TITLE, AUTHOR, words="50万字", update="2019-01-01", priority=30
    ),
    _row(
        "alpha", "m07", TITLE, AUTHOR, words="50万字", update="2019-01-01", priority=30
    ),
    _row(
        "beta", "m08", TITLE, AUTHOR, words="50万字", update="2019-01-01", priority=30
    ),
    # -- punctuation / separator titles used as the whole keyword -----------
    _row(
        "zeta", "q01", "你好，旧时光", "八月长安", words="30万字", update="2018-01-01"
    ),
    _row(
        "alpha",
        "q02",
        "Re:Zero-从零开始的异世界生活",
        "作者己",
        words="30万字",
        update="2018-01-01",
    ),
    # -- filters: unknown fields and the inclusive 50000 boundary ----------
    _row("beta", "f01", TITLE, "", words="未知", update="暂无"),
    _row("zeta", "f02", TITLE, "边界作者", words="50000", update="2020-01-01"),
    # -- quality score signal: only the word count changes ------------------
    _row("alpha", "s01", "评分测试", "评分作者", words="1万字", update="2019-01-01"),
    _row("alpha", "s02", "评分测试", "评分作者", words="10万字", update="2019-01-01"),
    _row("alpha", "s03", "评分测试", "评分作者", words="100万字", update="2019-01-01"),
]

#: ``quality`` under the detected ``title`` intent.  Tier blocks (4 -> 0)
#: dominate; inside a block the score is non-increasing and exact ties fall
#: back to the content tie-break (``m07``, ``m08``, ``m06`` by site).
TITLE_QUALITY_ORDER = [
    "t01", "m01", "t02", "m07", "m08", "m06", "t04", "f01", "t05", "f02",
    "t03", "t06", "m02", "t07", "m03", "t10", "t08", "t09", "t11", "t12",
    "t14", "t15", "t13", "a01", "t16", "t18", "t17", "a06", "a03", "m04",
    "s03", "m05", "q02", "a07", "w07", "w01", "s02", "w02", "a08", "w03",
    "w08", "s01", "a10", "a04", "a09", "w04", "w10", "w05", "a05", "q01",
    "w09", "w06", "a02",
]  # fmt: skip

#: ``quality`` under the detected ``author`` intent.  The tier is the author
#: field's tier, so a sparse exact author hit (``a02``) beats rich unrelated
#: rows (``a06``) and a rich title collision (same row) loses its tier-4 spot.
AUTHOR_QUALITY_ORDER = [
    "t01", "a01", "t06", "m01", "m02", "m04", "m07", "m08", "m06", "a09",
    "a02", "a03", "a04", "a05", "a06", "a10", "t16", "t18", "t11", "t17",
    "t02", "t07", "m03", "t15", "s03", "m05", "q02", "a07", "w07", "w01",
    "s02", "w02", "a08", "t10", "t14", "w03", "w08", "t08", "s01", "w04",
    "t04", "f01", "w10", "w05", "q01", "t09", "t12", "t05", "w09", "w06",
    "f02", "t13", "t03",
]  # fmt: skip

#: Non-zero ``match_tier`` under ``intent="title"`` / ``"author"``.  Every id
#: not named here is expected to report tier 0.
TITLE_TIERS = {
    "t01": 4, "m01": 4, "t02": 4, "m07": 4, "m08": 4, "m06": 4, "t04": 4,
    "f01": 4, "t05": 4, "f02": 4, "t03": 4,
    "t06": 3, "m02": 3, "t07": 3, "m03": 3, "t10": 3, "t08": 3, "t09": 3,
    "t11": 2, "t12": 2,
    "t14": 1, "t15": 1, "t13": 1,
}  # fmt: skip

AUTHOR_TIERS = {
    "t01": 4, "a01": 4, "t06": 4, "m01": 4, "m02": 4, "m04": 4, "m07": 4,
    "m08": 4, "m06": 4, "a09": 4, "a02": 4,
    "a03": 3,
    "a04": 2,
    "a05": 1,
}  # fmt: skip


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _by_id(rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    return {row["book_id"]: row for row in (ROWS if rows is None else rows)}


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["book_id"]) for row in rows]


def _finalize_rows(
    keyword: str,
    *,
    sort: str = "quality",
    order: str | None = None,
    rows: list[dict[str, Any]] | None = None,
    limit: int | None = None,
    **filters: Any,
) -> list[dict[str, Any]]:
    """Run the real finalisation (cleaning, ranking, filtering) on a copy."""
    return _finalize(
        [dict(row) for row in (ROWS if rows is None else rows)],
        keyword=keyword,
        sort=sort,
        order=order,
        limit=limit,
        group=False,
        min_relevance=None,
        **filters,
    )


def _expected_tiers(mapping: dict[str, int]) -> dict[str, int]:
    return {book_id: mapping.get(book_id, 0) for book_id in _by_id()}


def _assert_tier_blocks_non_increasing(rows: list[dict[str, Any]]) -> None:
    """Scores may only fall while staying in one match tier."""
    tiers = [int(row["match_tier"]) for row in rows]
    scores = [float(row["rank_score"]) for row in rows]
    assert tiers == sorted(tiers, reverse=True)
    for current, following in pairwise(range(len(rows))):
        if tiers[current] == tiers[following]:
            assert scores[current] >= scores[following], (
                rows[current]["book_id"],
                rows[following]["book_id"],
            )


def _tie_key(row: dict[str, Any]) -> tuple[str, ...]:
    """The documented content tie-break, mirrored independently here."""
    return (
        str(row.get("site") or ""),
        normalize_text(row.get("title")),
        str(row.get("book_id") or ""),
        normalize_text(row.get("author")),
        str(row.get("book_url") or ""),
        str(row.get("word_count") or ""),
        str(row.get("update_date") or ""),
        str(row.get("latest_chapter") or ""),
        str(row.get("cover_url") or ""),
        str(row.get("priority") or ""),
    )


def _priority_key(row: dict[str, Any]) -> float:
    try:
        value = float(row.get("priority", 0))
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(value) else value


def _words_key(row: dict[str, Any]) -> float:
    words = parse_word_count(row.get("word_count"))
    return -1.0 if words is None else words


def _update_key(row: dict[str, Any]) -> float:
    if not str(row.get("update_date") or "").strip():
        return -1.0
    parsed = parse_update_date(row.get("update_date"), now=NOW)
    return -1.0 if parsed is None else float(parsed.toordinal())


def _primary_key(row: dict[str, Any], by: str) -> Any:
    """Independent re-statement of each sort key's primary component."""
    if by == "quality":
        return (
            match_tier_of(row, TITLE, intent="title"),
            rank_score(row, TITLE, intent="title", now=NOW),
        )
    if by == "priority":
        return _priority_key(row)
    if by == "site":
        return normalize_text(row.get("site"))
    if by == "title":
        return normalize_text(row.get("title"))
    if by == "author":
        return normalize_text(row.get("author"))
    if by == "words":
        return _words_key(row)
    if by == "update":
        return _update_key(row)
    raise AssertionError(f"unhandled sort key {by}")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/logging/selection off the real profile and off the tty."""
    from novel_downloader.apps.cli.commands import search as module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.setattr(
        module, "load_or_init_config", lambda *_a, **_k: {"general": {}}
    )
    monkeypatch.setattr(module.ui, "setup_logging", lambda **_k: None)
    monkeypatch.setattr(module.ui, "status", lambda _message: nullcontext())
    monkeypatch.setattr(module, "_unknown_search_sites", lambda _sites: [])
    monkeypatch.setattr(module, "_available_search_sites", lambda: [])


@pytest_asyncio.fixture
async def rest_client(tmp_path: Path) -> Any:
    application = create_app(token="", output_dir=str(tmp_path / "downloads"))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# golden 1/2: the full quality order under a title and an author query
# ---------------------------------------------------------------------------


def test_golden_title_quality_order_scores_and_tiers() -> None:
    rows = _finalize_rows(TITLE)

    assert _ids(rows) == TITLE_QUALITY_ORDER
    assert len(rows) == len(ROWS) == len(_by_id())
    assert {row["query_intent"] for row in rows} == {"title"}
    assert query_intent_of(rows) == "title"

    expected_tiers = _expected_tiers(TITLE_TIERS)
    assert {
        str(row["book_id"]): int(row["match_tier"]) for row in rows
    } == expected_tiers

    for row in rows:
        score = row["rank_score"]
        assert isinstance(score, float) and not isinstance(score, bool)
        assert math.isfinite(score)
        assert 0.0 <= score <= 1.0
    _assert_tier_blocks_non_increasing(rows)

    # A sparse exact hit outranks a metadata-rich lower tier: t03 has no word
    # count, date, cover or chapter and still beats rich prefix row t06.
    by_id = _by_id(rows)
    assert by_id["t03"]["rank_score"] < by_id["t06"]["rank_score"]
    assert TITLE_QUALITY_ORDER.index("t03") < TITLE_QUALITY_ORDER.index("t06")
    # Inside tier 4 the score still orders rows: rich t01 > mid t02 > sparse t03.
    assert by_id["t01"]["rank_score"] > by_id["t02"]["rank_score"]
    assert by_id["t02"]["rank_score"] > by_id["t03"]["rank_score"]


def test_golden_author_quality_order_scores_and_tiers() -> None:
    rows = _finalize_rows(AUTHOR)

    assert _ids(rows) == AUTHOR_QUALITY_ORDER
    assert {row["query_intent"] for row in rows} == {"author"}
    assert query_intent_of(rows) == "author"

    expected_tiers = _expected_tiers(AUTHOR_TIERS)
    assert {
        str(row["book_id"]): int(row["match_tier"]) for row in rows
    } == expected_tiers
    _assert_tier_blocks_non_increasing(rows)

    by_id = _by_id(rows)
    # The sparse exact author hit (a02) beats the rich title collision (a06),
    # which is the whole point of the primary-field tier outer key.
    assert by_id["a02"]["match_tier"] == 4
    assert by_id["a06"]["match_tier"] == 0
    assert AUTHOR_QUALITY_ORDER.index("a02") < AUTHOR_QUALITY_ORDER.index("a06")
    assert by_id["a06"]["rank_score"] > by_id["a02"]["rank_score"]


# ---------------------------------------------------------------------------
# rank_score surface: range, finiteness and per-signal monotonicity
# ---------------------------------------------------------------------------


def test_rank_score_stays_in_range_for_hostile_rows() -> None:
    hostile: list[dict[str, Any]] = [
        {
            "site": "alpha",
            "book_id": "h1",
            "book_url": "https://example.invalid/alpha/h1",
            "cover_url": "",
            "title": "",
            "author": "",
            "latest_chapter": "",
            "update_date": "",
            "word_count": "",
            "priority": None,
        },
        {**_by_id()["t01"], "book_id": "h2", "priority": float("nan")},
        {
            **_by_id()["t01"],
            "book_id": "h3",
            "priority": float("inf"),
            "word_count": "9" * 64,
            "update_date": "9999-12-31T23:59:59+08:00",
        },
        {
            **_by_id()["t01"],
            "book_id": "h4",
            "update_date": "0001-01-01T00:00:00",
            "word_count": "1e999",
            "priority": object(),
        },
        # Aware clock and aware update date must fold, not raise.
        {
            **_by_id()["t01"],
            "book_id": "h5",
            "update_date": "2024-06-15T08:00:00+08:00",
        },
    ]
    for keyword in (TITLE, AUTHOR, "", MULTI, "完全不存在"):
        for row in [*ROWS, *hostile]:
            score = rank_score(row, keyword, now=NOW)
            assert math.isfinite(score), (keyword, row["book_id"])
            assert 0.0 <= score <= 1.0, (keyword, row["book_id"], score)

    aware_now = datetime(2024, 6, 15, 12, 0, 0).astimezone()
    for row in hostile:
        score = rank_score(row, TITLE, now=aware_now)
        assert math.isfinite(score)
        assert 0.0 <= score <= 1.0


def test_rank_score_is_monotonic_in_each_signal() -> None:
    base = _row("alpha", "rel", TITLE, "", update="2019-01-01")

    # Match kind: exact > prefix > contains > partial > none.
    match_rows = [
        {**base, "book_id": "exact", "title": TITLE},
        {**base, "book_id": "prefix", "title": TITLE + "外传"},
        {**base, "book_id": "contains", "title": "重生之" + TITLE},
        {**base, "book_id": "partial", "title": "斗破"},
        {**base, "book_id": "none", "title": "三体"},
    ]
    assert [match_tier_of(row, TITLE, intent="title") for row in match_rows] == [
        4, 3, 2, 1, 0,
    ]  # fmt: skip
    match_scores = [
        rank_score(row, TITLE, intent="title", now=NOW) for row in match_rows
    ]
    assert match_scores == sorted(match_scores, reverse=True)
    assert len(set(match_scores)) == len(match_scores)

    # Word count: only the word count differs.
    by_id = _by_id()
    words_scores = [
        rank_score(by_id[book_id], "评分测试", now=NOW)
        for book_id in ("s01", "s02", "s03")
    ]
    assert words_scores == sorted(words_scores)
    assert len(set(words_scores)) == 3

    # Recency: each row lands in a different bucket (all else equal).
    recency_dates = [
        "2024-06-15",
        "2024-06-10",
        "2024-05-20",
        "2024-04-01",
        "2023-09-01",
        "2022-10-01",
        "2010-01-01",
    ]
    recency_scores = [
        rank_score({**base, "update_date": value}, TITLE, now=NOW)
        for value in recency_dates
    ]
    assert recency_scores == sorted(recency_scores, reverse=True)
    assert len(set(recency_scores)) == len(recency_scores)

    # Completeness: cover and latest chapter each add the same amount.
    completeness = {}
    for name, cover, latest in (
        ("none", False, False),
        ("cover", True, False),
        ("latest", False, True),
        ("both", True, True),
    ):
        row = {**base, "cover_url": "https://example.invalid/c.jpg" if cover else ""}
        row["latest_chapter"] = "第1章" if latest else ""
        completeness[name] = rank_score(row, TITLE, now=NOW)
    assert completeness["cover"] > completeness["none"]
    assert completeness["latest"] > completeness["none"]
    assert completeness["both"] > completeness["cover"]
    assert completeness["cover"] == pytest.approx(completeness["latest"])

    # Site priority: lower priority value is a better site.
    priority_scores = [
        rank_score({**base, "priority": value}, TITLE, now=NOW)
        for value in (5, 30, 80, 150, 300, 700, 2000)
    ]
    assert priority_scores == sorted(priority_scores, reverse=True)
    assert len(set(priority_scores)) == len(priority_scores)


# ---------------------------------------------------------------------------
# intent matrix
# ---------------------------------------------------------------------------


def test_query_intent_decision_matrix() -> None:
    # The verdict is a property of the whole result set, not just the string.
    assert detect_query_intent(TITLE, ROWS) == "title"
    assert detect_query_intent(AUTHOR, ROWS) == "author"
    assert detect_query_intent("土豆", ROWS) == "author"
    assert detect_query_intent("完全不存在", ROWS) == "either"
    assert detect_query_intent("", ROWS) == "either"

    # A title that merely equals the pen name: under an author search the
    # result set contains no author-field evidence, so the verdict is title.
    collision = [_row("alpha", "c1", AUTHOR, "路人甲")]
    assert detect_query_intent(AUTHOR, collision) == "title"
    # Mirror image: a real author hit is decisive.
    author_hit = [_row("alpha", "c2", TITLE, AUTHOR)]
    assert detect_query_intent(AUTHOR, author_hit) == "author"
    # Balanced evidence is not a verdict.
    ambiguous = [
        _row("alpha", "c3", "测试用的书名", "甲"),
        _row("beta", "c4", "乙", "测试用的书名"),
    ]
    assert detect_query_intent("测试用的书名", ambiguous) == "either"
    assert detect_query_intent("量子力学", ambiguous) == "either"

    # ``_finalize`` reports the verdict on every row and the list-level helper
    # agrees with it.
    for keyword, expected in (
        (TITLE, "title"),
        (AUTHOR, "author"),
        ("完全不存在", None),
        ("", None),
    ):
        rows = _finalize_rows(keyword)
        assert {row["query_intent"] for row in rows} == {expected}
        assert query_intent_of(rows) == expected

    # An explicit non-quality sort never uses the verdict, so it reports None
    # (and the rows expose no primary-field claim either).
    for sort_key in ("priority", "site", "title", "author", "words", "update"):
        rows = _finalize_rows(TITLE, sort=sort_key)
        assert {row["query_intent"] for row in rows} == {None}
        assert query_intent_of(rows) is None

    # Defensive list-level reads: mixed, invalid and absent values are not
    # guessed.  ``query_intent_of`` is the single reader every front end uses.
    assert query_intent_of([]) is None
    assert (
        query_intent_of([{"query_intent": "title"}, {"query_intent": "title"}])
        == "title"
    )
    assert (
        query_intent_of([{"query_intent": "title"}, {"query_intent": "author"}]) is None
    )
    assert (
        query_intent_of([{"query_intent": "title"}, {"query_intent": None}]) == "title"
    )
    assert query_intent_of([{"query_intent": "book"}]) is None
    assert query_intent_of([{}]) is None


# ---------------------------------------------------------------------------
# multi-word queries
# ---------------------------------------------------------------------------


def _multi_rows() -> list[dict[str, Any]]:
    return [_by_id()[book_id] for book_id in ("m01", "m02", "m03", "m04", "m05")]


def test_multi_word_query_hit_coverage_and_order_independence() -> None:
    rows = _finalize_rows(MULTI, rows=_multi_rows())
    scores = {row["book_id"]: row["rank_score"] for row in rows}

    # Both tokens hit (m01/m02) > only one token hits (m03/m04) > no token.
    assert min(scores["m01"], scores["m02"]) > max(scores["m03"], scores["m04"])
    assert max(scores["m03"], scores["m04"]) > scores["m05"]
    assert _ids(rows) == ["m01", "m02", "m04", "m03", "m05"]

    reversed_rows = _finalize_rows(f"{AUTHOR} {TITLE}", rows=_multi_rows())
    assert _ids(reversed_rows) == _ids(rows)
    reversed_scores = {row["book_id"]: row["rank_score"] for row in reversed_rows}
    assert reversed_scores == scores

    # A repeated token carries no extra evidence and must not disable the
    # multi-token path (or consume its 2-4 token window).
    repeated = _finalize_rows(f"{MULTI} {AUTHOR} {AUTHOR}", rows=_multi_rows())
    assert _ids(repeated) == _ids(rows)
    assert {row["book_id"]: row["rank_score"] for row in repeated} == scores

    # Tiers are the best tier any token reaches, so a token that hits either
    # field exactly reports 4 even when the *other* token also hits; a title
    # derivative reports the prefix tier and a no-token row reports 0.
    tiers = {row["book_id"]: row["match_tier"] for row in rows}
    assert tiers["m01"] == 4
    assert tiers["m02"] == 4
    assert tiers["m04"] == 4
    assert tiers["m03"] == 3
    assert tiers["m05"] == 0


def test_multi_word_equal_scores_use_the_content_tie_break() -> None:
    tie_rows = [_by_id()[book_id] for book_id in ("m06", "m07", "m08")]
    reference = _finalize_rows(MULTI, rows=tie_rows)
    assert _ids(reference) == ["m07", "m08", "m06"]
    assert len({row["rank_score"] for row in reference}) == 1

    rng = random.Random(20261116)
    for _ in range(6):
        shuffled = list(tie_rows)
        rng.shuffle(shuffled)
        assert _ids(_finalize_rows(MULTI, rows=shuffled)) == _ids(reference)

    # Identical duplicates fold to one row; the fold does not disturb order.
    with_duplicate = [*tie_rows, dict(tie_rows[0])]
    folded = _finalize_rows(MULTI, rows=with_duplicate)
    assert _ids(folded) == ["m07", "m08", "m06"]


def test_content_tie_break_orders_by_site_then_title_then_id() -> None:
    # Every row has the same priority, so the primary key ties and the
    # pre-sort decides: site first, then normalized title, then book id.
    rows = [
        _row("alpha", "z9", TITLE, AUTHOR, priority=30),
        _row("alpha", "a1", TITLE, AUTHOR, priority=30),
        _row("alpha", "m5", TITLE + "外传", AUTHOR, priority=30),
        _row("beta", "q0", TITLE, AUTHOR, priority=30),
    ]
    assert _ids(_finalize_rows("", sort="priority", rows=rows)) == [
        "a1",
        "z9",
        "m5",
        "q0",
    ]

    # A same-site/same-id duplicate is folded before the sort, so it cannot
    # occupy a second position or perturb the tie-break.
    rows.append(_row("alpha", "a1", TITLE, AUTHOR, priority=30))
    assert _ids(_finalize_rows("", sort="priority", rows=rows)) == [
        "a1",
        "z9",
        "m5",
        "q0",
    ]


# ---------------------------------------------------------------------------
# every --sort key and both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sort_key", SORT_KEYS)
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_every_sort_key_and_direction_is_deterministic(
    sort_key: str, order: str
) -> None:
    rows = _finalize_rows(TITLE, sort=sort_key, order=order)
    assert len(rows) == len(ROWS)
    assert set(_ids(rows)) == set(_by_id())

    # The primary key must be monotone in the requested direction, and equal
    # primary keys must be in the ascending content tie-break order.
    for earlier, later in pairwise(rows):
        first = _primary_key(earlier, sort_key)
        second = _primary_key(later, sort_key)
        if order == "desc":
            assert first >= second, (
                sort_key,
                order,
                earlier["book_id"],
                later["book_id"],
            )
        else:
            assert first <= second, (
                sort_key,
                order,
                earlier["book_id"],
                later["book_id"],
            )
        if first == second:
            assert _tie_key(earlier) <= _tie_key(later)

    rng = random.Random(f"{sort_key}-{order}")
    expected = _ids(rows)
    for _ in range(3):
        shuffled = list(ROWS)
        rng.shuffle(shuffled)
        assert (
            _ids(_finalize_rows(TITLE, sort=sort_key, order=order, rows=shuffled))
            == expected
        )


def test_natural_directions_match_default_order() -> None:
    # Omitted ``--order`` must equal the key's documented natural direction.
    for sort_key in SORT_KEYS:
        natural = default_order(sort_key)
        implicit = _finalize_rows(TITLE, sort=sort_key)
        explicit = _finalize_rows(TITLE, sort=sort_key, order=natural)
        assert _ids(implicit) == _ids(explicit)
        assert [row["rank_score"] for row in implicit] == [
            row["rank_score"] for row in explicit
        ]


# ---------------------------------------------------------------------------
# local filters: order-preserving, before limit, and time-independent
# ---------------------------------------------------------------------------


_FILTER_CASES: list[tuple[dict[str, Any], str]] = [
    ({"min_words": 50000}, "word_count >= 50000"),
    ({"max_words": 50000}, "word_count <= 50000"),
    ({"updated_after": date(2019, 1, 1)}, "update_date >= 2019-01-01"),
    (
        {"min_words": 10000, "max_words": 200000},
        "10000 <= word_count <= 200000",
    ),
    (
        {
            "min_words": 0,
            "max_words": 1_000_000_000,
            "updated_after": date(2015, 1, 1),
        },
        "bounded",
    ),
]


def _passes(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    words = parse_word_count(row.get("word_count"))
    if "min_words" in filters and (words is None or words < filters["min_words"]):
        return False
    if "max_words" in filters and (words is None or words > filters["max_words"]):
        return False
    if "updated_after" in filters:
        updated = parse_update_date(row.get("update_date"), now=NOW)
        if updated is None or updated.date() < filters["updated_after"]:
            return False
    return True


@pytest.mark.parametrize(("filters", "label"), _FILTER_CASES)
def test_filters_preserve_the_ranked_order(filters: dict[str, Any], label: str) -> None:
    base = _finalize_rows(TITLE)
    filtered = _finalize_rows(TITLE, **filters)
    expected_ids = [
        book_id for book_id in _ids(base) if _passes(_by_id()[book_id], filters)
    ]

    # Filtering never reorders the surviving rows: the filtered list is a
    # subsequence of the unfiltered one.
    assert _ids(filtered) == expected_ids, label
    if len(expected_ids) < len(_ids(base)):
        assert len(expected_ids) < len(_ids(base))

    # Filtering first and ranking second yields the same order (the filter is
    # pure and order-independent for this fixture).
    externally = [row for row in ROWS if _passes(row, filters)]
    assert _ids(_finalize_rows(TITLE, rows=externally)) == expected_ids

    # ``limit`` slices after the filter, never before it.
    for limit in (1, 3, 10):
        sliced = _finalize_rows(TITLE, limit=limit, **filters)
        assert _ids(sliced) == expected_ids[:limit]


def test_filter_boundaries_and_unknown_fields() -> None:
    base_rows = _by_id(_finalize_rows(TITLE))
    # Without a filter an unparseable row is kept untouched.
    assert "f01" in base_rows
    assert base_rows["f01"]["word_count"] == "未知"
    assert base_rows["f01"]["update_date"] == "暂无"

    # Word bounds are inclusive, so the exact 50000 row passes both.
    assert "f02" in _by_id(_finalize_rows(TITLE, min_words=50000))
    assert "f02" in _by_id(_finalize_rows(TITLE, max_words=50000))
    # The date bound is inclusive too.
    assert "f02" in _by_id(_finalize_rows(TITLE, updated_after=date(2020, 1, 1)))
    # A row missing the filtered field is dropped only while that filter is on.
    assert "f01" not in _by_id(_finalize_rows(TITLE, min_words=0))
    assert "f01" not in _by_id(_finalize_rows(TITLE, updated_after=date(2015, 1, 1)))
    # Contradictory bounds simply match nothing (no crash, no reorder).
    assert _finalize_rows(TITLE, min_words=200000, max_words=10000) == []


# ---------------------------------------------------------------------------
# cross-process determinism
# ---------------------------------------------------------------------------


_SUBPROCESS_SCRIPT = """
import json
import sys

from novel_downloader.plugins.search import _finalize

payload = json.load(sys.stdin)
rows = _finalize(
    payload["rows"],
    keyword=payload["keyword"],
    sort=payload["sort"],
    order=payload["order"],
    limit=None,
    group=False,
    min_relevance=None,
)
print(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
"""


def _snapshot(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _run_in_subprocess(seed: str) -> str:
    root = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "PYTHONHASHSEED": seed,
    }
    payload = {
        "rows": ROWS,
        "keyword": TITLE,
        "sort": "quality",
        "order": None,
    }
    completed = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SCRIPT],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        cwd=root,
        env=environment,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_repeated_and_cross_process_runs_are_identical() -> None:
    before = _snapshot(ROWS)
    first = _finalize_rows(TITLE)
    second = _finalize_rows(TITLE)
    assert _snapshot(first) == _snapshot(second)
    # The rows fed to finalisation are annotated on copies, never in place.
    assert _snapshot(ROWS) == before

    rng = random.Random(4242)
    shuffled = list(ROWS)
    rng.shuffle(shuffled)
    assert _snapshot(_finalize_rows(TITLE, rows=shuffled)) == _snapshot(first)

    expected = _snapshot(first)
    seed_zero = _run_in_subprocess("0")
    seed_other = _run_in_subprocess("12345")
    # Byte identical across two interpreters that hash strings differently.
    assert seed_zero == seed_other
    assert json.loads(seed_zero) == json.loads(expected)


# ---------------------------------------------------------------------------
# CLI JSON / CLI table / REST all expose the same order
# ---------------------------------------------------------------------------


def _cli_args(sort_key: str | None, order: str | None) -> list[str]:
    argv: list[str] = []
    for site in SITES:
        argv += ["--site", site]
    argv += ["--site-limit", "60"]
    if sort_key is not None:
        argv += ["--sort", sort_key]
    if order is not None:
        argv += ["--order", order]
    return [*argv, TITLE]


def _capture_cli_table(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Render the real paged table and collect every page's row cells.

    ``select_search_result`` paginates ten rows at a time; the fake chooser
    always asks for the next page (``"n"``) until the last page, where it
    cancels.  That exercises the production renderer and its ordering instead
    of capturing the raw result list.
    """
    captured: list[list[str]] = []

    def fake_render(_title: str, _columns: Any, rows: Any) -> None:
        captured.extend([list(row) for row in rows])

    def fake_choice(_question: str, choices: Any) -> str:
        return "n" if "n" in choices else ""

    monkeypatch.setattr(cli_ui, "render_table", fake_render)
    monkeypatch.setattr(cli_ui, "prompt_choice", fake_choice)
    return captured


@pytest.mark.parametrize(
    ("sort_key", "order"),
    [(None, None), ("words", "desc"), ("title", "asc")],
)
@pytest.mark.asyncio
async def test_cli_table_json_and_rest_share_one_order(
    rest_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    sort_key: str | None,
    order: str | None,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_ranked(monkeypatch, ROWS, calls)

    effective_sort = sort_key or "quality"
    response = await rest_client.post(
        "/api/v1/search",
        json={
            "keyword": TITLE,
            "sites": SITES,
            "per_site_limit": 60,
            "sort": effective_sort,
            "order": order,
        },
    )
    assert response.status_code == 200
    rest_payload = response.json()
    rest_rows = rest_payload["results"]

    code, out, _err = await _run_cli_async(["--json", *_cli_args(sort_key, order)])
    assert code == 0
    cli_rows = json.loads(out)

    captured = _capture_cli_table(monkeypatch)
    human_code, _human_out, _human_err = await _run_cli_async(
        _cli_args(sort_key, order)
    )
    assert human_code == 0
    # Column 6 is "Book ID" (``#``, Title, Author, Latest, Updated, Site, ID).
    table_ids = [row[6] for row in captured]

    rest_ids = [row["book_id"] for row in rest_rows]
    cli_ids = [row["book_id"] for row in cli_rows]
    assert len(rest_ids) == len(cli_ids) == len(table_ids) == len(ROWS)
    assert rest_ids == cli_ids == table_ids
    if effective_sort == "quality":
        assert rest_ids == TITLE_QUALITY_ORDER
    else:
        assert rest_ids == _ids(_finalize_rows(TITLE, sort=effective_sort, order=order))
    assert [row["rank_score"] for row in cli_rows] == [
        row["rank_score"] for row in rest_rows
    ]
    assert [row["match_tier"] for row in cli_rows] == [
        row["match_tier"] for row in rest_rows
    ]
    assert len(calls) >= 2

    expected_intent = "title" if effective_sort == "quality" else None
    assert rest_payload["query_intent"] == expected_intent
    assert query_intent_of(rest_rows) == expected_intent
    assert {row["query_intent"] for row in cli_rows} == {expected_intent}
