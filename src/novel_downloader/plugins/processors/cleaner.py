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
from re import Match, Pattern
from typing import Any

from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import BookInfoDict, ChapterDict

logger = logging.getLogger(__name__)


@registrar.register_processor()
class CleanerProcessor:
    """
    Implements the Processor protocol to clean book and chapter data.
    """

    _INVISIBLE_PATTERN: Pattern[str] = re.compile(r"[\ufeff\u200B\u200C\u200D\u2060]")

    def __init__(self, config: dict[str, Any]) -> None:
        self._remove_invisible: bool = bool(config.get("remove_invisible", True))

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
                            cinfo["title"] = self._clean_title(ctitle)

        return bi

    def process_chapter(self, chapter: ChapterDict) -> ChapterDict:
        """
        Apply cleaning rules to a single chapter (title + content).
        """
        ch = copy.deepcopy(chapter)

        if isinstance(title := ch.get("title"), str):
            ch["title"] = self._clean_title(title)
        if isinstance(content := ch.get("content"), str):
            ch["content"] = self._clean_content(content)

        return ch

    @classmethod
    def _remove_bom_and_invisible(cls, text: str) -> str:
        return cls._INVISIBLE_PATTERN.sub("", text)

    def _clean_title(self, text: str) -> str:
        return self._do_clean(text, self._title_combined_rx, self._title_repl_map)

    def _clean_content(self, text: str) -> str:
        return self._do_clean(text, self._content_combined_rx, self._content_repl_map)

    def _do_clean(
        self,
        text: str,
        combined_rx: Pattern[str] | None,
        repl_map: dict[str, str],
    ) -> str:
        if not isinstance(text, str):
            return text  # defensive

        if not self._remove_invisible and not combined_rx:
            return text.strip()

        if self._remove_invisible:
            text = self._remove_bom_and_invisible(text)

        if combined_rx:

            def _sub(match: Match[str]) -> str:
                return repl_map.get(match.group(0), "")

            text = combined_rx.sub(_sub, text)

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
