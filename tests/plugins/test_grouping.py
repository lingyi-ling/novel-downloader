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
from itertools import permutations

from novel_downloader.plugins.grouping import ResultGroup, group_results
from novel_downloader.plugins.ranking import normalize_text, rank_score, sort_results
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


def test_full_width_and_half_width_titles_and_authors_merge() -> None:
    # A Japanese/light-novel mirror may serve "ＳＡＯ" / "Ｒｅｋｉ" while the
    # user's other source serves half-width "SAO" / "Reki".  They are the same
    # book and must land in one group.
    half = _result(book_id="1", title="SAO", author="Reki")
    full = _result(book_id="2", site="beta", title="ＳＡＯ", author="Ｒｅｋｉ")

    groups = group_results([half, full])

    assert _shape(groups) == [("1", ["1", "2"])]


def test_full_width_title_does_not_fuse_distinct_authors() -> None:
    # Folding half/full-width must not weaken the author guard: the same title
    # with a different author is still a different book.
    first = _result(book_id="1", title="ＳＡＯ", author="Reki")
    second = _result(book_id="2", site="beta", title="SAO", author="Other")

    assert len(group_results([first, second])) == 2


def test_full_width_folding_keeps_distinct_volumes_apart() -> None:
    # NFKC folding is character-level, not "fold everything to one key":
    # volume numbers spelled as full-width digits or circled digits keep
    # their distinct numeric value and must stay separate books.
    one = _result(book_id="1", title="书1")
    two = _result(book_id="2", site="beta", title="书２")
    circled_one = _result(book_id="3", site="gamma", title="第①卷")
    circled_two = _result(book_id="4", site="delta", title="第②卷")

    groups = group_results([one, two, circled_one, circled_two])

    assert len(groups) == 4
    assert {str(g.primary["book_id"]) for g in groups} == {"1", "2", "3", "4"}


def test_full_width_author_folding_keeps_interleaved_unknown_with_first_author() -> (
    None
):
    # Full-width and half-width spellings of one author fold together, while a
    # genuinely different author stays split; an anonymous row in between
    # attaches to the first cluster, as documented.
    reki_full = _result(book_id="1", title="ＳＡＯ", author="Ｒｅｋｉ")
    anonymous = _result(book_id="2", site="beta", title="SAO", author="")
    other = _result(book_id="3", site="gamma", title="sao", author="Other")
    reki_half = _result(book_id="4", site="delta", title="SAO", author="Reki")

    groups = group_results([reki_full, anonymous, other, reki_half])

    members = {
        tuple(sorted(str(r["book_id"]) for r in group.sources)) for group in groups
    }
    assert members == {("1", "2", "4"), ("3",)}


# --------------------------------------------------------------------------- #
# decorative publication markers (second, conservative merge pass)
# --------------------------------------------------------------------------- #
def test_decorated_prefix_merges_with_plain_title() -> None:
    plain = _result(
        site="alpha",
        book_id="plain",
        title="斗罗：从武魂喷火龙开始！",
        author="唐家三少",
    )
    decorated = _result(
        site="beta",
        book_id="decorated",
        title="[网游]斗罗：从武魂喷火龙开始！",
        author="唐家三少",
    )

    groups = group_results([plain, decorated])

    assert _shape(groups) == [("plain", ["plain", "decorated"])]


def test_decorative_prefixes_and_suffixes_merge() -> None:
    plain = _result(
        book_id="plain",
        title="斗罗：从武魂喷火龙开始！",
        author="唐家三少",
    )
    decorated = _result(
        book_id="decorated",
        title="【完结】（精校版）斗罗：从武魂喷火龙开始！（全本）[TXT]",
        author="唐家三少",
    )

    groups = group_results([plain, decorated])

    assert _shape(groups) == [("plain", ["plain", "decorated"])]


def test_full_width_and_case_folded_decorations_merge() -> None:
    plain = _result(book_id="plain", title="三体", author="刘慈欣")
    decorated = _result(
        book_id="decorated",
        title="［ＴＸＴ］三体［精校版］",
        author="刘慈欣",
    )

    groups = group_results([plain, decorated])

    assert _shape(groups) == [("plain", ["plain", "decorated"])]


def test_decorated_title_with_different_author_stays_separate() -> None:
    known = _result(book_id="1", title="斗罗", author="甲")
    other = _result(book_id="2", title="[网游]斗罗", author="乙")

    groups = group_results([known, other])

    assert _shape(groups) == [("1", ["1"]), ("2", ["2"])]


def test_decorated_title_with_unknown_author_is_not_merged() -> None:
    # Cross-title merging requires *both* authors to be non-empty and equal,
    # unlike the exact-title pass where an unknown author joins the first group.
    plain = _result(book_id="1", title="斗罗", author="甲")
    anonymous = _result(book_id="2", title="[网游]斗罗", author="")
    both_anonymous = _result(book_id="3", title="[全本]斗罗", author="")

    groups = group_results([plain, anonymous, both_anonymous])

    assert _shape(groups) == [("1", ["1"]), ("2", ["2"]), ("3", ["3"])]


def test_decorated_volumes_with_different_numbers_stay_separate() -> None:
    # Only whitelisted marker contents are stripped.  ``第①卷`` / ``第②卷``
    # are real title text, so the two volumes remain distinct books.
    first = _result(book_id="1", title="斗罗（第①卷）[网游]", author="甲")
    second = _result(book_id="2", title="斗罗（第②卷）[网游]", author="甲")

    groups = group_results([first, second])

    assert _shape(groups) == [("1", ["1"]), ("2", ["2"])]


def test_unlisted_bracket_content_is_not_stripped() -> None:
    # A bracketed word that is part of the title (``同人``, ``上``) must not be
    # treated as a publication marker.
    plain = _result(book_id="1", title="斗罗", author="甲")
    derivative = _result(book_id="2", title="[同人]斗罗", author="甲")
    half_volume = _result(book_id="3", title="斗罗（上）", author="甲")

    groups = group_results([plain, derivative, half_volume])

    assert _shape(groups) == [("1", ["1"]), ("2", ["2"]), ("3", ["3"])]


def test_decorated_merge_orders_sources_by_rank_and_group_by_primary() -> None:
    weak = _result(
        site="alpha",
        book_id="weak",
        title="斗罗",
        author="甲",
        word_count="1万字",
    )
    other = _result(
        site="beta",
        book_id="other",
        title="另一本书",
        author="乙",
        word_count="300万字",
    )
    strong = _result(
        site="gamma",
        book_id="strong",
        title="[网游]斗罗",
        author="甲",
        word_count="300万字",
    )

    groups = group_results([weak, other, strong])

    # The other book's primary sits at input index 1, the merged 斗罗 group's
    # primary (the decorated, stronger copy) at index 2.
    assert _shape(groups) == [
        ("other", ["other"]),
        ("strong", ["strong", "weak"]),
    ]


def test_exact_title_grouping_still_merges_decorated_and_plain() -> None:
    # The new pass must build on, not replace, exact-title grouping.
    plain = _result(book_id="1", title="三体", author="刘慈欣")
    punctuated = _result(book_id="2", title="《三体》", author="刘慈欣")
    decorated = _result(book_id="3", title="[网游]三体", author="刘慈欣")
    other_author = _result(book_id="4", title="三体", author="别人")

    groups = group_results([plain, punctuated, decorated, other_author])

    assert _shape(groups) == [("1", ["1", "2", "3"]), ("4", ["4"])]


def test_ambiguous_core_cluster_is_not_merged() -> None:
    # "网游斗罗" as a literal title normalises to the same exact-title key as
    # "[网游]斗罗"; that cluster's entries disagree on the core, so it is kept
    # whole and never merged with the plain "斗罗" book.
    literal = _result(book_id="1", title="网游斗罗", author="甲")
    decorated = _result(book_id="2", title="[网游]斗罗", author="甲")
    plain = _result(book_id="3", title="斗罗", author="甲")

    groups = group_results([literal, decorated, plain])

    assert _shape(groups) == [("1", ["1", "2"]), ("3", ["3"])]


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


# --------------------------------------------------------------------------- #
# final-result telemetry (rank_score / match_tier) must stay derived data
# --------------------------------------------------------------------------- #
def test_telemetry_keys_never_influence_grouping_or_the_primary() -> None:
    weak = _result(site="alpha", book_id="weak", word_count="1万字")
    strong = _result(site="beta", book_id="strong", word_count="300万字")
    other = _result(site="gamma", book_id="other", title="另一本书", author="某人")
    baseline = _shape(group_results([weak, strong, other], keyword="三体"))
    assert baseline == [("strong", ["strong", "weak"]), ("other", ["other"])]

    # Values that would invert the outcome if grouping ever read them.
    poisoned = [
        {**weak, "rank_score": 1.0, "match_tier": 4},  # type: ignore[typeddict-item]
        {**strong, "rank_score": 0.0, "match_tier": 0},  # type: ignore[typeddict-item]
        {**other, "rank_score": 0.0, "match_tier": 0},  # type: ignore[typeddict-item]
    ]
    assert _shape(group_results(poisoned, keyword="三体")) == baseline  # type: ignore[arg-type]


def test_group_results_now_injection_controls_recency_ranking() -> None:
    # Two copies of one book differing only by update date.  Scored at 2024
    # the recent copy wins; scored in 2030 both are old and tie, so the
    # earlier input position decides.
    old = _result(site="alpha", book_id="old", update_date="2024-01-01")
    new = _result(site="beta", book_id="new", update_date="2024-06-14")

    assert _shape(group_results([old, new], now=datetime(2024, 6, 15, 12))) == [
        ("new", ["new", "old"])
    ]
    assert _shape(group_results([old, new], now=datetime(2030, 1, 1))) == [
        ("old", ["old", "new"])
    ]


# --------------------------------------------------------------------------- #
# arrival-order determinism
# --------------------------------------------------------------------------- #
_ARRIVAL_NOW = datetime(2024, 6, 15, 12, 0, 0)


def _url_shape(groups: list[ResultGroup]) -> list[tuple[str, list[str]]]:
    """Group layout keyed by URL (the rows below share empty book ids)."""
    return [
        (
            str(group.primary["book_url"]),
            [str(item["book_url"]) for item in group.sources],
        )
        for group in groups
    ]


def test_group_layout_is_deterministic_after_canonical_sort() -> None:
    # Every row ties on rank_score, so the primary and the source order are
    # decided purely by position.  The historical canonical key
    # (site, title, book_id) could not tell rows /a and /b apart (same site,
    # same title, both without an id), so the winner used to depend on which
    # row arrived first.  Sorting canonically first (as ``_finalize`` does)
    # must make the layout identical for every arrival order.
    rows = [
        _result(site="alpha", book_id="", book_url="https://example.com/a"),
        _result(site="alpha", book_id="", book_url="https://example.com/b"),
        _result(
            site="alpha",
            book_id="",
            book_url="https://example.com/c",
            title="三体外传",
            author="乙",
        ),
        _result(site="beta", book_id="", book_url="https://example.com/d"),
        _result(
            site="beta",
            book_id="",
            book_url="https://example.com/e",
            title="三体",
            author="乙",
        ),
    ]

    def layout(order: tuple[int, ...]) -> list[tuple[str, list[str]]]:
        work = [rows[index] for index in order]
        # Mirror _finalize: canonicalize before grouping.
        sort_results(work, keyword="", now=_ARRIVAL_NOW)
        return _url_shape(group_results(work, keyword="", now=_ARRIVAL_NOW))

    baseline = layout(tuple(range(len(rows))))
    assert baseline == [
        (
            "https://example.com/a",
            ["https://example.com/a", "https://example.com/b", "https://example.com/d"],
        ),
        ("https://example.com/c", ["https://example.com/c"]),
        ("https://example.com/e", ["https://example.com/e"]),
    ]

    for order in permutations(range(len(rows))):
        assert layout(order) == baseline
