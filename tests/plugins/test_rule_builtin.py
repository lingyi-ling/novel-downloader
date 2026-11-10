#!/usr/bin/env python3
"""
Integrity tests for the builtin rule files shipped with the project.

Every ``novel_downloader/plugins/rule/builtin/*.json`` is parsed with the real
loader and checked against the documented schema: required fields present,
types sane, no empty selectors, no unknown keys (typo guard).  No network.
"""

from __future__ import annotations

import codecs
import json
import re
from importlib.resources import files
from typing import Any

import pytest

from novel_downloader.plugins.rule import iter_rules, load_rules
from novel_downloader.plugins.rule.engine import extract, parse_page, select
from novel_downloader.plugins.rule.model import Field, Rule, RuleError, parse_rule

_BUILTIN_PKG = "novel_downloader.plugins.rule.builtin"


def _builtin_names() -> list[str]:
    root = files(_BUILTIN_PKG)
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.name.endswith(".json") and not entry.name.startswith("_")
    )


BUILTIN_NAMES = _builtin_names()

_TOP_LEVEL_KEYS = {
    "site_key",
    "site_name",
    "priority",
    "base_url",
    "encoding",
    "format",
    "hosts",
    "headers",
    "resolve",
    "search",
    "book",
    "chapter",
}
_SEARCH_KEYS = {
    "url",
    "method",
    "data",
    "json_body",
    "headers",
    "format",
    "item",
    "fields",
    "book_id_regex",
    "book_id_group",
    "priority",
}
_BOOK_KEYS = {"url", "fields", "chapters", "pages", "headers", "format"}
_CHAPTER_KEYS = {
    "url",
    "content",
    "title",
    "replace",
    "drop",
    "pages",
    "headers",
    "format",
}
_CHAPTERS_KEYS = {"item", "title", "url", "id_regex", "id_group", "volume_name"}
_PAGES_KEYS = {"url", "start", "max_pages"}
# Keep in sync with ``model._FIELD_KEYS`` (a contract test below fails on
# drift): a shipped source may use every option the loader accepts.
_FIELD_KEYS = {
    "selector",
    "attr",
    "regex",
    "group",
    "multi",
    "replace",
    "default",
    "steps",
    "fallback",
}
_RESOLVE_KEYS = {"book", "chapter"}


# A minimal rule shaped like a shipped source that needs a field-level
# ``fallback``: prefer the cover link, fall back to the title link when the
# cover has no ``href`` (exactly the ``biquge5r`` search layout).
FALLBACK_FIXTURE: dict[str, Any] = {
    "site_key": "fallback_fixture",
    "site_name": "fallback fixture",
    "base_url": "https://example.test",
    "hosts": ["example.test"],
    "search": {
        "url": "https://example.test/search.php?q={keyword}",
        "item": "dl",
        "fields": {
            "title": {"selector": "dd h3 a", "attr": "text"},
            "url": {
                "selector": "dt a",
                "attr": "href",
                "fallback": ["dd h3 a"],
            },
        },
    },
    "chapter": {
        "url": "{base_url}/{book_id}/{chapter_id}.html",
        "content": {"selector": "article"},
    },
}

FALLBACK_SEARCH_HTML = """
<html><body>
  <dl>
    <dt><a href="/1_2/"><img src="/covers/1_2.jpg"></a></dt>
    <dd><h3><a href="/1_2/">第一本</a></h3></dd>
  </dl>
  <dl>
    <dd><h3><a href="/3_4/">第二本</a></h3></dd>
  </dl>
</body></html>
"""


def _read_raw(name: str) -> dict[str, Any]:
    text = files(_BUILTIN_PKG).joinpath(name).read_text(encoding="utf-8-sig")
    data = json.loads(text)
    assert isinstance(data, dict)
    return data


def _load(name: str) -> Rule:
    return parse_rule(_read_raw(name), source=name)


def _assert_known_keys(mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(mapping) - allowed
    assert not unknown, f"{where} has unknown keys (possible typo): {sorted(unknown)}"


def _assert_fallback_chain(chain: Any, where: str) -> None:
    """Validate a ``fallback`` chain: selector strings or independent specs."""
    if chain is None:
        return
    assert isinstance(chain, list), f"{where} must be a list"
    for index, item in enumerate(chain):
        if isinstance(item, dict):
            _assert_known_keys(item, _FIELD_KEYS, f"{where}[{index}]")
            _assert_fallback_chain(item.get("fallback"), f"{where}[{index}].fallback")


def _assert_field_map(mapping: Any, where: str) -> None:
    """Validate a ``{name: field-spec}`` mapping (or bare-string field)."""
    if not isinstance(mapping, dict):
        return
    for name, spec in mapping.items():
        if isinstance(spec, dict):
            _assert_known_keys(spec, _FIELD_KEYS, f"{where}.{name}")
            _assert_fallback_chain(spec.get("fallback"), f"{where}.{name}.fallback")


def test_at_least_one_builtin_rule_is_shipped() -> None:
    assert BUILTIN_NAMES, "expected at least one builtin *.json rule"


def test_builtin_rules_are_registered_by_the_loader() -> None:
    loaded = load_rules()
    builtin_keys = {
        key for key, _, meta in iter_rules() if meta.get("origin") == "builtin"
    }
    assert builtin_keys
    for key in builtin_keys:
        assert key in loaded


@pytest.mark.parametrize("name", BUILTIN_NAMES or ["<none>"])
def test_builtin_rule_parses_and_has_required_fields(name: str) -> None:
    if name == "<none>":
        pytest.skip("no builtin rules present")

    rule = _load(name)

    assert rule.site_key, f"{name}: empty site_key"
    assert rule.base_url.startswith(("http://", "https://")), (
        f"{name}: base_url must be absolute"
    )
    assert rule.hosts, f"{name}: hosts must be non-empty for URL resolution"
    assert rule.book is not None or rule.chapter is not None
    assert isinstance(rule.priority, int)
    # encoding must be a codec Python knows about
    codecs.lookup(rule.encoding)


@pytest.mark.parametrize("name", BUILTIN_NAMES or ["<none>"])
def test_builtin_rule_has_no_unknown_keys(name: str) -> None:
    if name == "<none>":
        pytest.skip("no builtin rules present")

    data = _read_raw(name)
    _assert_known_keys(data, _TOP_LEVEL_KEYS, f"{name}: top level")
    if isinstance(data.get("resolve"), dict):
        _assert_known_keys(data["resolve"], _RESOLVE_KEYS, f"{name}: resolve")
    if isinstance(data.get("search"), dict):
        _assert_known_keys(data["search"], _SEARCH_KEYS, f"{name}: search")
        _assert_field_map(data["search"].get("fields"), f"{name}: search.fields")
    if isinstance(data.get("book"), dict):
        _assert_known_keys(data["book"], _BOOK_KEYS, f"{name}: book")
        _assert_field_map(data["book"].get("fields"), f"{name}: book.fields")
        if isinstance(data["book"].get("chapters"), dict):
            _assert_known_keys(
                data["book"]["chapters"], _CHAPTERS_KEYS, f"{name}: book.chapters"
            )
            _assert_field_map(
                {
                    k: data["book"]["chapters"][k]
                    for k in ("title", "url")
                    if k in data["book"]["chapters"]
                },
                f"{name}: book.chapters",
            )
        if isinstance(data["book"].get("pages"), dict):
            _assert_known_keys(
                data["book"]["pages"], _PAGES_KEYS, f"{name}: book.pages"
            )
    if isinstance(data.get("chapter"), dict):
        _assert_known_keys(data["chapter"], _CHAPTER_KEYS, f"{name}: chapter")
        _assert_field_map(
            {
                k: data["chapter"][k]
                for k in ("content", "title")
                if k in data["chapter"]
            },
            f"{name}: chapter",
        )
        if isinstance(data["chapter"].get("pages"), dict):
            _assert_known_keys(
                data["chapter"]["pages"], _PAGES_KEYS, f"{name}: chapter.pages"
            )


def _iter_fields(rule: Rule) -> list[tuple[str, Field]]:
    found: list[tuple[str, Field]] = []
    if rule.search is not None:
        found.extend((f"search.fields.{k}", v) for k, v in rule.search.fields.items())
    if rule.book is not None:
        found.extend((f"book.fields.{k}", v) for k, v in rule.book.fields.items())
        chapters = rule.book.chapters
        found.append(("book.chapters.title", chapters.title))
        found.append(("book.chapters.url", chapters.url))
    if rule.chapter is not None:
        found.append(("chapter.content", rule.chapter.content))
        if rule.chapter.title is not None:
            found.append(("chapter.title", rule.chapter.title))
    return found


@pytest.mark.parametrize("name", BUILTIN_NAMES or ["<none>"])
def test_builtin_rule_has_no_empty_selectors(name: str) -> None:
    """An empty-string selector is almost always a copy/paste bug.

    ``null`` (no selector) is legal -- it means "operate on the current node".
    """
    if name == "<none>":
        pytest.skip("no builtin rules present")

    rule = _load(name)
    offenders = [where for where, fld in _iter_fields(rule) if fld.selector == ""]
    assert not offenders, f"{name}: fields with an empty selector: {offenders}"


@pytest.mark.parametrize("name", BUILTIN_NAMES or ["<none>"])
def test_builtin_rule_regexes_compile(name: str) -> None:
    if name == "<none>":
        pytest.skip("no builtin rules present")

    data = _read_raw(name)

    def check(pattern: Any, where: str) -> None:
        if pattern in (None, ""):
            return
        assert isinstance(pattern, str), f"{name}: {where} is not a string"
        try:
            re.compile(pattern)
        except re.error as e:  # pragma: no cover - parse_rule already rejects
            pytest.fail(f"{name}: {where} invalid regex {pattern!r}: {e}")

    resolve = data.get("resolve")
    if isinstance(resolve, dict):
        check(resolve.get("book"), "resolve.book")
        check(resolve.get("chapter"), "resolve.chapter")
    search = data.get("search")
    if isinstance(search, dict):
        check(search.get("book_id_regex"), "search.book_id_regex")
    book = data.get("book")
    if isinstance(book, dict) and isinstance(book.get("chapters"), dict):
        check(book["chapters"].get("id_regex"), "book.chapters.id_regex")
    chapter = data.get("chapter")
    if isinstance(chapter, dict):
        for idx, pattern in enumerate(chapter.get("drop") or []):
            check(pattern, f"chapter.drop[{idx}]")


def test_search_sections_declare_a_usable_item() -> None:
    for name in BUILTIN_NAMES:
        rule = _load(name)
        if rule.search is not None:
            assert rule.search.url, f"{name}: search.url is empty"
            assert rule.search.item, f"{name}: search.item is empty"
            assert "url" in rule.search.fields, f"{name}: search needs a 'url' field"


def test_a_builtin_rule_invalid_document_is_rejected() -> None:
    """Guard: rule parsing really does reject malformed builtin-shaped input."""
    with pytest.raises(RuleError):
        parse_rule({"site_key": "broken"}, source="broken.json")


def test_field_key_whitelist_matches_the_model_schema() -> None:
    """The integrity whitelist must accept every key the loader accepts.

    A stale copy of ``_FIELD_KEYS`` rejected a legitimate ``fallback`` in
    ``biquge5r`` and forced an unreadable CSS union; drift is now a failure.
    """
    from novel_downloader.plugins.rule.model import _FIELD_KEYS as MODEL_KEYS

    assert set(MODEL_KEYS) == _FIELD_KEYS, (
        "tests/plugins/test_rule_builtin.py::_FIELD_KEYS drifted from "
        "model._FIELD_KEYS; update the whitelist instead of working around it"
    )


def test_a_field_fallback_is_allowed_and_resolves() -> None:
    """A shipped rule may rely on ``fallback``; the engine must honour it."""
    _assert_known_keys(FALLBACK_FIXTURE, _TOP_LEVEL_KEYS, "fallback_fixture: top level")
    _assert_field_map(
        FALLBACK_FIXTURE["search"]["fields"], "fallback_fixture: search.fields"
    )

    rule = parse_rule(FALLBACK_FIXTURE, source="fallback_fixture.json")
    assert rule.search is not None
    url_field = rule.search.fields["url"]
    assert [alt.selector for alt in url_field.fallback] == ["dd h3 a"]
    # a bare selector string inherits the parent field's ``attr``
    assert url_field.fallback[0].attr == "href"

    tree = parse_page(FALLBACK_SEARCH_HTML)
    rows = select(tree, rule.search.item)
    assert [extract(row, url_field) for row in rows] == ["/1_2/", "/3_4/"]
