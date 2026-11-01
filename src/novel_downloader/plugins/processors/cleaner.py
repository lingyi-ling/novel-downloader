#!/usr/bin/env python3
"""
novel_downloader.plugins.processors.cleaner
-------------------------------------------

A text cleaner that removes invisible characters, deletes unwanted patterns,
and applies literal replacements for both book-level metadata and chapters.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import unicodedata
from re import Match, Pattern
from typing import Any

from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import BookInfoDict, ChapterDict

logger = logging.getLogger(__name__)

#: Unicode general categories removed by ``remove_format_chars``:
#: ``Cf`` (format characters: zero-width joiners, bidi controls, BOM, ...) and
#: ``Co`` (private-use glyphs, which pirate sites abuse as watermarks).
#: Python's :mod:`re` has no ``\p{Cf}`` support, so this must be done per
#: character through :func:`unicodedata.category`.
_FORMAT_CATEGORIES: frozenset[str] = frozenset({"Cf", "Co"})

#: Characters that may legitimately follow a repeated leading chapter title.
#: A match only counts when the title is followed by whitespace, a colon or the
#: end of the text. Sentence punctuation (``，。！？；、``) is deliberately *not*
#: a boundary: a body that opens with the title text as part of its first
#: sentence ("第一次相遇，他紧张得说不出话。") must keep that sentence. Brackets
#: are excluded for the same reason: ``(上)``/``（续）`` after a title means the
#: heading continues and must not be cut off.
_TITLE_BOUNDARY: frozenset[str] = frozenset(" \t\r\n\u3000：:")

#: Empty paragraph tags (``<p></p>``, ``<p> </p>``, ``<p><br/></p>``, ...).
#: ``[^>]`` is bounded: an unbounded run makes every ``<p`` prefix in a body
#: rescan the whole remainder (a 160 KB string of ``<p`` took ~29 s), so a tag
#: whose attributes exceed the bound is simply left untouched (still safe).
_ATTR_RUN = r"[^>]{0,512}"
_EMPTY_PARAGRAPH_RE: Pattern[str] = re.compile(
    rf"<p\b{_ATTR_RUN}>(?:\s|&nbsp;|&#160;|<br\b{_ATTR_RUN}>)*</p\s*>",
    re.IGNORECASE,
)
#: Self-closing empty paragraph tags (``<p/>``).
_EMPTY_SELF_CLOSING_RE: Pattern[str] = re.compile(
    rf"<p\b{_ATTR_RUN}/\s*>", re.IGNORECASE
)
#: Two or more consecutive line breaks: artifacts left by empty paragraphs.
_BR_RUN_RE: Pattern[str] = re.compile(rf"(?:<br\b{_ATTR_RUN}>\s*){{2,}}", re.IGNORECASE)

#: Characters accepted as Chinese/Arabic chapter numbers.
_CHAPTER_DIGITS = "0-9零〇一二三四五六七八九十百千万两"

#: ``第1章`` / ``第一章`` style heads, optionally already followed by a name.
_CHAPTER_HEAD_RE: Pattern[str] = re.compile(
    rf"(?P<head>第\s*[{_CHAPTER_DIGITS}]+\s*[章节回卷])(?P<rest>.*)"
)
#: ``1. 名字`` / ``12、名字`` / ``一、名字`` style titles. The lookahead rejects
#: decimal numbers such as ``3.14 圆周率`` and version-like strings.
_NUMBER_SEP_RE: Pattern[str] = re.compile(
    rf"(?P<num>[0-9]{{1,4}}|[{_CHAPTER_DIGITS}]+)"
    r"\s*[.．、，,]\s*(?=\D)(?P<name>\S.*)"
)


@registrar.register_processor()
class CleanerProcessor:
    """
    Implements the Processor protocol to clean book and chapter data.
    """

    _INVISIBLE_PATTERN: Pattern[str] = re.compile(r"[\ufeff\u200B\u200C\u200D\u2060]")

    def __init__(self, config: dict[str, Any]) -> None:
        self._remove_invisible: bool = bool(config.get("remove_invisible", True))

        # ``remove_format_chars`` defaults to whatever ``remove_invisible`` is:
        # enabling it is a strict superset of the old behaviour, so existing
        # configs keep their semantics while new installs get the full cleanup.
        format_setting = config.get("remove_format_chars")
        self._remove_format_chars: bool = (
            self._remove_invisible if format_setting is None else bool(format_setting)
        )
        # Off by default: by the time content reaches a processor it is plain
        # text extracted from the page, so ``<p></p>`` is almost always a
        # literal quote (a technical book, a novel about programming) rather
        # than markup. Enable it only for an HTML-preserving parser.
        self._remove_empty_tags: bool = bool(config.get("remove_empty_tags", False))
        self._strip_leading_title: bool = bool(config.get("strip_leading_title", True))
        # Off by default: renaming what the reader sees is a user-visible change.
        self._normalize_chapter_title: bool = bool(
            config.get("normalize_chapter_title", False)
        )

        # --- load JSON files ---
        title_remove_patterns = self._load_str_list(
            str(config.get("title_removes") or "")
        )
        content_remove_patterns = self._load_str_list(
            str(config.get("content_removes") or "")
        )
        title_replacements = self._load_str_dict(str(config.get("title_replace") or ""))
        content_replacements = self._load_str_dict(
            str(config.get("content_replace") or "")
        )

        # dedupe removal patterns (preserve order)
        title_remove = list(dict.fromkeys(title_remove_patterns))
        content_remove = list(dict.fromkeys(content_remove_patterns))

        self._title_repl_map: dict[str, str] = title_replacements
        self._content_repl_map: dict[str, str] = content_replacements

        # build combined regexes (longer first to avoid prefix collisions)
        self._title_combined_rx: Pattern[str] | None = self._compile(
            title_remove + [re.escape(k) for k in self._title_repl_map]
        )
        self._content_combined_rx: Pattern[str] | None = self._compile(
            content_remove + [re.escape(k) for k in self._content_repl_map],
            re.MULTILINE,
        )

    def process_book_info(self, book_info: BookInfoDict) -> BookInfoDict:
        """
        Apply cleaning rules to book metadata and nested structures.
        """
        bi = copy.deepcopy(book_info)

        if isinstance(name := bi.get("book_name"), str):
            bi["book_name"] = self._clean_title(name)
        if isinstance(author := bi.get("author"), str):
            bi["author"] = self._clean_title(author)

        if isinstance(summary := bi.get("summary"), str):
            bi["summary"] = self._clean_content(summary)

        if isinstance(tags := bi.get("tags"), list):
            bi["tags"] = [
                self._clean_title(t) if isinstance(t, str) else t for t in tags
            ]

        if isinstance(volumes := bi.get("volumes"), list):
            for vol in volumes:
                if isinstance(vname := vol.get("volume_name"), str):
                    vol["volume_name"] = self._clean_title(vname)

                if isinstance(intro := vol.get("volume_intro"), str):
                    vol["volume_intro"] = self._clean_content(intro)

                if isinstance(chapters := vol.get("chapters"), list):
                    for cinfo in chapters:
                        if isinstance(ctitle := cinfo.get("title"), str):
                            cinfo["title"] = self._clean_chapter_title(ctitle)

        return bi

    def process_chapter(self, chapter: ChapterDict) -> ChapterDict:
        """
        Apply cleaning rules to a single chapter (title + content).

        The cleaned title is passed to the content cleaner so that a title
        repeated at the start of the body can be stripped (see
        :meth:`strip_leading_title`).
        """
        ch = copy.deepcopy(chapter)

        cleaned_title: str | None = None
        if isinstance(title := ch.get("title"), str):
            cleaned_title = self._clean_chapter_title(title)
            ch["title"] = cleaned_title
        if isinstance(content := ch.get("content"), str):
            ch["content"] = self._clean_content(content, title=cleaned_title)

        return ch

    @classmethod
    def _remove_bom_and_invisible(cls, text: str) -> str:
        return cls._INVISIBLE_PATTERN.sub("", text)

    @staticmethod
    def _strip_format_chars(text: str) -> str:
        """
        Remove Unicode format (``Cf``) and private-use (``Co``) characters.

        This is the Unicode-property-aware counterpart to
        :attr:`_INVISIBLE_PATTERN`: pirate sites sprinkle private-use glyphs
        (U+E000..U+F8FF), bidi controls, zero-width characters and stray BOMs
        through the body as watermarking. :mod:`re` cannot express these
        classes portably, so the check is done per character.
        """
        return "".join(
            ch for ch in text if unicodedata.category(ch) not in _FORMAT_CATEGORIES
        )

    @staticmethod
    def _strip_empty_tags(text: str) -> str:
        """
        Drop empty markup only: ``<p></p>`` and runs of two or more ``<br>``.

        A single line break and any tag with real text inside are preserved.
        """
        text = _EMPTY_PARAGRAPH_RE.sub("", text)
        text = _EMPTY_SELF_CLOSING_RE.sub("", text)
        return _BR_RUN_RE.sub("\n", text)

    @staticmethod
    def strip_leading_title(text: str, title: str) -> str:
        """
        Remove a chapter title repeated at the very start of ``text``.

        Only the beginning is considered (leading whitespace/newlines are
        allowed). The title is matched literally, with all internal whitespace
        removed, and whitespace-insensitively (any run of whitespace in the
        heading may be absent or differently sized in the body). A match
        additionally requires a boundary after the title -- whitespace, a colon
        or the end of the text -- so prose that merely starts with the title is
        left untouched. If the whole body is the repeated title, the text is
        returned unchanged instead of becoming empty.

        :param text: Chapter body.
        :param title: Chapter title (already cleaned is fine).
        :return: ``text`` without the leading duplicated title.
        """
        if not isinstance(text, str) or not isinstance(title, str):
            return text

        base = title.strip()
        if not base:
            return text

        compact = re.sub(r"\s+", "", base)
        # Exact candidates first (longest first so a full heading wins over its
        # compact form), plus a whitespace-flexible pattern: headings are
        # copied with inconsistent spacing (``第1章 相遇`` vs ``第1章相遇``), and
        # requiring the body to use exactly the title's spacing would miss
        # real repeated headings.
        candidates = sorted(dict.fromkeys([base, compact]), key=len, reverse=True)
        parts = base.split()
        ws_rx = (
            re.compile(r"\s*".join(re.escape(part) for part in parts))
            if len(parts) > 1
            else None
        )
        # A very short title ("开始", "序") followed by punctuation is far more
        # likely to be the first word of a sentence than a repeated heading, so
        # it only counts when separated by whitespace or the end of the text.
        short_title = len(compact) < 4

        start = len(text) - len(text.lstrip())
        ends = {
            start + len(candidate)
            for candidate in candidates
            if text.startswith(candidate, start)
        }
        if ws_rx is not None and (match := ws_rx.match(text, start)):
            ends.add(match.end())

        for end in sorted(ends, reverse=True):
            if end < len(text):
                following = text[end]
                if following not in _TITLE_BOUNDARY:
                    continue
                if short_title and not following.isspace():
                    continue
            rest = text[end:].lstrip()
            if rest[:1] in {":", "："}:
                rest = rest[1:].lstrip()
            # Never turn a title-only chapter into an empty one: keeping the
            # repeated heading is strictly better than shipping an empty body.
            if not rest:
                return text
            return rest
        return text

    @staticmethod
    def normalize_chapter_title(text: str) -> str:
        """
        Normalize ``1. 名字`` / ``1、名字`` / ``第1章名字`` to ``第1章 名字``.

        Only unambiguous numeric prefixes are rewritten; already-normalized
        headings are left alone and the function is idempotent.
        """
        if not isinstance(text, str):
            return text

        head = _CHAPTER_HEAD_RE.fullmatch(text)
        if head:
            head_text = head.group("head")
            rest = head.group("rest")
            if not rest or rest[0].isspace() or rest[0] in _TITLE_BOUNDARY:
                return text
            return f"{head_text} {rest}"

        numbered = _NUMBER_SEP_RE.fullmatch(text)
        if not numbered:
            return text
        name = numbered.group("name").strip()
        if not name:
            return text
        return f"第{numbered.group('num')}章 {name}"

    def _clean_title(self, text: str) -> str:
        return self._do_clean(text, self._title_combined_rx, self._title_repl_map)

    def _clean_chapter_title(self, text: str) -> str:
        cleaned = self._clean_title(text)
        if self._normalize_chapter_title:
            cleaned = self.normalize_chapter_title(cleaned)
        return cleaned

    def _clean_content(self, text: str, *, title: str | None = None) -> str:
        return self._do_clean(
            text,
            self._content_combined_rx,
            self._content_repl_map,
            title=title,
        )

    def _do_clean(
        self,
        text: str,
        combined_rx: Pattern[str] | None,
        repl_map: dict[str, str],
        *,
        title: str | None = None,
    ) -> str:
        if not isinstance(text, str):
            return text  # defensive

        strip_title = self._strip_leading_title and bool(title)
        if (
            not self._remove_invisible
            and not self._remove_format_chars
            and not self._remove_empty_tags
            and not combined_rx
            and not strip_title
        ):
            return text.strip()

        if self._remove_invisible:
            text = self._remove_bom_and_invisible(text)

        if self._remove_format_chars:
            text = self._strip_format_chars(text)

        if self._remove_empty_tags:
            text = self._strip_empty_tags(text)

        if combined_rx:

            def _sub(match: Match[str]) -> str:
                return repl_map.get(match.group(0), "")

            text = combined_rx.sub(_sub, text)

        if strip_title and title is not None:
            text = self.strip_leading_title(text, title)

        return text.strip()

    @staticmethod
    def _compile(patterns: list[str], flags: int = 0) -> Pattern[str] | None:
        """
        Compile a single alternation regex out of ``patterns``.

        Patterns are ordered longest-first so that a longer literal is matched
        before any of its prefixes. Returns ``None`` when there is nothing to
        compile, keeping the "no rules configured" fast path.

        Raises:
            ValueError: If one of the user-provided removal patterns is not a
                valid regular expression (surfaced eagerly with a clear message
                instead of a raw :class:`re.error`).
        """
        if not patterns:
            return None
        parts = sorted(patterns, key=len, reverse=True)
        try:
            return re.compile("|".join(parts), flags)
        except re.error as e:
            raise ValueError(
                f"CleanerProcessor: invalid removal regex ({e}). "
                "Check the patterns inside the *_removes JSON files."
            ) from e

    @staticmethod
    def _load_str_list(path: str) -> list[str]:
        """
        Load a JSON file containing a list of strings.

        Returns [] if path is empty/invalid/unreadable (logged as a warning
        rather than silently swallowed). Non-string entries are dropped so that
        a malformed config can never blow up inside :func:`re.compile`.
        """
        if not path:
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # a bad rule file must not abort the stage
            logger.warning(
                "CleanerProcessor: cannot load removal list %r (%s); ignoring.",
                path,
                e,
            )
            return []

        if not isinstance(data, list):
            logger.warning(
                "CleanerProcessor: %r must contain a JSON array, got %s; ignoring.",
                path,
                type(data).__name__,
            )
            return []

        out: list[str] = []
        for item in data:
            if isinstance(item, str):
                out.append(item)
            else:
                logger.warning(
                    "CleanerProcessor: ignoring non-string entry %r in %r.",
                    item,
                    path,
                )
        return out

    @staticmethod
    def _load_str_dict(path: str) -> dict[str, str]:
        """
        Load a JSON file containing a dict of string-to-string mappings.

        Returns {} if path is empty/invalid/unreadable (logged as a warning
        rather than silently swallowed). Non-string values are dropped: a JSON
        ``null`` must not silently turn into the literal text ``"None"``.
        """
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # a bad rule file must not abort the stage
            logger.warning(
                "CleanerProcessor: cannot load replacement map %r (%s); ignoring.",
                path,
                e,
            )
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "CleanerProcessor: %r must contain a JSON object, got %s; ignoring.",
                path,
                type(data).__name__,
            )
            return {}

        out: dict[str, str] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str):
                out[k] = v
            else:
                logger.warning(
                    "CleanerProcessor: ignoring non-string mapping entry %r: %r.",
                    k,
                    v,
                )
        return out
