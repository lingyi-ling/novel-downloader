#!/usr/bin/env python3
"""Tests for the same-book grouping of aggregated search results."""

from __future__ import annotations

from novel_downloader.plugins.grouping import ResultGroup, group_results
from novel_downloader.schemas import SearchResult


def _result(**overrides: object) -> SearchResult:
    """Build a :class:`SearchResult` with neutral defaults.

    Update date / word count / priority defaults are equal for every
    result, so the recency / site signals stay constant and rank order
    only changes through the fields a test overrides.
    """
    base: dict[str, object] = {
        "site": "alpha",
        "book_id": "1",
        "book_url": "https://example.com/1",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 100,
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# basic shape
# --------------------------------------------------------------------------- #
def test_single_result_yields_single_group() -> None:
    only = _result(book_id="42")
    groups = group_results([only])

    assert len(groups) == 1
    group = groups[0]
    assert isinstance(group, ResultGroup)
    assert group.primary is only
    assert group.sources == [only]


def test_empty_input_yields_no_groups() -> None:
    assert group_results([]) == []
    assert group_results(()) == []


# --------------------------------------------------------------------------- #
# merging across sites
# --------------------------------------------------------------------------- #
def test_same_title_author_merges_across_sites() -> None:
    small = _result(site="alpha", book_id="a", word_count="10万字")
    big = _result(site="beta", book_id="b", word_count="300万字")
    mid = _result(site="gamma", book_id="c", word_count="100万字")

    groups = group_results([small, big, mid])

    assert len(groups) == 1
    group = groups[0]
    # Highest word count wins, no matter the input order.
    assert group.primary["book_id"] == "b"
    assert [r["book_id"] for r in group.sources] == ["b", "c", "a"]
    assert any(r is group.primary for r in group.sources)


def test_primary_tie_prefers_first_occurrence() -> None:
    first = _result(site="alpha", book_id="1")
    second = _result(site="beta", book_id="2")

    groups = group_results([first, second])

    assert len(groups) == 1
    assert groups[0].primary is first
    assert groups[0].sources == [first, second]


# --------------------------------------------------------------------------- #
# conservative author handling
# --------------------------------------------------------------------------- #
def test_same_title_different_authors_split_into_two_groups() -> None:
    a = _result(book_id="1", author="刘慈欣")
    b = _result(book_id="2", author="天蚕土豆")

    groups = group_results([a, b])

    assert len(groups) == 2
    assert [g.primary["book_id"] for g in groups] == ["1", "2"]
    assert all(len(g.sources) == 1 for g in groups)


def test_author_split_keeps_same_author_together() -> None:
    a1 = _result(book_id="1", author="甲作者")
    b = _result(book_id="2", author="乙作者")
    a2 = _result(book_id="3", author="甲作者")

    groups = group_results([a1, b, a2])

    assert len(groups) == 2
    assert [r["book_id"] for r in groups[0].sources] == ["1", "3"]
    assert [r["book_id"] for r in groups[1].sources] == ["2"]


def test_empty_author_merges_with_known_author() -> None:
    known = _result(book_id="1", author="刘慈欣")
    unknown = _result(book_id="2", author="")

    groups = group_results([known, unknown])
    groups_reversed = group_results([unknown, known])

    assert len(groups) == 1
    assert [r["book_id"] for r in groups[0].sources] == ["1", "2"]
    assert len(groups_reversed) == 1
    assert [r["book_id"] for r in groups_reversed[0].sources] == ["2", "1"]


def test_empty_author_joins_first_matching_group() -> None:
    x1 = _result(book_id="1", author="甲")
    y = _result(book_id="2", author="乙")
    x2 = _result(book_id="3", author="")

    groups = group_results([x1, y, x2])

    assert len(groups) == 2
    # The unknown-author entry follows the first matching group ("甲").
    assert [r["book_id"] for r in groups[0].sources] == ["1", "3"]
    assert [r["book_id"] for r in groups[1].sources] == ["2"]


# --------------------------------------------------------------------------- #
# title normalization
# --------------------------------------------------------------------------- #
def test_title_punctuation_and_case_differences_merge() -> None:
    plain = _result(book_id="1", title="三体", author="刘慈欣")
    punct = _result(book_id="2", title="三体。", author="刘慈欣")
    upper = _result(book_id="3", title="SAN TI", author="Ivan")
    lower = _result(book_id="4", title="san ti", author="Ivan")

    groups = group_results([plain, punct, upper, lower])

    # "三体" == "三体。" and "SAN TI" == "san ti" after normalization,
    # but the CJK and Latin titles are still two different books.
    assert len(groups) == 2
    assert sorted(r["book_id"] for r in groups[0].sources) == ["1", "2"]
    assert sorted(r["book_id"] for r in groups[1].sources) == ["3", "4"]


# --------------------------------------------------------------------------- #
# keyword influence
# --------------------------------------------------------------------------- #
def test_keyword_shifts_primary_selection() -> None:
    # Inside one group the normalized titles are identical, so the keyword
    # can only change relevance through the author signal: all other
    # fields (word count, update date, priority, completeness) are equal,
    # making relevance the only differing signal.
    authored = _result(book_id="1", author="刘慈欣")
    anonymous = _result(book_id="2", author="")
    results = [anonymous, authored]

    with_keyword = group_results(results, keyword="刘慈欣")
    assert len(with_keyword) == 1
    assert with_keyword[0].primary is authored
    assert [r["book_id"] for r in with_keyword[0].sources] == ["1", "2"]

    # Without a keyword both score equally: the first occurrence wins.
    without_keyword = group_results(results, keyword="")
    assert without_keyword[0].primary is anonymous


# --------------------------------------------------------------------------- #
# group ordering
# --------------------------------------------------------------------------- #
def test_group_order_follows_primary_position() -> None:
    # "甲"'s group appears first in the input (index 0), but its primary
    # is the stronger duplicate at index 2, so the "乙" group (whose
    # primary sits at index 1) is listed first.
    a_weak = _result(book_id="a1", title="甲", author="A", word_count="1万字")
    b = _result(book_id="b1", title="乙", author="B", word_count="")
    a_strong = _result(book_id="a2", title="甲", author="A", word_count="300万字")

    groups = group_results([a_weak, b, a_strong])

    assert [g.primary["book_id"] for g in groups] == ["b1", "a2"]
    assert [r["book_id"] for r in groups[0].sources] == ["b1"]
    assert [r["book_id"] for r in groups[1].sources] == ["a2", "a1"]


def test_sorted_input_keeps_relative_group_order() -> None:
    # Pre-sorted input (best first): the group primaries lead their groups,
    # so the output preserves the input order of the books.
    a_best = _result(book_id="a", title="甲", author="A", word_count="300万字")
    b_best = _result(book_id="b", title="乙", author="B", word_count="200万字")
    a_extra = _result(book_id="a2", title="甲", author="A", word_count="10万字")

    groups = group_results([a_best, b_best, a_extra])

    assert [g.primary["book_id"] for g in groups] == ["a", "b"]


# --------------------------------------------------------------------------- #
# purity
# --------------------------------------------------------------------------- #
def test_input_list_is_not_modified() -> None:
    results = [
        _result(book_id="1", title="三体", author="刘慈欣"),
        _result(book_id="2", title="SAN TI", author="Ivan"),
        _result(book_id="3", title="《三体》", author="刘慈欣"),
    ]
    snapshot = [dict(r) for r in results]

    group_results(results, keyword="三体")

    assert results == snapshot
