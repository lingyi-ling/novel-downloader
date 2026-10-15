#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.model
-----------------------------------

Data model for the rule-driven ("book source") plugin system.

A *rule* is a small JSON document that describes how to search, parse and
download novels from a website.  The idea is deliberately kept close to the
"book source" JSON formats used by other community downloaders so that new
sources can be added by writing configuration instead of Python code.

The public surface is intentionally tiny:

* :class:`Rule` and friends -- plain dataclasses, one per JSON section.
* :func:`parse_rule` -- validate a raw ``dict`` (already ``json.loads``-ed)
  into a :class:`Rule`, raising :class:`RuleError` on malformed input.

Field extraction semantics (see :class:`Field`):

* ``attr`` values are mutually exclusive:
    - ``text``       : whitespace-stripped concatenation of descendant text
    - ``inner_text`` : descendant text lines joined by ``"\\n"``
    - ``html``       : inner HTML of the selected node(s)
    - anything else  : the value of that attribute (``href``, ``src``, ``content``, ...)
* ``regex`` + ``group`` optionally post-process the extracted string.
* ``multi`` returns every match instead of only the first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Field",
    "SearchRule",
    "ChaptersRule",
    "PagesRule",
    "BookRule",
    "ChapterRule",
    "ResolveRule",
    "Rule",
    "RuleError",
    "parse_rule",
]


class RuleError(ValueError):
    """Raised when a rule document is structurally invalid."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_replace(value: Any) -> list[tuple[str, str]]:
    """Normalize a list of ``[old, new]`` pairs."""
    if not value:
        return []
    out: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0]), str(item[1])))
    return out


def _as_headers(value: Any) -> dict[str, str]:
    """Normalize a request header mapping into ``{str: str}``."""
    if not isinstance(value, dict):
        return {}
    return {str(k): _as_str(v) for k, v in value.items()}


# --------------------------------------------------------------------------- #
# field
# --------------------------------------------------------------------------- #
@dataclass
class Field:
    """A single extraction instruction."""

    selector: str | None = None
    attr: str = "text"
    regex: str | None = None
    group: int = 1
    multi: bool = False
    replace: list[tuple[str, str]] = field(default_factory=list)
    default: str = ""

    _pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.regex:
            try:
                self._pattern = re.compile(self.regex, re.S)
            except re.error as e:  # pragma: no cover - defensive
                raise RuleError(f"invalid field regex {self.regex!r}: {e}") from e

    @classmethod
    def from_dict(cls, data: Any) -> Field:
        if isinstance(data, str):
            return cls(selector=data)
        if not isinstance(data, dict):
            raise RuleError(f"field must be a string or object, got {type(data)!r}")
        return cls(
            selector=data.get("selector"),
            attr=_as_str(data.get("attr"), "text"),
            regex=data.get("regex"),
            group=int(data.get("group", 1)),
            multi=bool(data.get("multi", False)),
            replace=_as_replace(data.get("replace")),
            default=_as_str(data.get("default")),
        )

    def apply_replace(self, value: str) -> str:
        for old, new in self.replace:
            value = value.replace(old, new)
        return value


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
@dataclass
class SearchRule:
    url: str
    method: str = "GET"
    data: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    item: str = ""
    fields: dict[str, Field] = field(default_factory=dict)
    book_id_regex: str | None = None
    book_id_group: int = 1
    priority: int = 200

    @classmethod
    def from_dict(cls, data: Any) -> SearchRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError("search section requires a 'url'")
        fields = {k: Field.from_dict(v) for k, v in (data.get("fields") or {}).items()}
        return cls(
            url=_as_str(data["url"]),
            method=_as_str(data.get("method"), "GET").upper(),
            data={str(k): _as_str(v) for k, v in (data.get("data") or {}).items()},
            headers=_as_headers(data.get("headers")),
            item=_as_str(data.get("item")),
            fields=fields,
            book_id_regex=data.get("book_id_regex"),
            book_id_group=int(data.get("book_id_group", 1)),
            priority=int(data.get("priority", 200)),
        )


# --------------------------------------------------------------------------- #
# book (info + catalog)
# --------------------------------------------------------------------------- #
@dataclass
class ChaptersRule:
    item: str
    title: Field
    url: Field
    id_regex: str | None = None
    id_group: int = 1
    volume_name: str = "正文"

    @classmethod
    def from_dict(cls, data: Any) -> ChaptersRule:
        if not isinstance(data, dict) or not data.get("item"):
            raise RuleError("book.chapters requires an 'item' selector")
        return cls(
            item=_as_str(data["item"]),
            title=Field.from_dict(data.get("title", {"attr": "text"})),
            url=Field.from_dict(data.get("url", {"attr": "href"})),
            id_regex=data.get("id_regex"),
            id_group=int(data.get("id_group", 1)),
            volume_name=_as_str(data.get("volume_name"), "正文"),
        )


@dataclass
class PagesRule:
    """Extra pagination pages for info / catalog / chapter pages."""

    url: str
    start: int = 2

    @classmethod
    def from_dict(cls, data: Any) -> PagesRule:
        if isinstance(data, str):
            return cls(url=data)
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError("pages rule requires a 'url'")
        return cls(url=_as_str(data["url"]), start=int(data.get("start", 2)))


@dataclass
class BookRule:
    url: str
    fields: dict[str, Field]
    chapters: ChaptersRule
    pages: PagesRule | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> BookRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError("book section requires a 'url'")
        pages = data.get("pages")
        return cls(
            url=_as_str(data["url"]),
            fields={
                k: Field.from_dict(v) for k, v in (data.get("fields") or {}).items()
            },
            chapters=ChaptersRule.from_dict(data.get("chapters") or {}),
            pages=PagesRule.from_dict(pages) if pages else None,
            headers=_as_headers(data.get("headers")),
        )


# --------------------------------------------------------------------------- #
# chapter (content)
# --------------------------------------------------------------------------- #
@dataclass
class ChapterRule:
    url: str
    content: Field
    title: Field | None = None
    replace: list[tuple[str, str]] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    pages: PagesRule | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> ChapterRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError("chapter section requires a 'url'")
        pages = data.get("pages")
        return cls(
            url=_as_str(data["url"]),
            content=Field.from_dict(data.get("content", {"selector": "body"})),
            title=Field.from_dict(data["title"]) if data.get("title") else None,
            replace=_as_replace(data.get("replace")),
            drop=[str(p) for p in (data.get("drop") or [])],
            pages=PagesRule.from_dict(pages) if pages else None,
            headers=_as_headers(data.get("headers")),
        )


# --------------------------------------------------------------------------- #
# url -> site resolution
# --------------------------------------------------------------------------- #
@dataclass
class ResolveRule:
    book: str | None = None
    chapter: str | None = None


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #
@dataclass
class Rule:
    site_key: str
    site_name: str = ""
    priority: int = 200
    base_url: str = ""
    encoding: str = "utf-8"
    hosts: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    resolve: ResolveRule | None = None
    search: SearchRule | None = None
    book: BookRule | None = None
    chapter: ChapterRule | None = None

    @property
    def display_name(self) -> str:
        return self.site_name or self.site_key


_VALID_ATTRS = {"text", "inner_text", "html"}


def _validate_fields(fields: dict[str, Field], where: str) -> None:
    for name, fld in fields.items():
        if fld.attr not in _VALID_ATTRS and not fld.attr:
            raise RuleError(f"{where}.{name}: empty 'attr'")


def parse_rule(data: dict[str, Any], *, source: str = "<memory>") -> Rule:
    """Validate a raw rule mapping and return a :class:`Rule`."""
    if not isinstance(data, dict):
        raise RuleError(f"{source}: rule must be a JSON object")

    site_key = _as_str(data.get("site_key"))
    if not site_key:
        raise RuleError(f"{source}: missing 'site_key'")

    resolve_raw = data.get("resolve")
    resolve = None
    if isinstance(resolve_raw, dict):
        resolve = ResolveRule(
            book=resolve_raw.get("book"),
            chapter=resolve_raw.get("chapter"),
        )

    book = BookRule.from_dict(data["book"]) if data.get("book") else None
    chapter = ChapterRule.from_dict(data["chapter"]) if data.get("chapter") else None
    search = SearchRule.from_dict(data["search"]) if data.get("search") else None

    if book is None and chapter is None:
        raise RuleError(f"{source}: rule needs at least a 'book' or 'chapter' section")
    if book is not None:
        _validate_fields(book.fields, "book.fields")
    if search is not None:
        _validate_fields(search.fields, "search.fields")

    return Rule(
        site_key=site_key,
        site_name=_as_str(data.get("site_name")),
        priority=int(data.get("priority", 200)),
        base_url=_as_str(data.get("base_url")),
        encoding=_as_str(data.get("encoding"), "utf-8"),
        hosts=[str(h).lower() for h in (data.get("hosts") or [])],
        headers=_as_headers(data.get("headers")),
        resolve=resolve,
        search=search,
        book=book,
        chapter=chapter,
    )
