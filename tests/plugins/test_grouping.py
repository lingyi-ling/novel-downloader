#!/usr/bin/env python3
"""Boundary and equivalence tests for same-book grouping.

``tests/test_grouping.py`` covers the documented happy paths; this module
adds the awkward inputs (blank titles, author conflicts, very long titles)
and locks the optimised O(n) clustering against a straightforward reference
implementation of the previous O(n^2) scan.
"""

from __future__ import annotations

import random
from datetime import datetime

from novel_downloader.plugins.grouping import ResultGroup, group_results
from novel_downloader.plugins.ranking import normalize_text, rank_score
from novel_downloader.schemas import SearchResult


def _result(**overrides: object) -> SearchResult:
    base: dict[str, object] = {
        "site": "alpha",
        "book_id": "1",
        "book_url": "https://example.com/1",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "",
        "update_date": "2020-01-01",  # far from every recency boundary
        "word_count": "",
        "priority": 100,
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


def _shape(groups: list[ResultGroup]) -> list[tuple[str, list[str]]]:
    """Reduce groups to (primary book_id, source book_ids) for comparison."""
    return [
        (str(group.primary["book_id"]), [str(r["book_id"]) for r in group.sources])
        for group in groups
    ]


# --------------------------------------------------------------------------- #
# reference implementation (the previous O(n^2) algorithm)
# --------------------------------------------------------------------------- #
class _RefCluster:
    __slots__ = ("author", "entries")

    def __init__(self) -> None:
        self.entries: list[tuple[int, object]] = []
        self.author = ""


def _reference_shape(
    results: list[SearchResult], keyword: str
) -> list[tuple[str, list[str]]]:
    current = datetime.now()
    by_title: dict[str, list[_RefCluster]] = {}
    for index, result in enumerate(results):
        title_key = normalize_text(result.get("title"))
        author_key = normalize_text(result.get("author"))
        clusters = by_title.setdefault(title_key, [])
        target = next(
            (
                cluster
                for cluster in clusters
                if not author_key or not cluster.author or cluster.author == author_key
            ),
            None,
        )
        if target is None:
            target = _RefCluster()
            clusters.append(target)
        target.entries.append((index, result))
        if author_key:
            target.author = author_key

    groups: list[tuple[int, str, list[str]]] = []
    for clusters in by_title.values():
        for cluster in clusters:
            scored = sorted(
                cluster.entries,
                key=lambda item: (-rank_score(item[1], keyword, now=current), item[0]),  # type: ignore[arg-type]
            )
            groups.append(
                (
                    scored[0][0],
                    str(scored[0][1]["book_id"]),  # type: ignore[index]
                    [str(result["book_id"]) for _, result in scored],
                )
            )
    groups.sort(key=lambda pair: pair[0])
    return [(primary, sources) for _, primary, sources in groups]


# --------------------------------------------------------------------------- #
# boundary behaviour
# --------------------------------------------------------------------------- #
def test_all_titles_empty_groups_by_author() -> None:
    a1 = _result(book_id="a1", title="", author="甲")
    b1 = _result(book_id="b1", title="", author="乙")
    a2 = _result(book_id="a2", title="", author="甲")

    groups = group_results([a1, b1, a2])

    assert len(groups) == 2
    assert _shape(groups) == [("a1", ["a1", "a2"]), ("b1", ["b1"])]


def test_all_titles_and_authors_empty_merge_into_one_group() -> None:
    first = _result(book_id="1", title="", author="")
    second = _result(book_id="2", title="", author="")

    groups = group_results([first, second])

    assert _shape(groups) == [("1", ["1", "2"])]


def test_missing_title_and_author_keys_do_not_crash() -> None:
    sparse: SearchResult = {
        "site": "alpha",
        "book_id": "x",
        "book_url": "",
        "cover_url": "",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 100,
    }  # title / author omitted entirely
    groups = group_results([sparse, sparse])
    assert len(groups) == 1
    assert len(groups[0].sources) == 2


def test_author_conflict_splits_even_with_interleaved_unknowns() -> None:
    a1 = _result(book_id="a1", author="甲")
    unknown = _result(book_id="u", author="")
    b1 = _result(book_id="b1", author="乙")
    a2 = _result(book_id="a2", author="甲")
    unknown2 = _result(book_id="u2", author="")

    groups = group_results([a1, unknown, b1, a2, unknown2])

    # Unknown authors always attach to the first cluster of their title.
    assert _shape(groups) == [("a1", ["a1", "u", "a2", "u2"]), ("b1", ["b1"])]


def test_very_long_titles_merge_after_normalization() -> None:
    base = "长" * 5000
    plain = _result(book_id="1", title=base)
    decorated = _result(book_id="2", title=f"《{base}》!")
    different = _result(book_id="3", title=f"{base}续")

    groups = group_results([plain, decorated, different])

    assert len(groups) == 2
    assert _shape(groups)[0] == ("1", ["1", "2"])
    assert _shape(groups)[1] == ("3", ["3"])


def test_grouping_is_idempotent() -> None:
    results = [
        _result(book_id="a1", title="三体", author="刘慈欣", word_count="10万字"),
        _result(book_id="b1", title="球状闪电", author="刘慈欣", word_count="5万字"),
        _result(book_id="a2", title="《三体》", author="刘慈欣", word_count="300万字"),
        _result(book_id="c1", title="三体", author="别人", word_count="1万字"),
    ]
    first = _shape(group_results(results, keyword="三体"))
    second = _shape(group_results(results, keyword="三体"))
    assert first == second
    # Groups follow the input position of their primary: b1 (index 1),
    # then the 三体 group's primary a2 (index 2), then c1 (index 3).
    assert first == [("b1", ["b1"]), ("a2", ["a2", "a1"]), ("c1", ["c1"])]


def test_grouping_tolerates_timezone_aware_update_dates() -> None:
    # ``parse_update_date`` produces aware datetimes for ISO strings with a
    # UTC offset; the scoring inside ``group_results`` must not crash on them,
    # and same-title/different-author entries must still stay separate.
    same_author = _result(
        book_id="a1",
        title="三体",
        author="刘慈欣",
        update_date="2024-06-01T12:00:00+08:00",
    )
    other_author = _result(
        book_id="b1",
        title="三体",
        author="别人",
        update_date="2020-01-01T00:00:00+00:00",
    )

    groups = group_results([same_author, other_author], keyword="三体")

    assert _shape(groups) == [("a1", ["a1"]), ("b1", ["b1"])]


def test_grouping_does_not_modify_results_even_for_weird_input() -> None:
    results = [
        _result(book_id="1", title="", author=""),
        _result(book_id="2", title="三体", author="未知"),
    ]
    snapshot = [dict(r) for r in results]
    group_results(results)
    assert [dict(r) for r in results] == snapshot


# --------------------------------------------------------------------------- #
# snapshot of the exact group layout
# --------------------------------------------------------------------------- #
def test_group_layout_snapshot() -> None:
    results = [
        _result(
            site="alpha",
            book_id="weak",
            title="斗破苍穹",
            author="天蚕土豆",
            word_count="1万字",
        ),
        _result(
            site="beta",
            book_id="other",
            title="另一本书",
            author="某人",
            word_count="300万字",
        ),
        _result(
            site="gamma",
            book_id="strong",
            title="《斗破苍穹》",
            author="天蚕土豆",
            word_count="300万字",
            cover_url="https://example.com/c.jpg",
            latest_chapter="第100章",
        ),
    ]

    # The "other" group's primary is at input index 1, so it is listed before
    # the 斗破苍穹 group whose primary sits at index 2.
    assert _shape(group_results(results, keyword="斗破苍穹")) == [
        ("other", ["other"]),
        ("strong", ["strong", "weak"]),
    ]


# --------------------------------------------------------------------------- #
# equivalence with the previous implementation on randomised input
# --------------------------------------------------------------------------- #
_TITLES = ("", "三体", "三体。", "《三体》", "SAN TI", "san ti", "甲", "乙", "长" * 300)
_AUTHORS = ("", "A", "B", "a", "A ", "未知")
_WORDS = ("", "1万字", "10万字", "300万字")
_DATES = ("", "2020-01-01", "3天前")
_PRIORITIES = (5, 100, 1000)


def test_optimised_grouping_matches_reference_on_random_input() -> None:
    rng = random.Random(20240615)
    for round_index in range(12):
        results = [
            _result(
                site=f"site{index % 4}",
                book_id=f"{round_index}-{index}",
                title=rng.choice(_TITLES),
                author=rng.choice(_AUTHORS),
                word_count=rng.choice(_WORDS),
                update_date=rng.choice(_DATES),
                priority=rng.choice(_PRIORITIES),
            )
            for index in range(120)
        ]
        keyword = rng.choice(("", "三体", "A", "未知"))
        assert _shape(group_results(results, keyword)) == _reference_shape(
            results, keyword
        )
