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
was updated more recently.  Because relevance is one weighted sum though, a
metadata-rich row could still overtake a genuine primary-field hit, so
:func:`sort_results` orders ``by="quality"`` with a two-level key under a
decided intent: primary-field match tier first, then the quality score.

Because plain ``max(title, author)`` cannot tell a search for an author from a
search for a title, :func:`detect_query_intent` first asks whether the result
set as a whole looks title-like or author-like, and
:func:`sort_results_with_intent` feeds that verdict back into the relevance
signal (see the intent constants below).

A query may also name *both* fields ("斗破苍穹 天蚕土豆").  When the keyword
splits on a supported separator into two to four tokens, each token is scored
against title and author separately and the best title token / best author
token are combined; the whole-keyword score stays in the comparison, so a
title that legitimately contains a separator is never scored lower than
before.  A single-token query keeps the historical whole-string path exactly.

Every helper is a pure function (no I/O); ``now`` can be injected to keep
the recency signal deterministic in tests.
"""

from __future__ import annotations

import math
import re
import unicodedata
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
    "filter_by_relevance",
    "match_tier_of",
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

#: Weight of the non-primary field once an intent has been decided.  It keeps
#: a book with no primary match from scoring zero, but a weighted sum alone
#: cannot guarantee primacy: metadata (word count, recency, cover, ...) is
#: 45% of :func:`rank_score`, so a rich row whose *title* merely equals an
#: author's name used to outrank that author's own sparse rows.  Ordering
#: therefore uses the match tier of the primary field as the outer key (see
#: :func:`_match_tier`); this weight only shapes the residual relevance.
_INTENT_SECONDARY_WEIGHT = 0.6

#: Intents whose primary field is a single named attribute; these are the
#: intents that get the two-level (tier, score) ordering.
_PRIMARY_FIELD = {"author": "author", "title": "title"}

#: Match tiers, best first.  They mirror :func:`_match_score`'s branches
#: exactly -- exact equality, prefix, containment, partial character coverage
#: -- so the tier ordering and the relevance score can never disagree about
#: *what kind* of match a pair is.
_TIER_EXACT = 4
_TIER_PREFIX = 3
_TIER_CONTAINS = 2
_TIER_PARTIAL = 1
_TIER_NONE = 0

_DAY = timedelta(days=1)

#: Values that all mean "unknown" once stripped and casefolded.
_NULL_TOKENS = frozenset({"", "-", "--", "—", "未知", "暂无", "none", "n/a"})

_NON_WORD_RE = re.compile(r"[\W_]+")

#: Separators that split a "title + author" style query into tokens: any run
#: of whitespace (``\s`` includes the full-width space U+3000), the middle dot
#: sites put between a title and a pen name, and half/full-width commas.
_QUERY_TOKEN_RE = re.compile(r"[\s·,，\-]+")

#: Multi-token matching is enabled only for this token-count window.  Two to
#: four tokens covers the realistic shapes ("斗破苍穹 天蚕土豆",
#: "斗破苍穹 · 天蚕土豆"); one token is the historical whole-keyword query, and
#: five or more is far more likely a pasted sentence than a query.
_MIN_QUERY_TOKENS = 2
_MAX_QUERY_TOKENS = 4

#: A word count: number (optionally with thousands separators / a decimal)
#: followed by an optional single magnitude character.
_WORD_COUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([a-zA-Z万亿千百日十])?")

#: Longest normalized word-count label still parsed.  Real labels are short
#: ("12.3万字", "1,234", "总字数：123万字"); a longer value is a mis-scraped
#: title, summary or whole paragraph whose first number would otherwise be
#: read as a count and skew the ``words``/``quality`` ordering.  The limit is
#: deliberately far above every shape a site serves, so normal values are
#: unaffected.
_MAX_WORD_COUNT_LENGTH = 64

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
_DOT_FORMATS = (
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d",
)
_CJK_FORMATS = (
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)


#: Fast-path shapes for the slash, dot and CJK formats.  ``strptime`` accepts
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
_DOT_DATE_RE = re.compile(
    r"(\d{4})\.(\d{1,2})\.(\d{1,2})(?: (\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
    re.ASCII,
)


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
    if len(parts) == 3:  # date-only patterns (no time component parsed)
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
    # NFKC first: it folds full-width alphanumerics/spaces/punctuation onto
    # their canonical half-width forms and composes decomposed accents, so a
    # query typed in half-width matches a title/author served in full-width
    # (and vice versa).  This mirrors ``apps.music.aggregate.normalize_text``;
    # without it "ＡＢＣ" and "ABC" were treated as unrelated strings for
    # relevance, de-duplication, grouping and cache keys alike.
    text = unicodedata.normalize("NFKC", text)
    return _NON_WORD_RE.sub("", text.casefold())


def normalize_text(value: object) -> str:
    """
    Casefold ``value`` and strip everything but letters, digits and CJK.

    Full-width and half-width forms are folded together first, so ``"ＡＢＣ"``
    and ``"ABC"`` normalise to the same key.
    """
    return _normalize_cached(str(value if value is not None else ""))


@lru_cache(maxsize=1024)
def _query_tokens_cached(keyword: str) -> tuple[str, ...]:
    # Fold full-width forms *before* splitting: ``，`` and a full-width hyphen
    # are both listed/implied separators, and the rest of the module already
    # folds them so "斗破苍穹－天蚕土豆" matches the same book as its
    # half-width spelling.  Splitting the raw string used to keep the whole
    # expression as one unusable token.
    folded = unicodedata.normalize("NFKC", keyword)
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _QUERY_TOKEN_RE.split(folded):
        if not token:
            continue
        # Only tokens that survive normalization carry evidence.  Counting a
        # punctuation-only fragment (or a repeat of a token already seen)
        # towards the 2-4 window used to *disable* multi-token matching for a
        # perfectly good title+author query ("斗破苍穹 天蚕土豆 天蚕土豆 ...").
        identity = normalize_text(token)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        tokens.append(token)
    if _MIN_QUERY_TOKENS <= len(tokens) <= _MAX_QUERY_TOKENS:
        return tuple(tokens)
    return ()


def _query_tokens(keyword: str) -> tuple[str, ...]:
    """
    Return the tokens to score separately, or ``()`` to use the old path.

    A multi-field query ("斗破苍穹 天蚕土豆") does not literally occur in either
    the title or the author field, so the historical whole-string containment
    test scores it far too low.  Splitting it on whitespace (full-width
    included), ``·``, ``,``, ``，`` and ``-`` yields the field-shaped tokens
    instead.  The keyword is NFKC-folded first, so full-width separators (the
    full-width hyphen in particular) split exactly like their half-width
    counterparts.

    Only two to four *distinct, non-empty* tokens enable multi-token matching;
    any other count returns ``()`` so the caller keeps the historical
    whole-keyword behaviour (one token is a normal query, five or more is
    usually a pasted sentence).  Separator runs, punctuation-only fragments
    and repeated tokens are dropped first: they carry no evidence and must not
    consume the window.  The result is cached because relevance is computed
    once per result row.
    """
    return _query_tokens_cached(keyword)


@lru_cache(maxsize=8192)
def _word_count_cached(raw: str) -> float | None:
    """
    Parse one already-stringified word-count label (cached by raw text).

    The caller (:func:`parse_word_count`) has already turned the field into a
    string, so this layer only normalizes and parses.  The cache key is the raw
    text: aggregation, filtering and ranking each ask for the same labels
    repeatedly (a page of rows usually carries a handful of distinct
    ``"12.3万字"``-style strings), and the function is pure, so caching the
    result cannot change behaviour.
    """
    text = unicodedata.normalize("NFKC", raw).strip()
    if text.casefold() in _NULL_TOKENS:
        return None
    if len(text) > _MAX_WORD_COUNT_LENGTH:
        return None

    text = text.replace("，", ",")
    match = _WORD_COUNT_RE.search(text)
    if match is None:
        return None

    number = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").casefold()
    return number * _UNIT_SCALE.get(unit, 1.0)


def parse_word_count(value: object) -> float | None:
    """
    Parse a raw word-count string into a number, or ``None`` when unknown.

    Handles plain numbers (``"1234"``), thousands separators (``"1,234"``),
    decimals (``"12.3"``) and unit suffixes (``"12.3万字"`` -> 123000,
    ``"1.2M"`` -> 1200000, ``"3千字"`` -> 3000).

    The text is NFKC-normalised before parsing, so the full-width spellings
    sites actually serve -- ``"１．２万字"``, ``"１２３４"``, ``"１，２３４"`` --
    yield the same numbers as their half-width counterparts instead of being
    truncated at the first full-width separator (``"１．２万字"`` used to parse
    as just ``1.0``).  A value without any digit still returns ``None``.

    A label longer than :data:`_MAX_WORD_COUNT_LENGTH` characters is treated
    as "unknown" instead of being scraped for its first number: such values
    are mis-extracted descriptions rather than word counts, and reading e.g.
    a year or a chapter number out of a paragraph would distort the
    ``words``/``quality`` ordering.  Every value a bundled site produces is
    far below the limit.

    The parse is memoized by the raw label (see :func:`_word_count_cached`):
    ranking scores each row once, the local word-count filter then reads the
    same field, and the final annotation scores the surviving rows again.  A
    repeated label is therefore parsed once rather than two or three times.
    """
    return _word_count_cached(str(value if value is not None else ""))


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


def _parse_update_date_text(text: str, current: datetime) -> datetime | None:
    """
    Parse one already-stringified update-date label against ``current``.

    ``current`` is always supplied by the caller -- absolute dates do not use
    it, while relative words (``"3天前"``) do.  Keeping the clock explicit is
    what makes the memoized wrapper below safe: two calls that resolve a
    relative date against different instants can never share a cache entry.
    """
    if text.casefold() in _NULL_TOKENS:
        return None

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


@lru_cache(maxsize=4096)
def _update_date_cached(raw: str, now: datetime) -> datetime | None:
    """Memoized :func:`_parse_update_date_text` for one raw label and clock."""
    return _parse_update_date_text(unicodedata.normalize("NFKC", raw).strip(), now)


def parse_update_date(value: object, *, now: datetime | None = None) -> datetime | None:
    """
    Parse a raw update-date string into a ``datetime``, or ``None``.

    Both absolute formats (``2024-05-01``, ``2024/5/1``, ``2024.5.1 10:00``,
    ``2024年5月1日``, ISO) and the relative keywords (``刚刚``/``昨天``/
    ``前天``) / durations such as ``"3天前"`` / ``"2个月前"`` are recognised;
    the two sets are disjoint, so their internal evaluation order does not
    affect the result.

    The text is NFKC-normalised before parsing -- like
    :func:`parse_word_count` -- so the full-width spellings real sites serve
    (``"２０２４年５月１日"``, ``"２０２４－０５－０１"``, ``"２０２４/５/１"``)
    parse to the same date as their half-width counterparts.  Folding alone
    cannot turn a time-of-day value into a date: ``"１２：３０"`` becomes
    ``"12:30"``, which is not an absolute date format, so it still returns
    ``None``.

    When ``now`` is supplied the parse is memoized by ``(raw label, now)``
    (see :func:`_update_date_cached`): ranking, the local update-date filter
    and the final annotation all resolve the same labels against the same
    instant, so each distinct label is parsed once per call instead of once
    per row.  A relative date is never cached without a clock: ``now=None``
    means "use the current time", which must be read afresh on every call.
    An aware clock is not cached either: two equal instants may spell
    different local wall times, and a relative label's calendar date depends
    on that spelling.
    """
    raw = str(value if value is not None else "")
    if now is None:
        return _parse_update_date_text(
            unicodedata.normalize("NFKC", raw).strip(), datetime.now()
        )
    if now.tzinfo is not None:
        return _parse_update_date_text(unicodedata.normalize("NFKC", raw).strip(), now)
    return _update_date_cached(raw, now)


def _coverage(needle: str, haystack: str) -> float:
    if not needle:
        return 0.0
    # Building ``set(haystack)`` allocates a new set on every call, and this
    # helper runs several times per result row during ranking/grouping/
    # annotation.  Membership in a short haystack is cheaper without the set;
    # the result is identical because both forms test "does this character
    # occur anywhere in haystack".  A short haystack (a pen name) keeps the
    # set path, where its O(1) membership beats repeated scanning.
    if len(haystack) <= 8:
        haystack_chars = set(haystack)
        hits = sum(1 for ch in needle if ch in haystack_chars)
    else:
        hits = sum(1 for ch in needle if ch in haystack)
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


def _match_tier(keyword: str, text: str) -> int:
    """
    Return the match tier of an already-normalised ``keyword`` against ``text``.

    The branches are the same, in the same order, as :func:`_match_score`:
    exact equality, prefix, containment, then partial character coverage.
    Only the *kind* of match is reported (``_TIER_EXACT`` down to
    ``_TIER_NONE``), so two rows in the same tier are still ordered by their
    full :func:`rank_score`, while an empty or unrelated field always sorts
    below any real hit regardless of metadata.
    """
    if not keyword or not text:
        return _TIER_NONE
    if text == keyword:
        return _TIER_EXACT
    if text.startswith(keyword):
        return _TIER_PREFIX
    if keyword in text:
        return _TIER_CONTAINS
    if _coverage(keyword, text) > 0.0:
        return _TIER_PARTIAL
    return _TIER_NONE


def _field_scores(needle: str, title: str, author: str) -> tuple[float, float]:
    """Return the (title, author) match scores of one normalised ``needle``."""
    return (
        _match_score(needle, title, exact=1.0, prefix=0.75, contains=0.7, base=0.55),
        _match_score(needle, author, exact=0.9, prefix=0.7, contains=0.7, base=0.5),
    )


def _primary_match_tier(keyword: str, result: SearchResult, field: str) -> int:
    """Return the best :func:`_match_tier` for ``result``'s primary ``field``."""
    text = normalize_text(result.get(field))
    # The whole keyword stays in the comparison so a multi-token query never
    # downgrades a title/author that contains the exact keyword string.
    tier = _match_tier(normalize_text(keyword), text)
    for token in _query_tokens(keyword):
        token_needle = normalize_text(token)
        if token_needle:
            tier = max(tier, _match_tier(token_needle, text))
    return tier


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


def _normalize_intent(intent: object) -> str:
    """Coerce an unknown or unhashable intent value to ``"either"``."""
    if isinstance(intent, str) and intent in _VALID_INTENTS:
        return intent
    return "either"


def _intent_verdict(needle: str, results: Sequence[SearchResult]) -> str:
    """Score one normalised needle against both fields of ``results``."""
    title_weight, author_weight = INTENT_WEIGHTS[_intent_tier(len(needle))]
    # ``math.fsum`` is correctly rounded and independent of summation order,
    # so the verdict cannot flip when the same rows arrive in a different
    # order (e.g. sites completing concurrently).  A plain ``+=`` loop is
    # order-sensitive in the last bits, and this verdict feeds a threshold
    # comparison (:data:`_INTENT_MARGIN`).
    title_total = math.fsum(
        _similarity(needle, result.get("title")) for result in results
    )
    author_total = math.fsum(
        _similarity(needle, result.get("author")) for result in results
    )

    title_score = title_total * title_weight
    author_score = author_total * author_weight
    total = title_score + author_score
    if total <= 0.0:
        return "either"
    if abs(title_score - author_score) / total <= _INTENT_MARGIN:
        return "either"
    return "title" if title_score > author_score else "author"


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

    A two-to-four-token keyword (see :func:`_query_tokens`) is judged token by
    token with the same accumulation.  This is how a "title + author" query is
    recognised as mixed: one token points at the title field and another at the
    author field, so no single field can be declared primary and ``"either"``
    is returned.  Tokens without any evidence are ignored; if the remaining
    tokens all agree, their verdict is used.

    The function is pure with respect to ``results`` (they are only read) and
    never mutates the input.
    """
    needle = normalize_text(keyword)
    if not needle:
        return "either"

    tokens = _query_tokens(keyword)
    if tokens:
        verdicts: set[str] = set()
        for token in tokens:
            token_needle = normalize_text(token)
            if not token_needle:
                continue
            verdict = _intent_verdict(token_needle, results)
            if verdict != "either":
                verdicts.add(verdict)
        if len(verdicts) == 1:
            return verdicts.pop()
        # No evidence at all, or one token pointing each way.
        return "either"

    return _intent_verdict(needle, results)


def _relevance(
    keyword: str, title: str, author: str, *, intent: str = "either"
) -> float:
    """
    Return the 0..1 keyword relevance of one title/author pair.

    ``intent`` changes which field is primary:

    * ``"either"`` (default) keeps the historical behaviour, i.e. the larger
      of ``title_score`` and ``author_score * 0.9``; output is unchanged.
    * ``"author"`` puts the author first: the title score is scaled down by
      :data:`_INTENT_SECONDARY_WEIGHT`.  The score alone can still be
      overtaken by metadata; the ordering guarantee lives in
      :func:`sort_results`, which uses the author match tier as its outer key.
    * ``"title"`` is the mirror image, demoting the author score.

    A two-to-four-token keyword (see :func:`_query_tokens`) is additionally
    scored token by token: the best title token and the best author token are
    combined, so ``"斗破苍穹 天蚕土豆"`` matches a row titled 斗破苍穹 by
    天蚕土豆.  The whole-keyword scores stay in the ``max``, so the multi-token
    path can only raise relevance -- a title that contains a listed separator
    itself (``"你好，旧时光"``) scores exactly as it did before.

    Unknown intent values fall back to ``"either"``.
    """
    needle = normalize_text(keyword)
    if not needle:
        return 0.5

    norm_title = normalize_text(title)
    norm_author = normalize_text(author)
    title_score, author_score = _field_scores(needle, norm_title, norm_author)
    for token in _query_tokens(keyword):
        token_needle = normalize_text(token)
        if not token_needle:
            continue
        token_title, token_author = _field_scores(token_needle, norm_title, norm_author)
        title_score = max(title_score, token_title)
        author_score = max(author_score, token_author)

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


def match_tier_of(result: SearchResult, keyword: str, *, intent: str = "either") -> int:
    """
    Return the match tier of ``keyword`` against ``result``'s primary field.

    The value is the *kind* of match, on a stable 0..4 scale (larger is
    better):

    =====  =========================================================
    ``4``  exact equality with the normalized field value
    ``3``  the field starts with the keyword (prefix match)
    ``2``  the keyword occurs inside the field (containment match)
    ``1``  only some characters of the keyword occur in the field
    ``0``  no character of the keyword occurs in the field
    =====  =========================================================

    ``intent`` selects which field is primary, mirroring
    :func:`sort_results`:

    * ``"title"`` / ``"author"`` -- the tier of that single field.  This is
      exactly the outer sort key used for ``by="quality"`` under a decided
      intent, so a row's ``match_tier`` explains why it outranked a richer
      row that only collided on the other field;
    * anything else (``"either"``, the default) -- the better of the two
      fields' tiers.

    Multi-token keywords (see :func:`_query_tokens`) are scored token by
    token as well, so ``"斗破苍穹 天蚕土豆"`` reports the best tier any token
    achieves.  The function is pure and never mutates ``result``.  An empty
    keyword yields ``0``.
    """
    resolved = _normalize_intent(intent)
    primary_field = _PRIMARY_FIELD.get(resolved)
    if primary_field is not None:
        return _primary_match_tier(keyword, result, primary_field)
    return max(
        _primary_match_tier(keyword, result, "title"),
        _primary_match_tier(keyword, result, "author"),
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
    # NaN is "unknown", not "worst": every ``<=`` comparison against NaN is
    # false, so without this guard it fell through to the 0.2 bucket.  Mirror
    # ``_priority_key``, which already treats NaN as a missing priority.
    if math.isnan(value):
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


def _tie_break_key(result: SearchResult) -> tuple[str, ...]:
    """
    Return the deterministic pre-sort key used before the primary sort.

    The first three components -- raw ``site``, normalized title, raw
    ``book_id`` -- are the historical tie-break.  Every component after them
    only separates rows that key could not distinguish (two rows of one site
    sharing a title and an empty/equal id), where the old stable sort fell
    back to arrival order and therefore produced a different final ordering
    depending on which site or page finished first.  Appending the remaining
    content fields makes the pre-sort a total order over the row contents.

    Only *content* fields participate: the derived ``rank_score`` /
    ``match_tier`` annotations are deliberately ignored, so a cached row
    carrying stale telemetry can never influence ordering.
    """
    return (
        str(result.get("site") or ""),
        normalize_text(result.get("title")),
        str(result.get("book_id") or ""),
        normalize_text(result.get("author")),
        str(result.get("book_url") or ""),
        str(result.get("word_count") or ""),
        str(result.get("update_date") or ""),
        str(result.get("latest_chapter") or ""),
        str(result.get("cover_url") or ""),
        str(result.get("priority") or ""),
    )


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

    Equal primary keys are resolved by a pre-sort over the row contents
    (:func:`_tie_break_key`), so the result does not depend on the input
    order in which the rows arrived.  Only content fields take part: the
    derived ``rank_score``/``match_tier`` annotations never influence the
    comparison.

    Under ``by="quality"`` with a decided ``intent`` (``"title"``/``"author"``)
    the key is two-level: the match tier of the intent's *primary* field
    first (:func:`_match_tier`), then :func:`rank_score`.  A real primary-field
    hit -- even on a row whose word count, update date and cover are all
    missing -- therefore outranks any row that merely collides on the other
    field, no matter how rich that row's metadata is.  Within one tier the
    previous weighted score still decides.  ``"either"`` and every other
    ``by`` keep the single score/explicit key untouched.

    An unrecognised ``by`` falls back to ``"quality"`` *including* its natural
    direction, so a caller that passes a stale key gets the best rows first
    rather than the worst.
    """
    current = now or datetime.now()
    resolved_intent = _normalize_intent(intent)
    primary_field = _PRIMARY_FIELD.get(resolved_intent)

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
            value = float(result.get("priority", 0))
        except (TypeError, ValueError):
            return 0.0
        # A NaN key makes every comparison false, so the sort's outcome would
        # depend on arrival order (and is not even guaranteed to be a stable
        # permutation of the ties).  Treat it like an unknown priority.
        return 0.0 if math.isnan(value) else value

    def _site_key(result: SearchResult) -> str:
        key = str(result.get("site") or "")
        label = site_labels.get(key, key) if site_labels else key
        return normalize_text(label)

    def _title_key(result: SearchResult) -> str:
        return normalize_text(result.get("title"))

    def _author_key(result: SearchResult) -> str:
        return normalize_text(result.get("author"))

    def _quality_key(result: SearchResult) -> Any:
        score = rank_score(result, keyword, intent=resolved_intent, now=current)
        if primary_field is None:
            # ``"either"``: historical single-score ordering, byte for byte.
            return score
        return (_primary_match_tier(keyword, result, primary_field), score)

    key_fns: dict[str, Callable[[SearchResult], Any]] = {
        "quality": _quality_key,
        "priority": _priority_key,
        "site": _site_key,
        "title": _title_key,
        "author": _author_key,
        "words": _words_key,
        "update": _update_key,
    }
    # An unknown key falls back to the quality ranking.  Resolve the key before
    # the direction: ``default_order`` knows nothing about the substituted key,
    # so using the unknown key's natural direction ("asc") returned the worst
    # rows first.
    resolved_by = by if by in key_fns else "quality"
    key_fn = key_fns[resolved_by]
    direction = order or default_order(resolved_by)
    reverse = direction == "desc"

    # Two stable passes: the first fixes a deterministic tie-break order, the
    # second applies the primary key (and direction) on top of it.  The first
    # pass must be a total order over the row contents (see
    # :func:`_tie_break_key`); otherwise equal primary keys would fall back to
    # arrival order, which differs between concurrent runs.
    results.sort(key=_tie_break_key)
    results.sort(key=key_fn, reverse=reverse)


def filter_by_relevance(
    results: Sequence[SearchResult],
    keyword: str,
    *,
    intent: str = "either",
    min_relevance: float,
) -> list[SearchResult]:
    """
    Return the subset of ``results`` whose relevance clears ``min_relevance``.

    The result set is never emptied:

    * if at least one entry meets the threshold, only those entries are kept;
    * otherwise every entry with any positive relevance (``> 0.0``) is kept;
    * if even that set is empty, the input is returned verbatim.

    The last two rules exist because a strict floor must never turn a page of
    results into an empty one -- a user who typed a keyword would rather see
    poor matches than nothing at all.

    ``intent`` is forwarded to :func:`relevance_of`; an unknown value falls
    back to ``"either"``.  The input sequence is only read: the returned list
    is fresh, so cached snapshots stay pristine.
    """
    scored = [(relevance_of(item, keyword, intent=intent), item) for item in results]
    kept = [item for relevance, item in scored if relevance >= min_relevance]
    if kept:
        return kept
    with_any_match = [item for relevance, item in scored if relevance > 0.0]
    if with_any_match:
        return with_any_match
    return list(results)


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
        results[:] = filter_by_relevance(
            results, keyword, intent=intent, min_relevance=min_relevance
        )

    sort_results(
        results,
        by=by,
        order=order,
        keyword=keyword,
        intent=intent,
        now=current,
    )
