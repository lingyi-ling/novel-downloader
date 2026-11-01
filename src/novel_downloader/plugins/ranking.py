#!/usr/bin/env python3
"""
novel_downloader.plugins.ranking
--------------------------------

Quality-based ranking helpers for aggregated search results.

Aggregated results arrive from many sites as raw, heterogeneous strings
(e.g. ``"12.3万字"``, ``"3天前"``). :func:`rank_score` blends several
signals into a 0..1 score so the most relevant, substantial, fresh and
complete entries surface first::

    keyword relevance  (55%)  how well the keyword matches title / author
    word count         (15%)  how substantial the book is
    update recency     (12%)  how recently the book was updated
    site priority      ( 8%)  shared preference for well-maintained sites
    completeness       (10%)  cover image / latest chapter present

Keyword relevance is deliberately the dominant signal: an exact title match
should outrank a merely similar (e.g. derivative) title even when the latter
was updated more recently.

Because plain ``max(title, author)`` cannot tell a search for an author from a
search for a title, :func:`detect_query_intent` first asks whether the result
set as a whole looks title-like or author-like, and
:func:`sort_results_with_intent` feeds that verdict back into the relevance
signal (see the intent constants below).

Every helper is a pure function (no I/O); ``now`` can be injected to keep
the recency signal deterministic in tests.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any

from novel_downloader.schemas import SearchResult

__all__ = [
    "INTENT_LONG_MIN",
    "INTENT_SHORT_MAX",
    "INTENT_WEIGHTS",
    "SORT_KEYS",
    "default_order",
    "detect_query_intent",
    "normalize_text",
    "parse_update_date",
    "parse_word_count",
    "rank_score",
    "relevance_of",
    "sort_results",
    "sort_results_with_intent",
]

#: Supported sort keys (shared by the CLI and the Web UI).
SORT_KEYS: tuple[str, ...] = (
    "quality",
    "priority",
    "site",
    "title",
    "author",
    "words",
    "update",
)

#: Signal weights; the five values sum to 1.0.
_W_RELEVANCE = 0.55
_W_WORDS = 0.15
_W_RECENCY = 0.12
_W_SITE = 0.08
_W_COMPLETE = 0.10

#: The intent values accepted by the relevance helpers.  Any other string is
#: coerced to the default ``"either"`` instead of raising: intent may come
#: straight from a CLI/API query parameter and must never break a search.
_VALID_INTENTS = frozenset({"author", "either", "title"})

#: Keyword lengths up to this many characters form the short tier.
#:
#: Chinese pen names are overwhelmingly 2-4 characters ("天蚕土豆", "老舍",
#: "猫腻", "烽火戏诸侯"); a 4-character query is therefore far more often an
#: author than a full title, so the author side gets the larger weight.
INTENT_SHORT_MAX = 4

#: Keyword lengths of at least this many characters form the long tier.
#:
#: Web-novel titles are routinely 5-12 characters ("重生之都市修仙",
#: "我在东京教剑道"); once a query is 10+ characters the odds that it is a
#: title rather than a pen name are high, so the title side gets the larger
#: weight.
INTENT_LONG_MIN = 10

#: ``tier -> (title weight, author weight)`` used to combine per-field
#: similarity.  The middle tier (5-9 characters) is deliberately balanced:
#: at that length both interpretations are plausible, so the actual matches
#: alone should decide.
INTENT_WEIGHTS: dict[str, tuple[float, float]] = {
    "short": (0.35, 0.65),
    "medium": (0.5, 0.5),
    "long": (0.65, 0.35),
}

#: A weighted title/author lead narrower than this fraction of their sum is
#: not treated as evidence for either side.  Without it, tiny coverage
#: differences on a noisy result page would flip the intent on every search.
_INTENT_MARGIN = 0.15

#: Historical asymmetry kept for the default ``"either"`` path: an author
#: match is discounted to 0.9 so an equally strong title match always wins.
_EITHER_AUTHOR_WEIGHT = 0.9

#: Weight of the non-primary field once an intent has been decided.  Low
#: enough that the primary field dominates (a contained ``"土豆"`` in an
#: author must beat a book literally *titled* ``"土豆"`` under author intent),
#: high enough that a book with no primary match still gets partial credit.
_INTENT_SECONDARY_WEIGHT = 0.6

_DAY = timedelta(days=1)

#: Values that all mean "unknown" once stripped and casefolded.
_NULL_TOKENS = frozenset({"", "-", "--", "—", "未知", "暂无", "none", "n/a"})

_NON_WORD_RE = re.compile(r"[\W_]+")

#: A word count: number (optionally with thousands separators / a decimal)
#: followed by an optional single magnitude character.
_WORD_COUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([a-zA-Z万亿千百日十])?")

#: A relative update date such as "3天前" / "2个月前".
_REL_AGO_RE = re.compile(
    r"(\d+)\s*(秒钟|秒|分钟|分|个小时|小时|天|日|周|星期|个月|月|年)\s*前"
)

#: Absolute formats grouped by the separator they require.  A string can only
#: match a format whose literal separator it contains, so trying just that
#: group yields the same result as trying every format in order.
_DASH_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)
_SLASH_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)
_DOT_FORMATS = ("%Y.%m.%d",)
_CJK_FORMATS = (
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)


#: Fast-path shapes for the slash and CJK formats.  ``strptime`` accepts
#: 1-2 digit month/day/hour/minute/second fields and exactly four year digits;
#: these patterns mirror that, so a successful match builds the same datetime.
_SLASH_DATE_RE = re.compile(
    r"(\d{4})/(\d{1,2})/(\d{1,2})(?: (\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
    re.ASCII,
)
_CJK_DATE_RE = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日(?: (\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
    re.ASCII,
)
_DOT_DATE_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", re.ASCII)


def _formats_for(text: str) -> tuple[str, ...]:
    """Return the absolute formats that can possibly match ``text``."""
    if "年" in text:
        return _CJK_FORMATS
    if "-" in text:
        return _DASH_FORMATS
    if "/" in text:
        return _SLASH_FORMATS
    if "." in text:
        return _DOT_FORMATS
    return ()


def _fast_parse_date(text: str) -> datetime | None:
    """Parse the slash/dot/CJK shapes without going through ``strptime``."""
    if "年" in text:
        match = _CJK_DATE_RE.fullmatch(text)
    elif "/" in text:
        match = _SLASH_DATE_RE.fullmatch(text)
    elif "." in text:
        match = _DOT_DATE_RE.fullmatch(text)
    else:
        return None
    if match is None:
        return None
    parts = [int(part) if part else 0 for part in match.groups()]
    if len(parts) == 3:  # date-only ".", slash and CJK patterns
        year, month, day = parts
        hour = minute = second = 0
    else:
        year, month, day, hour, minute, second = parts
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


#: Multipliers for the magnitude characters found in ``_WORD_COUNT_RE``.
_UNIT_SCALE = {
    "亿": 1e8,
    "万": 1e4,
    "千": 1e3,
    "百": 1e2,
    "十": 1e1,
    "k": 1e3,
    "m": 1e6,
}


@lru_cache(maxsize=8192)
def _normalize_cached(text: str) -> str:
    return _NON_WORD_RE.sub("", text.casefold())


def normalize_text(value: object) -> str:
    """Casefold ``value`` and strip everything but letters, digits and CJK."""
    return _normalize_cached(str(value if value is not None else ""))


def parse_word_count(value: object) -> float | None:
    """
    Parse a raw word-count string into a number, or ``None`` when unknown.

    Handles plain numbers (``"1234"``), thousands separators (``"1,234"``),
    decimals (``"12.3"``) and unit suffixes (``"12.3万字"`` -> 123000,
    ``"1.2M"`` -> 1200000, ``"3千字"`` -> 3000).
    """
    text = str(value if value is not None else "").strip()
    if text.casefold() in _NULL_TOKENS:
        return None

    text = text.replace("，", ",")
    match = _WORD_COUNT_RE.search(text)
    if match is None:
        return None

    number = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").casefold()
    return number * _UNIT_SCALE.get(unit, 1.0)


def _relative_delta(amount: int, unit: str) -> timedelta:
    if unit in {"秒", "秒钟"}:
        return timedelta(seconds=amount)
    if unit in {"分", "分钟"}:
        return timedelta(minutes=amount)
    if unit in {"小时", "个小时"}:
        return timedelta(hours=amount)
    if unit in {"天", "日"}:
        return timedelta(days=amount)
    if unit in {"周", "星期"}:
        return timedelta(weeks=amount)
    if unit in {"月", "个月"}:
        return timedelta(days=30 * amount)
    if unit == "年":
        return timedelta(days=365 * amount)
    return timedelta(0)


def parse_update_date(value: object, *, now: datetime | None = None) -> datetime | None:
    """
    Parse a raw update-date string into a ``datetime``, or ``None``.

    Both absolute formats (``2024-05-01``, ``2024/5/1``, ``2024年5月1日``,
    ISO) and the relative keywords (``刚刚``/``昨天``/``前天``) / durations
    such as ``"3天前"`` / ``"2个月前"`` are recognised; the two sets are
    disjoint, so their internal evaluation order does not affect the result.
    """
    text = str(value if value is not None else "").strip()
    if text.casefold() in _NULL_TOKENS:
        return None

    current = now or datetime.now()

    # Relative forms are checked first: none of them can match an absolute
    # format (those never accept a trailing "前" or the keyword words), so the
    # result is identical while sparing ten failing ``strptime`` calls for
    # every "3天前"-style value seen on real sites.
    lowered = text.casefold()
    if lowered in {"刚刚", "刚才", "just now", "justnow"}:
        return current
    if lowered in {"今天", "today"}:
        return current
    if lowered in {"昨天", "yesterday"}:
        return current - _DAY
    if lowered == "前天":
        return current - 2 * _DAY

    match = _REL_AGO_RE.search(text)
    if match is not None:
        return current - _relative_delta(int(match.group(1)), match.group(2))

    formats = _formats_for(text)
    if "年" in text or "/" in text or "." in text:
        parsed = _fast_parse_date(text)
        if parsed is not None:
            return parsed
    # ISO parsing is implemented in C and covers the common dash-separated
    # forms; the strptime formats stay as a fallback for inputs such as
    # non-padded dates that ``fromisoformat`` rejects.
    elif "-" in text:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _coverage(needle: str, haystack: str) -> float:
    if not needle:
        return 0.0
    haystack_chars = set(haystack)
    hits = sum(1 for ch in needle if ch in haystack_chars)
    return hits / len(needle)


def _match_score(
    keyword: str,
    text: str,
    *,
    exact: float,
    prefix: float,
    contains: float,
    base: float,
) -> float:
    if not text:
        return 0.0
    if text == keyword:
        return exact
    if text.startswith(keyword):
        return prefix
    if keyword in text:
        return contains
    return base * _coverage(keyword, text)


@lru_cache(maxsize=8192)
def _similarity_cached(needle: str, text: str) -> float:
    """
    Return the 0..1 similarity between two already-normalised strings.

    Exact / prefix / substring relations reuse :func:`_match_score` with the
    *same* parameters for both fields, so title and author are compared on a
    level playing field.  For merely overlapping strings the score blends
    character coverage (order-insensitive, but robust for the sparse CJK
    overlap where only one or two characters coincide) with
    :class:`difflib.SequenceMatcher` (order-sensitive, so "苍穹斗破" scores
    lower than "斗破苍穹").  Both are pure standard library.
    """
    if not needle or not text:
        return 0.0
    matched = _match_score(
        needle, text, exact=1.0, prefix=0.85, contains=0.75, base=0.0
    )
    if matched:
        return matched
    coverage = _coverage(needle, text)
    ordered = SequenceMatcher(None, needle, text).ratio()
    return 0.6 * (0.5 * coverage + 0.5 * ordered)


def _similarity(keyword: object, text: object) -> float:
    """Normalise both sides, then score them with :func:`_similarity_cached`."""
    return _similarity_cached(normalize_text(keyword), normalize_text(text))


def _intent_tier(length: int) -> str:
    """Map a normalised keyword length to an :data:`INTENT_WEIGHTS` tier."""
    if length <= INTENT_SHORT_MAX:
        return "short"
    if length >= INTENT_LONG_MIN:
        return "long"
    return "medium"


def _normalize_intent(intent: str) -> str:
    """Coerce an unknown intent string to ``"either"`` (never raises)."""
    return intent if intent in _VALID_INTENTS else "either"


def detect_query_intent(keyword: str, results: Sequence[SearchResult]) -> str:
    """
    Decide whether ``keyword`` looks like a title, an author, or neither.

    Returns ``"title"``, ``"author"`` or ``"either"``.

    Heuristic
    ---------
    Plain ``max(title_score, author_score)`` relevance cannot notice *which*
    field a whole result set agrees on.  This function accumulates a
    similarity score for both fields over every result, weights the two sums
    by the keyword-length tier (:data:`INTENT_WEIGHTS`), and compares them:

    * a short query (<= 4 characters) leans ``"author"``, because Chinese pen
      names are typically 2-4 characters while full titles rarely are;
    * 5-9 characters is balanced -- both readings are plausible;
    * a long query (>= 10 characters) leans ``"title"``, because that is
      almost always a full web-novel title rather than a pen name.

    When the weighted sums are within :data:`_INTENT_MARGIN` of each other,
    the evidence is too weak or contradictory and ``"either"`` is returned,
    which keeps the historical ranking behaviour.  An empty keyword or a
    result set with no overlap at all also yields ``"either"``.

    The function is pure with respect to ``results`` (they are only read) and
    never mutates the input.
    """
    needle = normalize_text(keyword)
    if not needle:
        return "either"

    title_weight, author_weight = INTENT_WEIGHTS[_intent_tier(len(needle))]
    title_total = 0.0
    author_total = 0.0
    for result in results:
        title_total += _similarity(needle, result.get("title"))
        author_total += _similarity(needle, result.get("author"))

    title_score = title_total * title_weight
    author_score = author_total * author_weight
    total = title_score + author_score
    if total <= 0.0:
        return "either"
    if abs(title_score - author_score) / total <= _INTENT_MARGIN:
        return "either"
    return "title" if title_score > author_score else "author"


def _relevance(
    keyword: str, title: str, author: str, *, intent: str = "either"
) -> float:
    """
    Return the 0..1 keyword relevance of one title/author pair.

    ``intent`` changes which field is primary:

    * ``"either"`` (default) keeps the historical behaviour, i.e. the larger
      of ``title_score`` and ``author_score * 0.9``; output is unchanged.
    * ``"author"`` puts the author first: the title score is scaled down by
      :data:`_INTENT_SECONDARY_WEIGHT`, so a genuine author hit outranks a
      book whose *title* merely contains the same characters.
    * ``"title"`` is the mirror image, demoting the author score.

    Unknown intent values fall back to ``"either"``.
    """
    needle = normalize_text(keyword)
    if not needle:
        return 0.5

    title_score = _match_score(
        needle, normalize_text(title), exact=1.0, prefix=0.75, contains=0.7, base=0.55
    )
    author_score = _match_score(
        needle, normalize_text(author), exact=0.9, prefix=0.7, contains=0.7, base=0.5
    )

    resolved = _normalize_intent(intent)
    if resolved == "author":
        return max(author_score, title_score * _INTENT_SECONDARY_WEIGHT)
    if resolved == "title":
        return max(title_score, author_score * _INTENT_SECONDARY_WEIGHT)
    return max(title_score, author_score * _EITHER_AUTHOR_WEIGHT)


def relevance_of(
    result: SearchResult, keyword: str, *, intent: str = "either"
) -> float:
    """
    Return the 0..1 keyword relevance of ``result`` under ``intent``.

    Public counterpart of :func:`_relevance` for callers that need the raw
    relevance (CLI/API display, custom filtering) without the quality blend.
    ``intent`` accepts ``"title"``, ``"author"`` or ``"either"``; anything
    else is treated as ``"either"``.  An empty keyword yields the neutral
    ``0.5`` for every result.
    """
    return _relevance(
        keyword,
        str(result.get("title") or ""),
        str(result.get("author") or ""),
        intent=intent,
    )


def _words_score(words: float | None) -> float:
    if words is None or words <= 0:
        return 0.5
    return min(1.0, math.log10(words) / 7.0)


def _recency_score(updated: datetime | None, now: datetime) -> float:
    if updated is None:
        return 0.5
    # ``parse_update_date`` yields *aware* datetimes for ISO strings with a
    # UTC offset, while the clock used for scoring is naive.  Fold any aware
    # datetime onto that naive local timeline first: subtracting mixed
    # offset-aware/naive datetimes raises ``TypeError`` and would take down
    # the whole aggregate search.
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    if updated.tzinfo is not None:
        updated = updated.astimezone().replace(tzinfo=None)
    days = max(0.0, (now - updated).total_seconds() / 86400.0)
    if days <= 1:
        return 1.0
    if days <= 7:
        return 0.98
    if days <= 30:
        return 0.95
    if days <= 90:
        return 0.9
    if days <= 365:
        return 0.8
    if days <= 730:
        return 0.6
    return 0.5


def _site_score(priority: object) -> float:
    try:
        value = float(priority)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.6
    if value <= 10:
        return 1.0
    if value <= 50:
        return 0.9
    if value <= 100:
        return 0.75
    if value <= 200:
        return 0.6
    if value <= 500:
        return 0.45
    if value <= 1000:
        return 0.3
    return 0.2


def _completeness_score(result: SearchResult) -> float:
    score = 0.0
    if str(result.get("cover_url") or "").strip():
        score += 0.5
    if str(result.get("latest_chapter") or "").strip():
        score += 0.5
    return score


def rank_score(
    result: SearchResult,
    keyword: str = "",
    *,
    intent: str = "either",
    now: datetime | None = None,
) -> float:
    """
    Return a 0..1 quality score for ``result`` given the search ``keyword``.

    ``intent`` is forwarded to :func:`_relevance`; the default ``"either"``
    reproduces the historical score exactly.
    """
    current = now or datetime.now()
    relevance = _relevance(
        keyword,
        str(result.get("title") or ""),
        str(result.get("author") or ""),
        intent=intent,
    )
    words = _words_score(parse_word_count(result.get("word_count")))
    recency = _recency_score(
        parse_update_date(result.get("update_date"), now=current), current
    )
    site = _site_score(result.get("priority"))
    completeness = _completeness_score(result)
    return (
        _W_RELEVANCE * relevance
        + _W_WORDS * words
        + _W_RECENCY * recency
        + _W_SITE * site
        + _W_COMPLETE * completeness
    )


def default_order(by: str) -> str:
    """Return the natural sort direction for a given key (``"asc"``/``"desc"``)."""
    return "desc" if by in {"quality", "words", "update"} else "asc"


def sort_results(
    results: list[SearchResult],
    *,
    by: str = "quality",
    order: str | None = None,
    keyword: str = "",
    intent: str = "either",
    now: datetime | None = None,
    site_labels: Mapping[str, str] | None = None,
) -> None:
    """
    Sort ``results`` in place.

    ``order`` defaults to :func:`default_order` for the chosen ``by``.
    ``keyword`` feeds the relevance signal used by ``by="quality"``, and
    ``site_labels`` maps a site key to its display label for ``by="site"``.
    ``intent`` selects how title and author are weighted
    (:func:`_relevance`); the default ``"either"`` is the historical
    behaviour and leaves existing output unchanged.
    """
    direction = order or default_order(by)
    reverse = direction == "desc"
    current = now or datetime.now()

    def _words_key(result: SearchResult) -> float:
        words = parse_word_count(result.get("word_count"))
        return -1.0 if words is None else words

    def _update_key(result: SearchResult) -> float:
        updated = parse_update_date(result.get("update_date"), now=current)
        if updated is None:
            return -1.0
        if updated.tzinfo is None:
            try:
                return updated.timestamp()
            except (OSError, OverflowError, ValueError):
                # 一个 naive ``timestamp()`` 会查平台时区库：Windows 上
                # 拒绝范围外的日期（OSError），Linux 上拒绝（ValueError）。
                # 极端值按 UTC 解释是纯算术，既不炸也能正确排在两端。
                updated = updated.replace(tzinfo=UTC)
        return updated.timestamp()

    def _priority_key(result: SearchResult) -> float:
        try:
            return float(result.get("priority", 0))
        except (TypeError, ValueError):
            return 0.0

    def _site_key(result: SearchResult) -> str:
        key = str(result.get("site") or "")
        label = site_labels.get(key, key) if site_labels else key
        return normalize_text(label)

    def _title_key(result: SearchResult) -> str:
        return normalize_text(result.get("title"))

    def _author_key(result: SearchResult) -> str:
        return normalize_text(result.get("author"))

    key_fns: dict[str, Callable[[SearchResult], Any]] = {
        "quality": lambda r: rank_score(r, keyword, intent=intent, now=current),
        "priority": _priority_key,
        "site": _site_key,
        "title": _title_key,
        "author": _author_key,
        "words": _words_key,
        "update": _update_key,
    }
    key_fn = key_fns.get(by, key_fns["quality"])

    # Two stable passes: the first fixes a deterministic tie-break order, the
    # second applies the primary key (and direction) on top of it.
    results.sort(
        key=lambda r: (
            str(r.get("site") or ""),
            normalize_text(r.get("title")),
            str(r.get("book_id") or ""),
        )
    )
    results.sort(key=key_fn, reverse=reverse)


def sort_results_with_intent(
    results: list[SearchResult],
    keyword: str,
    *,
    by: str = "quality",
    order: str | None = None,
    min_relevance: float | None = None,
    now: datetime | None = None,
) -> None:
    """
    Sort ``results`` in place after inferring what ``keyword`` refers to.

    The intent is detected once with :func:`detect_query_intent` and handed to
    :func:`rank_score` via :func:`sort_results`, so an author search ranks the
    author's own books above books whose *title* happens to contain the same
    characters (and vice versa).

    ``min_relevance`` (0..1, or ``None`` for no filtering) drops entries whose
    :func:`relevance_of` under the detected intent falls below the threshold.
    Filtering never empties the result set: if nothing clears the bar, entries
    with any positive relevance are kept instead, and if even those do not
    exist the input is left untouched so callers always get something back.

    Other parameters mirror :func:`sort_results`; ``by="quality"`` (the
    default) is the key whose result intent actually improves.
    """
    current = now or datetime.now()
    intent = detect_query_intent(keyword, results)

    if min_relevance is not None:
        kept = [
            item
            for item in results
            if relevance_of(item, keyword, intent=intent) >= min_relevance
        ]
        if not kept:
            # Fall back to "anything that matches at all" rather than handing
            # back an empty page just because the threshold was too strict.
            kept = [
                item
                for item in results
                if relevance_of(item, keyword, intent=intent) > 0.0
            ]
        if kept:
            results[:] = kept
        # Otherwise nothing is relevant: keep the input so the caller still
        # sees the raw results (the historical "never clear the page" rule).

    sort_results(
        results,
        by=by,
        order=order,
        keyword=keyword,
        intent=intent,
        now=current,
    )
