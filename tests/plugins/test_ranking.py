#!/usr/bin/env python3
"""Tests for the search-result quality ranking helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

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
