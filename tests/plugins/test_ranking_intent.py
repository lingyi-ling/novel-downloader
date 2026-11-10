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
    filter_by_relevance,
    normalize_text,
    rank_score,
    relevance_of,
    sort_results,
    sort_results_with_intent,
)
from novel_downloader.plugins.search_cache import build_key
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


# --------------------------------------------------------------------------- #
# two-level ordering under a decided intent
#
# ``rank_score`` is a weighted sum (relevance 55% + metadata 45%), so a row
# whose *title* merely equals the searched author's name can outscore that
# author's own rows when those rows carry no cover / word count / update date.
# Ordering therefore ranks by the primary field's match tier first and only
# then by ``rank_score`` -- but only for a decided intent on ``"quality"``.
# --------------------------------------------------------------------------- #
def _rich_collision() -> SearchResult:
    """A title-collision row with every optional metadata field filled in."""
    return _result(
        site="a",
        book_id="collision",
        title="天蚕土豆",
        author="路人甲",
        word_count="300万字",
        update_date="2024-06-14",
        cover_url="https://example.com/c.jpg",
        latest_chapter="第100章",
        priority=5,
    )


def _sparse_author_work() -> SearchResult:
    """The searched author's own book, with every optional field missing."""
    return _result(
        site="b",
        book_id="author",
        title="斗破苍穹",
        author="天蚕土豆",
        word_count="",
        update_date="",
        cover_url="",
        latest_chapter="",
        priority=1000,
    )


def test_author_intent_sparse_author_work_beats_rich_title_collision() -> None:
    collision = _rich_collision()
    author_book = _sparse_author_work()
    keyword = "天蚕土豆"

    # The weighted sum alone still prefers the metadata-rich collision ...
    assert rank_score(collision, keyword, intent="author", now=NOW) > rank_score(
        author_book, keyword, intent="author", now=NOW
    )

    # ... yet the primary-field tier puts the sparse author hit first.
    results = [collision, author_book]
    sort_results(results, keyword=keyword, intent="author", now=NOW)

    assert [r["book_id"] for r in results] == ["author", "collision"]


def test_title_intent_sparse_title_beats_rich_author_hit() -> None:
    keyword = "重生之都市修仙"
    title_hit = _result(
        site="b",
        book_id="title",
        title=keyword,
        author="某作者",
        word_count="",
        update_date="",
        cover_url="",
        latest_chapter="",
        priority=1000,
    )
    derivative = _result(
        site="c",
        book_id="deriv",
        title=f"{keyword}外传",
        author="别人",
        word_count="",
        update_date="",
        priority=1000,
    )
    author_hit = _result(
        site="a",
        book_id="author",
        title="无关书名",
        author=keyword,
        word_count="300万字",
        update_date="2024-06-14",
        cover_url="https://example.com/c.jpg",
        latest_chapter="第100章",
        priority=5,
    )

    # Mirror of the author case: the rich row wins the weighted sum ...
    assert rank_score(author_hit, keyword, intent="title", now=NOW) > rank_score(
        title_hit, keyword, intent="title", now=NOW
    )

    # ... but tier order (exact > prefix > no hit) decides the sort.
    results = [author_hit, derivative, title_hit]
    sort_results(results, keyword=keyword, intent="title", now=NOW)

    assert [r["book_id"] for r in results] == ["title", "deriv", "author"]


def test_either_intent_keeps_metadata_weighted_order() -> None:
    # ``"either"`` must stay byte-for-byte historical: the explicit path and
    # the default path both keep preferring the metadata-rich collision.
    collision = _rich_collision()
    author_book = _sparse_author_work()

    explicit = [collision, author_book]
    sort_results(explicit, keyword="天蚕土豆", intent="either", now=NOW)
    assert [r["book_id"] for r in explicit] == ["collision", "author"]

    default = [collision, author_book]
    sort_results(default, keyword="天蚕土豆", now=NOW)
    assert [r["book_id"] for r in default] == [r["book_id"] for r in explicit]


def test_same_primary_tier_still_orders_by_quality_score() -> None:
    # Both rows are exact author matches, so tier cannot separate them and the
    # existing score ordering (rich/fresh first) must still apply.
    rich = _result(
        site="a",
        book_id="rich",
        title="斗破苍穹",
        author="天蚕土豆",
        word_count="300万字",
        update_date="2024-06-14",
        cover_url="https://example.com/c.jpg",
        latest_chapter="第100章",
        priority=5,
    )
    sparse = _result(
        site="b",
        book_id="sparse",
        title="武动乾坤",
        author="天蚕土豆",
        word_count="",
        update_date="",
        cover_url="",
        latest_chapter="",
        priority=1000,
    )

    results = [sparse, rich]
    sort_results(results, keyword="天蚕土豆", intent="author", now=NOW)

    assert [r["book_id"] for r in results] == ["rich", "sparse"]


def test_primary_match_tiers_sort_exact_prefix_contains_partial_none() -> None:
    keyword = "土豆"
    fields = {
        "exact": "土豆",
        "prefix": "土豆丝",
        "contains": "天蚕土豆",
        "partial": "土",
        "none": "某作者",
    }
    results = [
        _result(book_id=name, title="无关书名", author=author)
        for name, author in fields.items()
    ]

    sort_results(results, keyword=keyword, intent="author", now=NOW)

    assert [r["book_id"] for r in results] == [
        "exact",
        "prefix",
        "contains",
        "partial",
        "none",
    ]


def test_explicit_update_sort_ignores_primary_match_tier() -> None:
    # An explicit "update" ordering must not be reshuffled by intent/tier.
    fresh_collision = _rich_collision()
    old_exact = _result(
        site="b",
        book_id="old-exact",
        title="斗破苍穹",
        author="天蚕土豆",
        update_date="2020-01-01",
    )

    results = [old_exact, fresh_collision]
    sort_results(results, by="update", keyword="天蚕土豆", intent="author", now=NOW)

    assert [r["book_id"] for r in results] == ["collision", "old-exact"]


# --------------------------------------------------------------------------- #
# filter_by_relevance
# --------------------------------------------------------------------------- #
def test_filter_by_relevance_keeps_rows_at_or_above_the_floor() -> None:
    exact = _result(book_id="exact", title="斗破苍穹", author="天蚕土豆")
    derivative = _result(book_id="deriv", title="斗破苍穹外传", author="别人")
    unrelated = _result(book_id="none", title="三体", author="刘慈欣")

    floor = relevance_of(derivative, "斗破苍穹", intent="either")

    kept = filter_by_relevance(
        [unrelated, derivative, exact],
        "斗破苍穹",
        intent="either",
        min_relevance=floor,
    )

    # The floor is inclusive: a row whose relevance equals it survives.
    assert kept == [derivative, exact]


def test_filter_by_relevance_drops_rows_just_below_the_floor() -> None:
    exact = _result(book_id="exact", title="斗破苍穹", author="天蚕土豆")
    derivative = _result(book_id="deriv", title="斗破苍穹外传", author="别人")

    floor = relevance_of(derivative, "斗破苍穹", intent="either") + 1e-6

    kept = filter_by_relevance(
        [derivative, exact], "斗破苍穹", intent="either", min_relevance=floor
    )

    assert kept == [exact]


def test_filter_by_relevance_never_returns_an_empty_page() -> None:
    # Nothing clears the floor -> fall back to "any positive relevance".
    exact = _result(book_id="exact", title="斗破苍穹", author="天蚕土豆")
    unrelated = _result(book_id="none", title="三体", author="刘慈欣")

    kept = filter_by_relevance(
        [unrelated, exact], "斗破苍穹", intent="either", min_relevance=1.5
    )

    assert kept == [exact]


def test_filter_by_relevance_keeps_everything_when_nothing_matches() -> None:
    first = _result(book_id="a", title="三体", author="刘慈欣")
    second = _result(book_id="b", title="活着", author="余华")

    kept = filter_by_relevance(
        [first, second], "完全不存在的书名", intent="either", min_relevance=0.45
    )

    assert kept == [first, second]


def test_filter_by_relevance_does_not_mutate_the_input() -> None:
    results = [
        _result(book_id="exact", title="斗破苍穹", author="天蚕土豆"),
        _result(book_id="none", title="三体", author="刘慈欣"),
    ]
    snapshot = [dict(item) for item in results]

    kept = filter_by_relevance(results, "斗破苍穹", intent="either", min_relevance=0.45)

    assert kept == [results[0]]
    assert results == snapshot


# --------------------------------------------------------------------------- #
# full-width / half-width folding
# --------------------------------------------------------------------------- #
def test_normalize_text_folds_full_width_forms() -> None:
    # Japanese/light-novel sites routinely serve Latin titles and pen names in
    # full-width forms.  They must compare equal to the half-width spelling a
    # user types.
    assert normalize_text("ＡＢＣ１２３") == "abc123"
    assert normalize_text("ＳＡＯ") == normalize_text("sao")
    assert normalize_text("Ｒｅｋｉ") == "reki"
    # Decomposed accents are composed before the non-word characters go.
    assert normalize_text("Cafe\u0301") == "café"


def test_full_width_query_matches_half_width_title_exactly() -> None:
    item = _result(title="ABC123", author="Reki")

    assert relevance_of(item, "ＡＢＣ１２３") == pytest.approx(1.0)
    assert relevance_of(item, "ＡＢＣ１２３") == relevance_of(item, "ABC123")


def test_full_width_author_query_is_detected_as_author() -> None:
    results = [
        _result(title="Sword Art Online", author="Ｒｅｋｉ"),
        _result(title="Accel World", author="Reki"),
    ]

    assert detect_query_intent("reki", results) == "author"
    assert relevance_of(results[0], "reki", intent="author") == relevance_of(
        results[0], "Ｒｅｋｉ", intent="author"
    )


def test_sort_results_with_intent_handles_full_width_author() -> None:
    title_collision = _result(book_id="title", title="Reki", author="路人")
    author_book = _result(book_id="author", title="Sword Art Online", author="Ｒｅｋｉ")

    results = [title_collision, author_book]
    sort_results_with_intent(results, "reki", now=NOW)

    assert [r["book_id"] for r in results] == ["author", "title"]


def test_build_key_folds_full_width_keyword() -> None:
    # The cache identity must use the same normalisation as ranking, otherwise
    # the two spellings would fetch twice.
    assert build_key("ＡＢＣ", None) == build_key("abc", None)


def test_build_key_normalizes_site_keys() -> None:
    # Site keys arrive from CLI flags, settings files and UIs, so case and
    # stray whitespace must not create separate cache entries.  This mirrors
    # the registry's own ``strip().lower()`` normalization.
    canonical = build_key("k", ["qidian"])
    assert build_key("k", ["Qidian"]) == canonical
    assert build_key("k", [" Qidian "]) == canonical
    assert build_key("k", ["qidian", "QIDIAN "]) == canonical
    assert canonical.sites == ("qidian",)


def test_build_key_keeps_distinct_normalized_sites_apart() -> None:
    # Normalization must not collapse genuinely different sites.
    assert build_key("k", ["qidian"]) != build_key("k", ["fanqie"])
    assert build_key("k", ["qidian", "fanqie"]) == build_key("k", ["fanqie", "Qidian"])
