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

Every helper is a pure function (no I/O); ``now`` can be injected to keep
the recency signal deterministic in tests.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from novel_downloader.schemas import SearchResult

__all__ = [
    "SORT_KEYS",
    "default_order",
    "normalize_text",
    "parse_update_date",
    "parse_word_count",
    "rank_score",
    "sort_results",
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


def _relevance(keyword: str, title: str, author: str) -> float:
    needle = normalize_text(keyword)
    if not needle:
        return 0.5

    title_score = _match_score(
        needle, normalize_text(title), exact=1.0, prefix=0.75, contains=0.7, base=0.55
    )
    author_score = _match_score(
        needle, normalize_text(author), exact=0.9, prefix=0.7, contains=0.7, base=0.5
    )
    return max(title_score, author_score * 0.9)


def _words_score(words: float | None) -> float:
    if words is None or words <= 0:
        return 0.5
    return min(1.0, math.log10(words) / 7.0)


def _recency_score(updated: datetime | None, now: datetime) -> float:
    if updated is None:
        return 0.5
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
    result: SearchResult, keyword: str = "", *, now: datetime | None = None
) -> float:
    """Return a 0..1 quality score for ``result`` given the search ``keyword``."""
    current = now or datetime.now()
    relevance = _relevance(
        keyword, str(result.get("title") or ""), str(result.get("author") or "")
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
    now: datetime | None = None,
    site_labels: Mapping[str, str] | None = None,
) -> None:
    """
    Sort ``results`` in place.

    ``order`` defaults to :func:`default_order` for the chosen ``by``.
    ``keyword`` feeds the relevance signal used by ``by="quality"``, and
    ``site_labels`` maps a site key to its display label for ``by="site"``.
    """
    direction = order or default_order(by)
    reverse = direction == "desc"
    current = now or datetime.now()

    def _words_key(result: SearchResult) -> float:
        words = parse_word_count(result.get("word_count"))
        return -1.0 if words is None else words

    def _update_key(result: SearchResult) -> float:
        updated = parse_update_date(result.get("update_date"), now=current)
        return -1.0 if updated is None else updated.timestamp()

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
        "quality": lambda r: rank_score(r, keyword, now=current),
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
