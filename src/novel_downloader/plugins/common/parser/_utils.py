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

from lxml import etree, html

from novel_downloader.schemas import ChapterInfoDict

#: Trailing pagination marker such as ``（1/2）`` or ``(1 / 2)``.
_PAGE_MARKER_RE = re.compile(r"\s*[（(]\s*\d+\s*/\s*\d+\s*[)）]\s*$")


def parse_html_tree(raw: str) -> Any | None:
    """
    Parse one page payload into an lxml tree, or ``None`` if it is no document.

    Anti-bot shells consisting of nothing but ``<!DOCTYPE html>``, JSON error
    bodies and XML prologs carrying an encoding declaration all make
    ``lxml.html.fromstring`` raise (``ParserError`` / ``ValueError``).  A
    parser cannot do anything with such a payload, so it must produce the same
    "nothing to parse" answer it already produces for an empty page instead of
    letting the exception escape into the download loop.

    A leading UTF-8 BOM is stripped first: it is invisible in the text but
    makes lxml graft the real document below a synthetic text node, which
    would hide ``og:*`` metadata from the ``//meta`` queries.

    :param raw: Raw HTML payload (already decoded to ``str``).
    :return: Parsed tree, or ``None`` when the payload is not parseable HTML.
    """
    text = (raw or "").lstrip("\ufeff")
    if not text.strip():
        return None
    try:
        return html.fromstring(text)
    except (etree.ParserError, ValueError):
        # ``ParserError``: empty/doctype-only document.
        # ``ValueError``: XML declaration with encoding, NUL bytes, ...
        return None


def _directory_base(base: str) -> str:
    """
    Normalise a page URL used as the base for resolving relative links.

    A base that denotes a document (``.../book/1.html``, ``.../index.php``)
    already resolves relative links against its containing directory, so
    appending ``/`` there would produce ``.../1.html/img/cover.jpg``.  Only
    a base whose last segment names a directory gets the trailing slash.
    """
    if base.endswith("/"):
        return base
    path = base.split("#", 1)[0].split("?", 1)[0]
    if "." in path.rsplit("/", 1)[-1]:
        return path
    return path + "/"


def resolve_url(tree: Any, raw: str, *, fallback_base: str = "") -> str:
    """
    Turn a possibly relative URL from a page into an absolute one.

    Resolution order is:

    1. already absolute (``http://`` / ``https://``) -- returned untouched;
    2. protocol relative (``//host/path``) -- ``https:`` is prepended;
    3. ``fallback_base`` supplied by the parser/site;
    4. the page's own ``og:url`` / canonical link as the base.

    ``fallback_base`` comes first on purpose: it is the origin the fetcher
    builds every request from, so it stays correct when a mirror page carries
    an ``og:url`` pointing at another host.

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
    return urljoin(_directory_base(base), url)


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
