#!/usr/bin/env python3
"""Tests for query-intent detection and intent-aware ranking."""

from __future__ import annotations

from datetime import datetime

import pytest

from novel_downloader.plugins.ranking import (
    INTENT_LONG_MIN,
    INTENT_SHORT_MAX,
    INTENT_WEIGHTS,
    detect_query_intent,
    rank_score,
    relevance_of,
    sort_results,
    sort_results_with_intent,
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
        "update_date": "2024-06-01",
        "word_count": "100万字",
        "priority": 100,
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# weights table
# --------------------------------------------------------------------------- #
def test_intent_weights_short_tier_favours_author() -> None:
    title_weight, author_weight = INTENT_WEIGHTS["short"]
    assert author_weight > title_weight


def test_intent_weights_medium_tier_is_balanced() -> None:
    assert INTENT_WEIGHTS["medium"] == (0.5, 0.5)


def test_intent_weights_long_tier_favours_title() -> None:
    title_weight, author_weight = INTENT_WEIGHTS["long"]
    assert title_weight > author_weight


def test_intent_thresholds_are_sane() -> None:
    assert 0 < INTENT_SHORT_MAX < INTENT_LONG_MIN


# --------------------------------------------------------------------------- #
# detect_query_intent
# --------------------------------------------------------------------------- #
def test_detect_author_from_full_pen_name() -> None:
    results = [
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="武动乾坤", author="天蚕土豆"),
        _result(title="大主宰", author="天蚕土豆"),
        _result(title="其他小说", author="别人"),
    ]
    assert detect_query_intent("天蚕土豆", results) == "author"


def test_detect_author_when_title_collides_with_pen_name() -> None:
    # 书名恰好等于作者名时，也应判定为作者搜索：作者命中远多于书名命中。
    results = [
        _result(title="天蚕土豆", author="路人甲"),
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="武动乾坤", author="天蚕土豆"),
    ]
    assert detect_query_intent("天蚕土豆", results) == "author"


def test_detect_author_for_short_partial_pen_name() -> None:
    # "土豆" 是 "天蚕土豆" 的一部分，属于典型的短作者名查询。
    results = [
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="武动乾坤", author="天蚕土豆"),
        _result(title="土豆炖牛肉", author="某作者"),
    ]
    assert detect_query_intent("土豆", results) == "author"


def test_detect_title_for_short_title() -> None:
    results = [
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="斗破苍穹外传", author="别人"),
        _result(title="斗破苍穹之崛起", author="别人2"),
    ]
    assert detect_query_intent("斗破苍穹", results) == "title"


def test_detect_title_for_long_title() -> None:
    results = [
        _result(title="重生之都市修仙", author="十里剑神"),
        _result(title="重生之都市修仙传", author="路人"),
    ]
    assert detect_query_intent("重生之都市修仙", results) == "title"


def test_detect_returns_either_for_empty_keyword() -> None:
    results = [_result(title="斗破苍穹", author="天蚕土豆")]
    assert detect_query_intent("", results) == "either"


def test_detect_returns_either_without_results() -> None:
    assert detect_query_intent("天蚕土豆", []) == "either"


def test_detect_returns_either_when_nothing_matches() -> None:
    results = [_result(title="三体", author="刘慈欣")]
    assert detect_query_intent("量子力学", results) == "either"


def test_detect_returns_either_when_evidence_is_mixed() -> None:
    # 一条书名命中、一条作者命中，且两侧强度相同 -> 不偏向任何一边。
    # （选 6 字关键词，落在均衡档，排除长度权重本身的偏向。）
    keyword = "测试用的书名"
    results = [
        _result(title=keyword, author="甲"),
        _result(title="乙", author=keyword),
    ]
    assert detect_query_intent(keyword, results) == "either"


def test_detect_does_not_mutate_results() -> None:
    results = [
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="武动乾坤", author="天蚕土豆"),
    ]
    snapshot = [dict(item) for item in results]
    detect_query_intent("天蚕土豆", results)
    assert [dict(item) for item in results] == snapshot


# --------------------------------------------------------------------------- #
# relevance_of / rank_score intent handling
# --------------------------------------------------------------------------- #
def test_relevance_of_default_matches_legacy_formula() -> None:
    keyword = "斗破苍穹"
    item = _result(title="斗破苍穹外传", author="别人")
    # Historical default path is unchanged; explicit "either" is identical.
    assert relevance_of(item, keyword) == relevance_of(item, keyword, intent="either")


def test_relevance_of_author_intent_prefers_author_hit() -> None:
    author_hit = _result(title="斗破苍穹", author="天蚕土豆")
    title_hit = _result(title="土豆", author="某作者")

    assert relevance_of(author_hit, "土豆", intent="author") > relevance_of(
        title_hit, "土豆", intent="author"
    )


def test_relevance_of_title_intent_prefers_title_hit() -> None:
    author_hit = _result(title="无关书名", author="斗破苍穹")
    title_hit = _result(title="斗破苍穹", author="别人")

    assert relevance_of(title_hit, "斗破苍穹", intent="title") > relevance_of(
        author_hit, "斗破苍穹", intent="title"
    )


def test_relevance_of_unknown_intent_falls_back_to_either() -> None:
    item = _result(title="斗破苍穹", author="天蚕土豆")
    assert relevance_of(item, "斗破苍穹", intent="nonsense") == relevance_of(
        item, "斗破苍穹"
    )


def test_rank_score_default_is_unchanged_by_new_parameter() -> None:
    strong = _result(
        title="斗破苍穹",
        author="天蚕土豆",
        word_count="300万字",
        update_date="2024-06-15",
        cover_url="https://example.com/c.jpg",
        latest_chapter="第100章",
        priority=5,
    )
    assert rank_score(strong, "斗破苍穹", now=NOW) == pytest.approx(
        0.9887954554582784, abs=1e-9
    )
    assert rank_score(strong, "斗破苍穹", intent="either", now=NOW) == rank_score(
        strong, "斗破苍穹", now=NOW
    )


def test_rank_score_author_intent_demotes_title_only_match() -> None:
    author_hit = _result(title="武动乾坤", author="天蚕土豆")
    title_hit = _result(title="天蚕土豆", author="路人甲")

    assert rank_score(author_hit, "天蚕土豆", intent="author", now=NOW) > rank_score(
        title_hit, "天蚕土豆", intent="author", now=NOW
    )


# --------------------------------------------------------------------------- #
# sort_results_with_intent
# --------------------------------------------------------------------------- #
def test_sort_results_with_intent_fixes_author_search() -> None:
    """原问题：搜作者名时，标题恰好含该词的书排到了作者本人前面。"""
    title_collision = _result(book_id="title", title="土豆", author="某作者")
    author_book = _result(book_id="author", title="斗破苍穹", author="天蚕土豆")

    legacy = [title_collision, author_book]
    sort_results(legacy, keyword="土豆", now=NOW)
    assert [r["book_id"] for r in legacy] == ["title", "author"]

    fixed = [dict(title_collision), dict(author_book)]  # type: ignore[misc]
    sort_results_with_intent(fixed, "土豆", now=NOW)
    assert [r["book_id"] for r in fixed] == ["author", "title"]


def test_sort_results_with_intent_keeps_title_search_order() -> None:
    exact = _result(book_id="exact", title="斗破苍穹", author="天蚕土豆")
    derivative = _result(book_id="deriv", title="斗破苍穹外传", author="别人")

    results = [derivative, exact]
    sort_results_with_intent(results, "斗破苍穹", now=NOW)

    assert [r["book_id"] for r in results] == ["exact", "deriv"]


def test_sort_results_with_intent_min_relevance_filters() -> None:
    exact = _result(book_id="exact", title="斗破苍穹", author="天蚕土豆")
    derivative = _result(book_id="deriv", title="斗破苍穹外传", author="别人")
    unrelated = _result(book_id="none", title="三体", author="刘慈欣")

    results = [unrelated, derivative, exact]
    sort_results_with_intent(results, "斗破苍穹", min_relevance=0.9, now=NOW)

    assert [r["book_id"] for r in results] == ["exact"]


def test_sort_results_with_intent_never_empties_results() -> None:
    # 阈值高到没有任何结果达标 -> 退化为 "relevance > 0"。
    exact = _result(book_id="exact", title="斗破苍穹", author="天蚕土豆")
    unrelated = _result(book_id="none", title="三体", author="刘慈欣")

    results = [unrelated, exact]
    sort_results_with_intent(results, "斗破苍穹", min_relevance=1.5, now=NOW)

    assert [r["book_id"] for r in results] == ["exact"]


def test_sort_results_with_intent_keeps_input_when_nothing_matches() -> None:
    # 连 "relevance > 0" 都为空时保留原始结果集，绝不返回空页。
    first = _result(book_id="a", title="三体", author="刘慈欣")
    second = _result(book_id="b", title="活着", author="余华")

    results = [first, second]
    sort_results_with_intent(results, "完全不存在的书名", min_relevance=0.5, now=NOW)

    assert {r["book_id"] for r in results} == {"a", "b"}


def test_sort_results_with_intent_honours_other_sort_keys() -> None:
    results = [
        _result(book_id="short", title="斗破苍穹", word_count="1万字"),
        _result(book_id="long", title="斗破苍穹外传", word_count="300万字"),
    ]
    sort_results_with_intent(results, "斗破苍穹", by="words", now=NOW)

    assert [r["book_id"] for r in results] == ["long", "short"]


def test_sort_results_accepts_intent_keyword() -> None:
    # sort_results 仍是可选参数，默认值不改变行为。
    title_collision = _result(book_id="title", title="土豆", author="某作者")
    author_book = _result(book_id="author", title="斗破苍穹", author="天蚕土豆")

    results = [dict(title_collision), dict(author_book)]  # type: ignore[misc]
    sort_results(results, keyword="土豆", intent="author", now=NOW)

    assert [r["book_id"] for r in results] == ["author", "title"]
