#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.engine
------------------------------------

HTML extraction primitives used by the rule-driven fetcher/parser/searcher.

Two selector dialects are supported for every ``selector`` string:

* XPath -- when it starts with ``/`` or ``(`` (e.g. ``//div[@id="list"]//a``).
* CSS   -- anything else (e.g. ``div#list a``), via ``lxml`` + ``cssselect``.

The extraction rules implemented here are intentionally simple and predictable;
anything beyond them should live in a dedicated site plugin.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from lxml import html as lxml_html

from .model import Field, RuleError

__all__ = [
    "parse_page",
    "strip_nested_html",
    "select",
    "node_to_str",
    "extract",
    "extract_multi",
    "render",
    "abs_url",
]

_NESTED_HTML_RE = re.compile(r"<!DOCTYPE.*?>", re.I | re.S)


def strip_nested_html(s: str) -> str:
    """Remove a nested document wrapper some sites embed inside a page."""
    s = _NESTED_HTML_RE.sub("", s)
    for tag in ("html", "body"):
        s = s.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
    return s


def parse_page(raw: str) -> Any:
    """Parse a raw HTML string into an lxml element tree."""
    if not raw or not raw.strip():
        raise RuleError("empty page cannot be parsed")
    return lxml_html.fromstring(strip_nested_html(raw))


def select(root: Any, selector: str) -> list[Any]:
    """Run an XPath or CSS selector against ``root`` and return matched nodes."""
    if not selector:
        return [root]
    if selector.startswith(("/", "(", "./")):
        return list(root.xpath(selector))
    try:
        return list(root.cssselect(selector))
    except Exception as e:  # cssselect raises various errors on bad input
        raise RuleError(f"invalid selector {selector!r}: {e}") from e


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
        return "".join(
            lxml_html.tostring(child, encoding="unicode") for child in node
        )

    return (node.get(attr) or "").strip()


def _extract_one(node: Any, fld: Field) -> str:
    value = node_to_str(node, fld.attr)
    if fld._pattern is not None:
        m = fld._pattern.search(value)
        if m:
            group = fld.group
            if group and group <= len(m.groups()):
                value = m.group(group)
            else:
                value = m.group(0)
    return fld.apply_replace(value).strip()


def extract(root: Any, fld: Field) -> str:
    """Extract a single string for ``fld`` from ``root`` (or a child node)."""
    nodes = select(root, fld.selector) if fld.selector else [root]
    for node in nodes:
        value = _extract_one(node, fld)
        if value:
            return value
    return fld.apply_replace(fld.default).strip()


def extract_multi(root: Any, fld: Field) -> list[str]:
    """Extract every non-empty match for ``fld`` from ``root``."""
    nodes = select(root, fld.selector) if fld.selector else [root]
    out: list[str] = []
    for node in nodes:
        value = _extract_one(node, fld)
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
