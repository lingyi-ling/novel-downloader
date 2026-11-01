#!/usr/bin/env python3
"""Tests for the search-result quality ranking helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from novel_downloader.plugins.ranking import (
    SORT_KEYS,
    default_order,
    normalize_text,
    parse_update_date,
    parse_word_count,
    rank_score,
    sort_results,
)
from novel_downloader.schemas import SearchResult

NOW = datetime(2024, 6, 15, 12, 0, 0)


def _result(**overrides: object) -> SearchResult:
    base = {
        "site": "alpha",
        "book_id": "1",
        "book_url": "https://example.com/1",
        "cover_url": "",
        "title": "",
        "author": "",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 1000,
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# parse_word_count
# --------------------------------------------------------------------------- #
def test_parse_word_count_plain_number() -> None:
    assert parse_word_count("1234") == 1234.0


def test_parse_word_count_with_wan_unit() -> None:
    assert parse_word_count("12.3万字") == 123000.0


def test_parse_word_count_thousands_separator() -> None:
    assert parse_word_count("1,234") == 1234.0


def test_parse_word_count_magnitude_letter() -> None:
    assert parse_word_count("1.2M") == 1_200_000.0


def test_parse_word_count_qian_char() -> None:
    assert parse_word_count("3千字") == 3000.0


def test_parse_word_count_unknown_is_none() -> None:
    assert parse_word_count("-") is None
    assert parse_word_count("") is None
    assert parse_word_count("未知") is None
    assert parse_word_count(None) is None


# --------------------------------------------------------------------------- #
# parse_update_date
# --------------------------------------------------------------------------- #
def test_parse_update_date_absolute_iso() -> None:
    assert parse_update_date("2024-05-01") == datetime(2024, 5, 1)


def test_parse_update_date_absolute_cjk() -> None:
    assert parse_update_date("2024年5月1日") == datetime(2024, 5, 1)


def test_parse_update_date_relative_days() -> None:
    assert parse_update_date("3天前", now=NOW) == NOW - timedelta(days=3)


def test_parse_update_date_yesterday() -> None:
    assert parse_update_date("昨天", now=NOW) == NOW - timedelta(days=1)


def test_parse_update_date_just_now() -> None:
    assert parse_update_date("刚刚", now=NOW) == NOW


def test_parse_update_date_unknown_is_none() -> None:
    assert parse_update_date("-", now=NOW) is None
    assert parse_update_date("", now=NOW) is None
    assert parse_update_date(None, now=NOW) is None


# --------------------------------------------------------------------------- #
# normalize_text
# --------------------------------------------------------------------------- #
def test_normalize_text_strips_punctuation_and_case() -> None:
    assert normalize_text("《斗破 苍穹》!") == "斗破苍穹"
    assert normalize_text("ABC_def-1") == "abcdef1"


# --------------------------------------------------------------------------- #
# rank_score
# --------------------------------------------------------------------------- #
def test_rank_score_prefers_exact_title_match() -> None:
    keyword = "斗破苍穹"
    exact = _result(title="斗破苍穹")
    prefix = _result(title="斗破苍穹外传")
    unrelated = _result(title="完全无关的小说")

    exact_score = rank_score(exact, keyword, now=NOW)
    prefix_score = rank_score(prefix, keyword, now=NOW)
    unrelated_score = rank_score(unrelated, keyword, now=NOW)

    assert exact_score > prefix_score > unrelated_score


def test_rank_score_rises_with_word_count() -> None:
    keyword = "斗破苍穹"
    small = _result(title=keyword, word_count="1万字")
    big = _result(title=keyword, word_count="300万字")
    assert rank_score(big, keyword, now=NOW) > rank_score(small, keyword, now=NOW)


def test_rank_score_rises_with_recency() -> None:
    keyword = "斗破苍穹"
    fresh = _result(title=keyword, update_date="2024-06-14")
    old = _result(title=keyword, update_date="2022-01-01")
    assert rank_score(fresh, keyword, now=NOW) > rank_score(old, keyword, now=NOW)


def test_rank_score_tolerates_timezone_aware_update_date() -> None:
    # ``parse_update_date`` accepts ISO strings with a UTC offset, which yield
    # *aware* datetimes; the naive scoring clock must not crash on them.
    aware = _result(title="斗破苍穹", update_date="2024-05-01 12:00:00+08:00")

    score = rank_score(aware, "斗破苍穹", now=NOW)

    assert 0.0 <= score <= 1.0


def test_rank_score_tolerates_timezone_aware_clock() -> None:
    # The scoring clock itself may be aware (callers can pass one).  Scoring
    # must fold it onto the naive local timeline instead of raising
    # ``TypeError`` on the mixed subtraction, and it must agree exactly with
    # the naive wall clock it represents.
    aware_now = NOW.astimezone()
    naive_updated = _result(title="斗破苍穹", update_date="2024-06-14")
    aware_updated = _result(title="斗破苍穹", update_date="2024-06-14 00:00:00+00:00")

    for result in (naive_updated, aware_updated):
        score = rank_score(result, "斗破苍穹", now=aware_now)
        assert 0.0 <= score <= 1.0
        assert score == rank_score(result, "斗破苍穹", now=NOW)


def test_sort_results_quality_tolerates_aware_update_dates() -> None:
    keyword = "斗破苍穹"
    aware_fresh = _result(
        book_id="a",
        title=keyword,
        update_date="2024-06-14 00:00:00+00:00",
        word_count="300万字",
    )
    naive_old = _result(
        book_id="b",
        title=keyword,
        update_date="2020-01-01",
        word_count="300万字",
    )

    results = [naive_old, aware_fresh]
    sort_results(results, keyword=keyword, now=NOW)

    # Both entries survive the sort and the fresher one ranks first.
    assert [r["book_id"] for r in results] == ["a", "b"]


# --------------------------------------------------------------------------- #
# default_order / sort_results
# --------------------------------------------------------------------------- #
def test_default_order() -> None:
    assert SORT_KEYS[0] == "quality"
    assert default_order("quality") == "desc"
    assert default_order("words") == "desc"
    assert default_order("update") == "desc"
    assert default_order("priority") == "asc"
    assert default_order("title") == "asc"


def test_sort_results_quality_puts_best_first() -> None:
    keyword = "斗破苍穹"
    strong = _result(title=keyword, word_count="300万字", update_date="2024-06-14")
    weak = _result(title="无关书名", word_count="1万字", update_date="2021-01-01")

    results = [weak, strong]
    sort_results(results, keyword=keyword, now=NOW)

    assert results[0]["title"] == keyword


def test_sort_results_priority_matches_legacy_order() -> None:
    results = [
        _result(book_id="a", priority=5),
        _result(book_id="b", priority=100),
        _result(book_id="c", priority=20),
    ]
    sort_results(results, by="priority", now=NOW)
    assert [r["book_id"] for r in results] == ["a", "c", "b"]


def test_sort_results_breaks_ties_deterministically() -> None:
    # Identical scores => the pre-sort tie-break (site, title, book_id) applies.
    results = [
        _result(title="B", book_id="2"),
        _result(title="A", book_id="1"),
    ]
    sort_results(results, keyword="", now=NOW)
    assert [r["title"] for r in results] == ["A", "B"]


def test_sort_results_desc_reverses_primary_key() -> None:
    results = [
        _result(book_id="a", word_count="1万字"),
        _result(book_id="b", word_count="300万字"),
    ]
    sort_results(results, by="words", order="desc", now=NOW)
    assert [r["book_id"] for r in results] == ["b", "a"]

    sort_results(results, by="words", order="asc", now=NOW)
    assert [r["book_id"] for r in results] == ["a", "b"]


# --------------------------------------------------------------------------- #
# ranking sanity (quality regression)
# --------------------------------------------------------------------------- #
def test_exact_title_beats_derivative_even_if_derivative_is_fresher() -> None:
    exact = _result(
        title="斗破苍穹",
        author="天蚕土豆",
        update_date="2020-01-01",  # 4+ years old
        word_count="10万字",
    )
    derivative = _result(
        title="斗破苍穹外传",
        author="天蚕土豆",
        update_date="2024-06-14",  # updated yesterday
        word_count="10万字",  # same as the exact match
    )

    assert rank_score(exact, "斗破苍穹", now=NOW) > rank_score(
        derivative, "斗破苍穹", now=NOW
    )

    results = [derivative, exact]
    sort_results(results, keyword="斗破苍穹", now=NOW)
    assert [r["title"] for r in results] == ["斗破苍穹", "斗破苍穹外传"]


def test_same_title_multiple_sites_sorted_by_quality() -> None:
    keyword = "三体"
    poor = _result(
        site="alpha",
        book_id="a",
        title=keyword,
        word_count="10万字",
        cover_url="",
        latest_chapter="",
        update_date="2019-01-01",
    )
    rich = _result(
        site="beta",
        book_id="b",
        title=keyword,
        word_count="300万字",
        cover_url="https://example.com/cover.jpg",
        latest_chapter="第300章",
        update_date="2024-06-14",
    )
    medium = _result(
        site="gamma",
        book_id="c",
        title=keyword,
        word_count="100万字",
        cover_url="https://example.com/cover.jpg",
        latest_chapter="",
        update_date="2024-05-01",
    )

    results = [poor, rich, medium]
    sort_results(results, keyword=keyword, now=NOW)

    assert [r["book_id"] for r in results] == ["b", "c", "a"]


def test_missing_and_malformed_fields_do_not_crash() -> None:
    sparse = _result()
    del sparse["update_date"]
    del sparse["word_count"]
    del sparse["priority"]

    garbage: SearchResult = {
        "site": "x",
        "book_id": "y",
        "book_url": "",
        "cover_url": None,  # type: ignore[typeddict-item]
        "title": None,  # type: ignore[typeddict-item]
        "author": None,  # type: ignore[typeddict-item]
        "latest_chapter": 42,  # type: ignore[typeddict-item]
        "update_date": object(),  # type: ignore[typeddict-item]
        "word_count": object(),  # type: ignore[typeddict-item]
        "priority": "not-a-number",  # type: ignore[typeddict-item]
    }  # type: ignore[typeddict-item]

    for item in (sparse, garbage):
        score = rank_score(item, "斗破苍穹", now=NOW)
        assert 0.0 <= score <= 1.0

    results = [garbage, sparse]
    sort_results(results, keyword="斗破苍穹", now=NOW)
    assert len(results) == 2


def test_sort_results_update_survives_out_of_platform_dates() -> None:
    """平台时区库范围之外的日期（如 0001 / 9999 年）。

    Windows 上 naive ``timestamp()`` 会为此抛 OSError，旧实现让整个
    ``by="update"`` 排序一起崩；极端值按 UTC 解释后仍可正确排在两端。
    """
    results = [
        _result(book_id="mid", update_date="2024-06-14"),
        _result(book_id="oldest", update_date="0001-01-01"),
        _result(book_id="newest", update_date="9999-12-31"),
    ]
    sort_results(results, by="update", now=NOW)
    assert [r["book_id"] for r in results] == ["newest", "mid", "oldest"]

    sort_results(results, by="update", order="asc", now=NOW)
    assert [r["book_id"] for r in results] == ["oldest", "mid", "newest"]


# --------------------------------------------------------------------------- #
# snapshots (lock behaviour while optimising internals)
# --------------------------------------------------------------------------- #
def test_rank_score_snapshot() -> None:
    strong = _result(
        title="斗破苍穹",
        author="天蚕土豆",
        word_count="300万字",
        update_date="2024-06-15",
        cover_url="https://example.com/c.jpg",
        latest_chapter="第100章",
        priority=5,
    )
    weak = _result(
        site="beta",
        title="斗破苍穹外传",
        author="别人",
        word_count="未知",
        update_date="3天前",
        priority=500,
    )

    assert rank_score(strong, "斗破苍穹", now=NOW) == pytest.approx(
        0.9887954554582784, abs=1e-9
    )
    assert rank_score(weak, "斗破苍穹", now=NOW) == pytest.approx(
        0.6411000000000001, abs=1e-9
    )


def test_sort_results_snapshot() -> None:
    results = [
        _result(site="gamma", book_id="g1", title="斗破苍穹外传", word_count="50万字"),
        _result(site="alpha", book_id="a1", title="斗破苍穹", word_count="10万字"),
        _result(site="beta", book_id="b1", title="斗破苍穹", word_count="300万字"),
        _result(site="alpha", book_id="a2", title="斗破苍穹", word_count="20万字"),
    ]
    sort_results(results, keyword="斗破苍穹", now=NOW)
    assert [r["book_id"] for r in results] == ["b1", "a2", "a1", "g1"]


# --------------------------------------------------------------------------- #
# parse_update_date equivalence with the original try-everything order
# --------------------------------------------------------------------------- #
_REFERENCE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)

_REFERENCE_NULL_TOKENS = frozenset({"", "-", "--", "—", "未知", "暂无", "none", "n/a"})
_REFERENCE_AGO = re.compile(
    r"(\d+)\s*(秒钟|秒|分钟|分|个小时|小时|天|日|周|星期|个月|月|年)\s*前"
)


def _reference_parse_update_date(value: object, now: datetime) -> datetime | None:
    text = str(value if value is not None else "").strip()
    if text.casefold() in _REFERENCE_NULL_TOKENS:
        return None
    for fmt in _REFERENCE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    lowered = text.casefold()
    if lowered in {"刚刚", "刚才", "just now", "justnow"}:
        return now
    if lowered in {"今天", "today"}:
        return now
    if lowered in {"昨天", "yesterday"}:
        return now - timedelta(days=1)
    if lowered == "前天":
        return now - timedelta(days=2)

    match = _REFERENCE_AGO.search(text)
    if match is not None:
        amount = int(match.group(1))
        unit = match.group(2)
        deltas = {
            "秒": timedelta(seconds=amount),
            "秒钟": timedelta(seconds=amount),
            "分": timedelta(minutes=amount),
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "个小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
            "日": timedelta(days=amount),
            "周": timedelta(weeks=amount),
            "星期": timedelta(weeks=amount),
            "月": timedelta(days=30 * amount),
            "个月": timedelta(days=30 * amount),
            "年": timedelta(days=365 * amount),
        }
        return now - deltas[unit]
    return None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-",
        "未知",
        "刚刚",
        "今天",
        "昨天",
        "前天",
        "3天前",
        "2个月前",
        "1年前",
        "2024-05-01",
        "2024-5-1",
        "2024-05-01 10:00",
        "2024-05-01 10:00:00",
        "2024-05-01T10:00:00",
        "2024-05-01 12:00:00+08:00",
        "20240501",
        "999-01-01",
        "02024-01-01",
        "2024/5/1",
        "2024/05/01",
        "2024/5/1 3:4",
        "2024/5/1 3:4:5",
        "2024/13/01",
        "2024.5.1",
        "2024.05.01",
        "2024.13.1",
        "2024年5月1日",
        "2024年5月1日 3:4",
        "2024年5月1日 3:4:5",
        "2024年13月1日",
        "3天前后",
        "2024-05-01前",
        "not a date",
        " 2024-05-01 ",
        "２０２４年５月１日",
    ],
)
def test_parse_update_date_matches_reference(value: str) -> None:
    assert parse_update_date(value, now=NOW) == _reference_parse_update_date(value, NOW)
