#!/usr/bin/env python3
"""
novel_downloader.plugins.common.parser._utils
---------------------------------------------

Small, dependency-free helpers shared by the biquge-style common parsers.

The helpers deliberately do not know about any particular site: they take the
raw HTML tree/payload given to a parser and normalise the parts that every
template family represents identically.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from novel_downloader.schemas import ChapterInfoDict

#: Trailing pagination marker such as ``（1/2）`` or ``(1 / 2)``.
_PAGE_MARKER_RE = re.compile(r"\s*[（(]\s*\d+\s*/\s*\d+\s*[)）]\s*$")


def resolve_url(tree: Any, raw: str, *, fallback_base: str = "") -> str:
    """
    Turn a possibly relative URL from a page into an absolute one.

    Resolution order is:

    1. already absolute (``http://`` / ``https://``) -- returned untouched;
    2. protocol relative (``//host/path``) -- ``https:`` is prepended;
    3. the page's own ``og:url`` / canonical link as the base;
    4. ``fallback_base`` supplied by the parser/site.

    When no base can be determined the original value is returned unchanged
    instead of producing a malformed URL such as ``https:/images/cover.jpg``.
    """
    url = (raw or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url

    base = (fallback_base or "").strip()
    if not base:
        values: list[str] = tree.xpath('//meta[@property="og:url"]/@content')
        if not values or not str(values[0]).strip():
            values = tree.xpath('//link[@rel="canonical"]/@href')
        if values:
            base = str(values[0]).strip()

    if base.startswith("//"):
        base = "https:" + base
    if not base:
        return url
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, url)


def chapter_id_from_href(href: str) -> str:
    """Derive the chapter id from a chapter URL (last path segment, no suffix)."""
    path = (href or "").strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1].split(".", 1)[0]


def strip_page_marker(title: str) -> str:
    """Remove a trailing ``（n/m）`` / ``(n / m)`` pagination marker."""
    return _PAGE_MARKER_RE.sub("", title or "").strip()


def strip_label(value: str, label: str) -> str:
    """
    Remove a leading ``label：`` prefix, tolerating padding inside the label.

    Templates commonly write ``作&nbsp;&nbsp;&nbsp;&nbsp;者：猫腻``; after
    entity decoding the padding is a mixture of NBSP/ideographic spaces, so a
    literal ``"作者："`` replacement is not enough.
    """
    cleaned = (value or "").replace("\xa0", " ").replace("\u3000", " ")
    label_pattern = r"\s*".join(re.escape(char) for char in label)
    cleaned = re.sub(rf"^\s*{label_pattern}\s*[：:]\s*", "", cleaned)
    return cleaned.strip()


def dedupe_chapters(chapters: list[ChapterInfoDict]) -> list[ChapterInfoDict]:
    """
    Drop repeated chapter entries while preserving their original order.

    Catalogue pages (especially 顶点-style templates) repeat the latest
    chapters in both the "最新章节" and "目录章节" blocks.  Downloaders key
    chapters by id, so the first occurrence is the canonical one.
    """
    seen: set[str] = set()
    result: list[ChapterInfoDict] = []
    for chapter in chapters:
        key = chapter["chapterId"] or chapter["url"]
        if key:
            if key in seen:
                continue
            seen.add(key)
        result.append(chapter)
    return result
