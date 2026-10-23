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

Response formats
----------------
The ``search`` / ``book`` / ``chapter`` sections may declare
``"format": "json"`` to consume an AJAX/JSON endpoint instead of HTML.  When a
section leaves it out, the site-wide :attr:`Rule.format` (default ``"html"``)
applies.  In JSON mode a ``selector`` is a dot path (``data.list``,
``items[*].name``) rather than a CSS/XPath expression, and ``attr`` selects a
key of the matched object (``text`` / ``inner_text`` mean "the value itself").

A JSON request body can be sent by adding ``"json_body": {...}`` to the
``search`` section; its string values accept the same ``{keyword}`` /
``{page}`` templates as URLs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from novel_downloader.infra.i18n import t

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


def _as_int(value: Any, default: int, *, where: str) -> int:
    """
    Coerce an optional integer field, raising :class:`RuleError` on bad input.

    Accepts ``None`` (-> ``default``), a real ``int`` or a numeric string.
    Anything else (a float, a list, a non-numeric string, a bool) is rejected
    instead of leaking a raw ``ValueError``/``TypeError`` to the caller.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuleError(f"{where} must be an integer, got {value!r}")
    try:
        return int(value)
    except ValueError as e:
        raise RuleError(f"{where} must be an integer, got {value!r}") from e


def _check_regex(pattern: Any, *, where: str) -> str | None:
    """
    Validate an optional regex string, returning it unchanged.

    A non-string or syntactically invalid pattern is a hard :class:`RuleError`
    so a bad rule is rejected when it is *parsed* rather than crashing later
    inside the fetcher/parser/searcher (where the exception would be opaque).
    """
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise RuleError(f"{where} must be a regex string, got {type(pattern).__name__}")
    try:
        re.compile(pattern)
    except re.error as e:
        raise RuleError(f"{where}: invalid regex {pattern!r}: {e}") from e
    return pattern


def _as_str_list(value: Any, *, where: str) -> list[str]:
    """Normalize an optional list-of-strings field, rejecting other shapes."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise RuleError(f"{where} must be a list")
    return [str(item) for item in value]


_VALID_FORMATS = {"html", "json"}


def _check_format(fmt: str) -> str:
    """Validate a response format name."""
    if fmt not in _VALID_FORMATS:
        raise RuleError(
            t("unsupported format {fmt!r} (expected one of {formats})").format(
                fmt=fmt, formats=sorted(_VALID_FORMATS)
            )
        )
    return fmt


def _section_format(data: Any) -> str | None:
    """Read an optional per-section ``format`` override (``None`` = inherit)."""
    raw = data.get("format") if isinstance(data, dict) else None
    if raw in (None, ""):
        return None
    return _check_format(_as_str(raw).lower())


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
            except (re.error, TypeError) as e:
                # TypeError covers a non-string ``regex`` (e.g. a number/list).
                raise RuleError(
                    t("invalid field regex {regex!r}: {err}").format(
                        regex=self.regex, err=e
                    )
                ) from e

    @classmethod
    def from_dict(cls, data: Any) -> Field:
        if isinstance(data, str):
            return cls(selector=data)
        if not isinstance(data, dict):
            raise RuleError(
                t("field must be a string or object, got {type!r}").format(
                    type=type(data)
                )
            )
        selector = data.get("selector")
        if selector is not None and not isinstance(selector, str):
            raise RuleError(
                f"field 'selector' must be a string, got {type(selector).__name__}"
            )
        raw_attr = data.get("attr", "text")
        if raw_attr is not None and not isinstance(raw_attr, str):
            raise RuleError(
                f"field 'attr' must be a string, got {type(raw_attr).__name__}"
            )
        return cls(
            selector=selector,
            attr=_as_str(raw_attr, "text"),
            regex=data.get("regex"),
            group=_as_int(data.get("group"), 1, where="field 'group'"),
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
    json_body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    format: str | None = None
    item: str = ""
    fields: dict[str, Field] = field(default_factory=dict)
    book_id_regex: str | None = None
    book_id_group: int = 1
    priority: int = 200

    @classmethod
    def from_dict(cls, data: Any) -> SearchRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("search section requires a 'url'"))
        fields = {k: Field.from_dict(v) for k, v in (data.get("fields") or {}).items()}
        json_body = data.get("json_body")
        if json_body is not None and not isinstance(json_body, dict):
            raise RuleError(t("search.json_body must be an object"))
        raw_data = data.get("data")
        if raw_data is not None and not isinstance(raw_data, dict):
            raise RuleError("search.data must be an object")
        method = _as_str(data.get("method"), "GET")
        if data.get("method") is not None and not isinstance(data["method"], str):
            raise RuleError("search.method must be a string")
        return cls(
            url=_as_str(data["url"]),
            method=method.upper(),
            data={str(k): _as_str(v) for k, v in (raw_data or {}).items()},
            json_body=json_body,
            headers=_as_headers(data.get("headers")),
            format=_section_format(data),
            item=_as_str(data.get("item")),
            fields=fields,
            book_id_regex=_check_regex(
                data.get("book_id_regex"), where="search.book_id_regex"
            ),
            book_id_group=_as_int(
                data.get("book_id_group"), 1, where="search.book_id_group"
            ),
            priority=_as_int(data.get("priority"), 200, where="search.priority"),
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
            raise RuleError(t("book.chapters requires an 'item' selector"))
        return cls(
            item=_as_str(data["item"]),
            title=Field.from_dict(data.get("title", {"attr": "text"})),
            url=Field.from_dict(data.get("url", {"attr": "href"})),
            id_regex=_check_regex(data.get("id_regex"), where="book.chapters.id_regex"),
            id_group=_as_int(data.get("id_group"), 1, where="book.chapters.id_group"),
            volume_name=_as_str(data.get("volume_name"), "正文"),
        )


@dataclass
class PagesRule:
    """Extra pagination pages for info / catalog / chapter pages.

    By default pagination stops as soon as the *next* page is no longer
    referenced from the current page's payload.  For endpoints that carry no
    such link (typically JSON APIs) set ``max_pages`` to fetch a fixed range
    instead.
    """

    url: str
    start: int = 2
    max_pages: int | None = None

    @classmethod
    def from_dict(cls, data: Any) -> PagesRule:
        if isinstance(data, str):
            return cls(url=data)
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("pages rule requires a 'url'"))
        raw_max = data.get("max_pages")
        return cls(
            url=_as_str(data["url"]),
            start=_as_int(data.get("start"), 2, where="pages.start"),
            max_pages=(
                None
                if raw_max is None
                else _as_int(raw_max, 2, where="pages.max_pages")
            ),
        )


@dataclass
class BookRule:
    url: str
    fields: dict[str, Field]
    chapters: ChaptersRule
    pages: PagesRule | None = None
    headers: dict[str, str] = field(default_factory=dict)
    format: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> BookRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("book section requires a 'url'"))
        pages = data.get("pages")
        return cls(
            url=_as_str(data["url"]),
            fields={
                k: Field.from_dict(v) for k, v in (data.get("fields") or {}).items()
            },
            chapters=ChaptersRule.from_dict(data.get("chapters") or {}),
            pages=PagesRule.from_dict(pages) if pages else None,
            headers=_as_headers(data.get("headers")),
            format=_section_format(data),
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
    format: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> ChapterRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("chapter section requires a 'url'"))
        pages = data.get("pages")
        drop = [
            _check_regex(p, where="chapter.drop") or ""
            for p in _as_str_list(data.get("drop"), where="chapter.drop")
        ]
        return cls(
            url=_as_str(data["url"]),
            content=Field.from_dict(data.get("content", {"selector": "body"})),
            title=Field.from_dict(data["title"]) if data.get("title") else None,
            replace=_as_replace(data.get("replace")),
            drop=drop,
            pages=PagesRule.from_dict(pages) if pages else None,
            headers=_as_headers(data.get("headers")),
            format=_section_format(data),
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
    format: str = "html"
    hosts: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    resolve: ResolveRule | None = None
    search: SearchRule | None = None
    book: BookRule | None = None
    chapter: ChapterRule | None = None

    @property
    def display_name(self) -> str:
        return self.site_name or self.site_key

    def format_of(self, section: str | None) -> str:
        """Resolve the effective format for a section (``None`` = inherit)."""
        return section or self.format

    @property
    def search_format(self) -> str:
        return self.format_of(self.search.format if self.search else None)

    @property
    def book_format(self) -> str:
        return self.format_of(self.book.format if self.book else None)

    @property
    def chapter_format(self) -> str:
        return self.format_of(self.chapter.format if self.chapter else None)


_VALID_ATTRS = {"text", "inner_text", "html"}


def _validate_fields(fields: dict[str, Field], where: str) -> None:
    for name, fld in fields.items():
        if fld.attr not in _VALID_ATTRS and not fld.attr:
            raise RuleError(
                t("{where}.{name}: empty 'attr'").format(where=where, name=name)
            )


def parse_rule(data: dict[str, Any], *, source: str = "<memory>") -> Rule:
    """Validate a raw rule mapping and return a :class:`Rule`."""
    if not isinstance(data, dict):
        raise RuleError(t("{source}: rule must be a JSON object").format(source=source))

    site_key = _as_str(data.get("site_key"))
    if not site_key:
        raise RuleError(t("{source}: missing 'site_key'").format(source=source))

    resolve_raw = data.get("resolve")
    resolve = None
    if resolve_raw is not None:
        if not isinstance(resolve_raw, dict):
            raise RuleError(f"{source}: 'resolve' must be an object")
        resolve = ResolveRule(
            book=_check_regex(resolve_raw.get("book"), where="resolve.book"),
            chapter=_check_regex(resolve_raw.get("chapter"), where="resolve.chapter"),
        )

    book = BookRule.from_dict(data["book"]) if data.get("book") else None
    chapter = ChapterRule.from_dict(data["chapter"]) if data.get("chapter") else None
    search = SearchRule.from_dict(data["search"]) if data.get("search") else None

    if book is None and chapter is None:
        raise RuleError(
            t("{source}: rule needs at least a 'book' or 'chapter' section").format(
                source=source
            )
        )
    if book is not None:
        _validate_fields(book.fields, "book.fields")
    if search is not None:
        _validate_fields(search.fields, "search.fields")

    return Rule(
        site_key=site_key,
        site_name=_as_str(data.get("site_name")),
        priority=_as_int(data.get("priority"), 200, where="priority"),
        base_url=_as_str(data.get("base_url")),
        encoding=_as_str(data.get("encoding"), "utf-8"),
        format=_check_format(_as_str(data.get("format"), "html").lower()),
        hosts=[h.lower() for h in _as_str_list(data.get("hosts"), where="hosts")],
        headers=_as_headers(data.get("headers")),
        resolve=resolve,
        search=search,
        book=book,
        chapter=chapter,
    )
