#!/usr/bin/env python3
"""Tests for query-intent detection and intent-aware ranking."""

from __future__ import annotations

import random
from datetime import datetime

import pytest

from novel_downloader.plugins.ranking import (
    INTENT_LONG_MIN,
    INTENT_SHORT_MAX,
    INTENT_WEIGHTS,
    detect_query_intent,
    filter_by_relevance,
    match_tier_of,
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


def test_detect_intent_is_independent_of_result_arrival_order() -> None:
    # The verdict compares two accumulated sums against a threshold.  A plain
    # float accumulation loop is order-sensitive in its last bits, so the same
    # rows arriving in a different order (concurrent sites completing at
    # different times) must still yield the identical verdict.
    results = [
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="武动乾坤", author="天蚕土豆"),
        _result(title="大主宰", author="天蚕土豆"),
        _result(title="斗破苍穹外传", author="别人"),
        _result(title="重生之都市修仙", author="十里剑神"),
        _result(title="三体", author="刘慈欣"),
    ]
    baseline = detect_query_intent("天蚕土豆", results)
    assert baseline == "author"

    rng = random.Random(20240615)
    for _ in range(25):
        shuffled = list(results)
        rng.shuffle(shuffled)
        assert detect_query_intent("天蚕土豆", shuffled) == baseline


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


# --------------------------------------------------------------------------- #
# multi-token ("书名 + 作者") queries
#
# "斗破苍穹 天蚕土豆" does not occur verbatim in either field, so the historical
# whole-string relevance scored it 0.275 -- below the 0.45 floor -- and the
# genuinely matching book was dropped.  Two-to-four tokens are now scored
# separately; every other token count keeps the old path exactly.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("separator", [" ", "\u3000", "·", "-", ",", "，"])
def test_query_tokens_split_on_every_supported_separator(separator: str) -> None:
    from novel_downloader.plugins.ranking import _query_tokens

    assert _query_tokens(f"斗破苍穹{separator}天蚕土豆") == ("斗破苍穹", "天蚕土豆")


def test_query_tokens_collapse_separator_runs_and_drop_empties() -> None:
    from novel_downloader.plugins.ranking import _query_tokens

    assert _query_tokens("  斗破苍穹  -，  天蚕土豆  ") == ("斗破苍穹", "天蚕土豆")
    assert _query_tokens("斗破苍穹 天蚕土豆 外传") == ("斗破苍穹", "天蚕土豆", "外传")
    assert _query_tokens("斗破苍穹 天蚕土豆 外传 全本") == (
        "斗破苍穹",
        "天蚕土豆",
        "外传",
        "全本",
    )


def test_query_tokens_disabled_outside_the_two_to_four_window() -> None:
    from novel_downloader.plugins.ranking import _query_tokens

    # One token (including a punctuated title) and five or more tokens keep
    # the historical whole-keyword behaviour.
    assert _query_tokens("") == ()
    assert _query_tokens("斗破苍穹") == ()
    assert _query_tokens("  斗破苍穹  ") == ()
    assert _query_tokens("斗破苍穹（精校版）") == ()
    assert _query_tokens("斗破苍穹 天蚕土豆 外传 全本 精校") == ()


@pytest.mark.parametrize("keyword", ["斗破苍穹 天蚕土豆", "天蚕土豆 斗破苍穹"])
def test_multi_token_query_matches_title_and_author(keyword: str) -> None:
    book = _result(title="斗破苍穹", author="天蚕土豆")

    # The old whole-string score for this book was 0.275 (below the floor).
    assert relevance_of(book, "斗破苍穹天蚕土豆") == pytest.approx(0.275)
    # Either token order now matches both fields of the book.
    assert relevance_of(book, keyword) == pytest.approx(1.0)


def test_multi_token_query_survives_the_relevance_floor() -> None:
    book = _result(book_id="book", title="斗破苍穹", author="天蚕土豆")
    derivative = _result(book_id="deriv", title="斗破苍穹之雷霆震碎", author="别人")
    unrelated = _result(book_id="none", title="三体", author="刘慈欣")

    kept = filter_by_relevance(
        [unrelated, derivative, book],
        "斗破苍穹 天蚕土豆",
        min_relevance=0.45,
    )

    # The book and its genuine derivative survive; the unrelated row does not.
    assert [row["book_id"] for row in kept] == ["deriv", "book"]


def test_multi_token_query_does_not_boost_unrelated_books() -> None:
    keyword = "斗破苍穹 天蚕土豆"
    for title, author in (
        ("三体", "刘慈欣"),
        ("活着", "余华"),
        ("斗罗大陆", "唐家三少"),
        ("苍穹榜", "土豆"),
    ):
        score = relevance_of(_result(title=title, author=author), keyword)
        assert score < 0.45, (title, author, score)


def test_punctuated_title_query_is_not_weakened_by_token_splitting() -> None:
    # A real title may itself contain a listed separator.  The whole-keyword
    # score stays in the comparison, so splitting never lowers relevance.
    for title in ("Re:Zero-从零开始的异世界生活", "你好，旧时光"):
        book = _result(title=title, author="某作者")
        assert relevance_of(book, title) == pytest.approx(1.0)


def test_more_than_four_tokens_keeps_the_whole_keyword_behaviour() -> None:
    keyword = "斗破苍穹 天蚕土豆 外传 全本 精校"
    whole = "斗破苍穹天蚕土豆外传全本精校"
    book = _result(title="斗破苍穹", author="天蚕土豆")

    # Five tokens disable multi-token matching: the score equals the
    # historical whole-string computation, which stays below the floor.
    assert relevance_of(book, keyword) == relevance_of(book, whole)
    assert relevance_of(book, keyword) < 0.45


def test_query_tokens_dedupe_repeated_tokens() -> None:
    from novel_downloader.plugins.ranking import _query_tokens

    # Duplicates carry no extra information and must not consume the 2-4 token
    # window: one repeated token is still a single-token query, while a
    # title+author pair repeated past four raw tokens must stay multi-token.
    assert _query_tokens("三体 三体") == ()
    # A punctuation-only fragment survives splitting but carries no
    # evidence, so it must not count either.
    assert _query_tokens("斗破苍穹 《》 天蚕土豆") == ("斗破苍穹", "天蚕土豆")
    assert _query_tokens("斗破苍穹 天蚕土豆 斗破苍穹") == ("斗破苍穹", "天蚕土豆")
    assert _query_tokens("斗破苍穹 天蚕土豆 天蚕土豆 天蚕土豆 天蚕土豆") == (
        "斗破苍穹",
        "天蚕土豆",
    )


def test_repeated_token_query_still_matches_title_and_author() -> None:
    # Five raw tokens used to disable multi-token matching, dropping this book
    # from 1.0 relevance to 0.36 -- below the default 0.45 floor.
    book = _result(title="斗破苍穹", author="天蚕土豆")
    keyword = "斗破苍穹 天蚕土豆 天蚕土豆 天蚕土豆 天蚕土豆"

    assert relevance_of(book, keyword) == pytest.approx(1.0)
    assert filter_by_relevance([book], keyword, min_relevance=0.45) == [book]


def test_query_tokens_fold_full_width_separators() -> None:
    from novel_downloader.plugins.ranking import _query_tokens

    # The module folds full-width forms everywhere (titles, authors, dates,
    # word counts), but tokenisation used to split only the full-width comma,
    # so a full-width hyphen left "title－author" as one unusable token.
    assert _query_tokens("斗破苍穹－天蚕土豆") == ("斗破苍穹", "天蚕土豆")
    assert _query_tokens("ＡＢＣ－ＤＥＦ") == ("ABC", "DEF")


@pytest.mark.parametrize("keyword", ["斗破苍穹－天蚕土豆", "天蚕土豆－斗破苍穹"])
def test_full_width_hyphen_query_matches_title_and_author(keyword: str) -> None:
    book = _result(title="斗破苍穹", author="天蚕土豆")

    assert relevance_of(book, keyword) == pytest.approx(1.0)


def test_relevance_of_unhashable_intent_falls_back_to_either() -> None:
    # ``intent`` may come straight from a JSON query parameter; an unknown
    # value must never break a search, and an unhashable one used to raise.
    item = _result(title="斗破苍穹", author="天蚕土豆")
    assert relevance_of(item, "斗破苍穹", intent=["author"]) == relevance_of(  # type: ignore[arg-type]
        item, "斗破苍穹"
    )


def test_detect_mixed_title_and_author_tokens_returns_either() -> None:
    results = [
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="武动乾坤", author="天蚕土豆"),
    ]

    # One token points at the title, the other at the pen name: mixed.
    assert detect_query_intent("斗破苍穹 天蚕土豆", results) == "either"
    assert detect_query_intent("天蚕土豆 斗破苍穹", results) == "either"


def test_detect_multi_token_title_only_query_returns_title() -> None:
    results = [
        _result(title="斗破苍穹外传", author=""),
        _result(title="斗破苍穹之雷霆震碎", author=""),
    ]

    assert detect_query_intent("斗破苍穹 外传", results) == "title"


def test_detect_single_token_behaviour_is_unchanged() -> None:
    results = [
        _result(title="斗破苍穹", author="天蚕土豆"),
        _result(title="斗破苍穹外传", author="别人"),
    ]

    # No tokenisation for a single token: same verdict as before.
    assert detect_query_intent("斗破苍穹", results) == "title"


# --------------------------------------------------------------------------- #
# match_tier_of: the 0..4 telemetry reported on final search rows
# --------------------------------------------------------------------------- #
def test_match_tier_of_reports_the_primary_field_under_an_intent() -> None:
    author_book = _result(title="斗破苍穹", author="天蚕土豆")
    title_collision = _result(title="天蚕土豆", author="路人甲")

    # Read as an author query, only the author field can produce the hit.
    assert match_tier_of(author_book, "天蚕土豆", intent="author") == 4
    assert match_tier_of(title_collision, "天蚕土豆", intent="author") == 0
    # Mirror image: the same text read as a title.
    assert match_tier_of(title_collision, "天蚕土豆", intent="title") == 4
    assert match_tier_of(author_book, "天蚕土豆", intent="title") == 0


def test_match_tier_of_either_uses_the_better_field() -> None:
    row = _result(title="三体", author="刘慈欣")

    assert match_tier_of(row, "三体") == 4
    assert match_tier_of(row, "刘慈欣") == 4
    assert match_tier_of(row, "斗破苍穹") == 0


def test_match_tier_of_distinguishes_every_match_kind() -> None:
    assert match_tier_of(_result(title="斗破苍穹", author=""), "斗破苍穹") == 4
    assert match_tier_of(_result(title="斗破苍穹外传", author=""), "斗破苍穹") == 3
    assert match_tier_of(_result(title="重生之斗破苍穹", author=""), "斗破苍穹") == 2
    # Only some characters overlap: a partial match, not containment.
    assert match_tier_of(_result(title="斗破", author=""), "斗破苍穹") == 1
    assert match_tier_of(_result(title="三体", author=""), "斗破苍穹") == 0
    # An empty keyword matches nothing at all.
    assert match_tier_of(_result(title="三体", author="刘慈欣"), "") == 0


def test_match_tier_of_is_multi_token_aware() -> None:
    book = _result(title="斗破苍穹", author="天蚕土豆")
    derivative = _result(title="斗破苍穹之雷霆震碎", author="别人")

    assert match_tier_of(book, "斗破苍穹 天蚕土豆") == 4
    assert match_tier_of(book, "天蚕土豆 斗破苍穹") == 4
    assert match_tier_of(derivative, "斗破苍穹 天蚕土豆") == 3
    # Five tokens keep the historical whole-keyword path: the long string is
    # not contained, and only its overlapping characters count (partial).
    assert match_tier_of(book, "斗破苍穹 天蚕土豆 外传 全本 精校") == 1
