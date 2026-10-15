#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.engine
------------------------------------

Extraction primitives used by the rule-driven fetcher/parser/searcher.

Two selector dialects are supported for every ``selector`` string:

* XPath -- when it starts with ``/`` or ``(`` (e.g. ``//div[@id="list"]//a``).
* CSS   -- anything else (e.g. ``div#list a``), via ``lxml`` + ``cssselect``.

When a section declares ``"format": "json"`` the same ``selector`` strings are
instead read as dot paths over the decoded JSON document (see
:func:`select_json`); ``items[*].name`` selects a key of every element of
``items``, while a trailing key that resolves to an array is expanded into its
elements.

The extraction rules implemented here are intentionally simple and predictable;
anything beyond them should live in a dedicated site plugin.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from lxml import etree
from lxml import html as lxml_html

from .model import Field, RuleError

__all__ = [
    "parse_page",
    "parse_json",
    "strip_nested_html",
    "select",
    "select_json",
    "node_to_str",
    "json_to_str",
    "extract",
    "extract_multi",
    "render",
    "abs_url",
]

_NESTED_HTML_RE = re.compile(r"<!DOCTYPE.*?>", re.I | re.S)

# a single dot-path segment: ``key``, ``key[2]``, ``key[*]`` or ``[0]``
_SEGMENT_RE = re.compile(r"^([^\[\]]*)(?:\[([^\]]*)\])?$")


def strip_nested_html(s: str) -> str:
    """Remove a nested document wrapper some sites embed inside a page."""
    s = _NESTED_HTML_RE.sub("", s)
    for tag in ("html", "body"):
        s = s.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
    return s


def parse_json(raw: str) -> Any:
    """Decode a JSON response body, raising :class:`RuleError` when unusable."""
    text = (raw or "").strip()
    if not text:
        raise RuleError("empty response cannot be parsed as JSON")
    try:
        return json.loads(text)
    except ValueError as e:
        raise RuleError(f"unparseable JSON response: {e}") from e


def parse_page(raw: str, fmt: str = "html") -> Any:
    """
    Parse a raw response into the document used by the extractors.

    Pages that are empty *after* removing a nested document wrapper (which
    happens on some sites' interstitial/blank responses) are reported as a
    :class:`RuleError` rather than leaking an ``lxml`` parser exception.

    :param fmt: ``"html"`` (default) returns an lxml tree, ``"json"`` returns
        the decoded Python object.
    """
    if fmt == "json":
        return parse_json(raw)

    text = strip_nested_html(raw or "")
    if not text.strip():
        raise RuleError("empty page cannot be parsed")
    try:
        return lxml_html.fromstring(text)
    except etree.ParserError as e:
        raise RuleError(f"unparseable page: {e}") from e


def select(root: Any, selector: str, fmt: str = "html") -> list[Any]:
    """Run a selector against ``root`` and return the matched nodes/values."""
    if fmt == "json":
        return select_json(root, selector)
    if not selector:
        return [root]
    if selector.startswith(("/", "(", "./")):
        return list(root.xpath(selector))
    try:
        return list(root.cssselect(selector))
    except Exception as e:  # cssselect raises various errors on bad input
        raise RuleError(f"invalid selector {selector!r}: {e}") from e


def _path_segments(path: str) -> list[tuple[str, str | None]]:
    """Split ``$.a.b[*].c`` into ``[(key, index-or-None), ...]``."""
    body = path.strip()
    if body.startswith("$"):
        body = body[1:]
    body = body.lstrip(".")
    if not body:
        return []
    segments: list[tuple[str, str | None]] = []
    for piece in body.split("."):
        match = _SEGMENT_RE.match(piece)
        if match is None:  # pragma: no cover - regex is total over dot-pieces
            segments.append((piece, None))
        else:
            segments.append((match.group(1), match.group(2)))
    return segments


def _apply_index(values: list[Any], index: str) -> list[Any]:
    out: list[Any] = []
    if index == "*":
        for value in values:
            if isinstance(value, (list, tuple)):
                out.extend(value)
            elif isinstance(value, dict):
                out.extend(value.values())
        return out
    try:
        wanted = int(index)
    except ValueError:
        return []
    for value in values:
        if isinstance(value, (list, tuple)) and -len(value) <= wanted < len(value):
            out.append(value[wanted])
    return out


def _walk(values: list[Any], key: str, index: str | None) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if not key:
            out.append(value)
        elif isinstance(value, dict):
            if key in value:
                out.append(value[key])
        elif isinstance(value, (list, tuple)):
            # implicit fan-out: ``items.name`` means "name of every element"
            out.extend(
                elem[key] for elem in value if isinstance(elem, dict) and key in elem
            )
    return _apply_index(out, index) if index is not None else out


def select_json(root: Any, path: str) -> list[Any]:
    """
    Resolve a dot path against a decoded JSON document.

    ``key`` descends into an object, ``[n]`` picks an array element, ``[*]``
    expands every element (or every value of an object) and ``a.b`` fans out
    over arrays of objects.  Unless the final segment indexes explicitly, an
    array result is expanded into its elements so a ``item`` selector yields
    the collection to iterate over.
    """
    segments = _path_segments(path)
    if not segments:
        return [root]

    values: list[Any] = [root]
    for key, index in segments:
        values = _walk(values, key, index)

    if segments[-1][1] is not None:
        return values

    expanded: list[Any] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            expanded.extend(value)
        else:
            expanded.append(value)
    return expanded


def node_to_str(node: Any, attr: str) -> str:
    """Convert a matched node into a string according to ``attr``."""
    if isinstance(node, str):
        # xpath returned a bare string (e.g. ".../@href" or ".../text()")
        return node.strip()

    if attr == "text":
        return "".join(t.strip() for t in node.itertext() if t and t.strip())

    if attr == "inner_text":
        lines = [t.strip() for t in node.itertext() if t and t.strip()]
        return "\n".join(lines)

    if attr == "html":
        return "".join(lxml_html.tostring(child, encoding="unicode") for child in node)

    return (node.get(attr) or "").strip()


def json_to_str(value: Any, attr: str) -> str:
    """
    Convert a decoded JSON value into a string according to ``attr``.

    ``text`` / ``inner_text`` mean "the value itself" (numbers and booleans are
    stringified, arrays are joined element-wise); any other ``attr`` is looked
    up as a key of the matched object.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if attr in ("text", "inner_text", "html"):
            return json.dumps(value, ensure_ascii=False)
        found = value.get(attr)
        return json_to_str(found, "text")
    if isinstance(value, (list, tuple)):
        if attr in ("text", "html"):
            return "".join(json_to_str(item, "text") for item in value)
        if attr == "inner_text":
            return "\n".join(json_to_str(item, "text") for item in value)
        return json.dumps(value, ensure_ascii=False)
    return str(value)  # pragma: no cover - defensive


def _node_str(node: Any, attr: str, fmt: str) -> str:
    if fmt == "json":
        return json_to_str(node, attr)
    return node_to_str(node, attr)


def _postprocess(value: str, fld: Field) -> str:
    if fld._pattern is not None:
        m = fld._pattern.search(value)
        if m:
            group = fld.group
            value = m.group(group) if group and group <= len(m.groups()) else m.group(0)
    return fld.apply_replace(value).strip()


def _extract_one(node: Any, fld: Field, fmt: str) -> str:
    return _postprocess(_node_str(node, fld.attr, fmt), fld)


def extract(root: Any, fld: Field, fmt: str = "html") -> str:
    """Extract a single string for ``fld`` from ``root`` (or a child node)."""
    nodes = select(root, fld.selector, fmt) if fld.selector else [root]
    for node in nodes:
        value = _extract_one(node, fld, fmt)
        if value:
            return value
    return fld.apply_replace(fld.default).strip()


def extract_multi(root: Any, fld: Field, fmt: str = "html") -> list[str]:
    """Extract every non-empty match for ``fld`` from ``root``."""
    nodes = select(root, fld.selector, fmt) if fld.selector else [root]
    out: list[str] = []
    for node in nodes:
        value = _extract_one(node, fld, fmt)
        if value:
            out.append(value)
    return out


def render(template: str, **kv: Any) -> str:
    """Substitute ``{name}`` placeholders without touching other braces."""
    out = template
    for key, value in kv.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def abs_url(base: str, url: str) -> str:
    """Resolve ``url`` against ``base`` and normalize protocol-relative URLs."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base, url)
