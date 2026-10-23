#!/usr/bin/env python3
"""
Validation tests for the rule-driven book source model.

These focus on *malformed* rule documents: every illegal type / missing field /
invalid regex must surface as a :class:`RuleError` at parse time instead of a
raw ``ValueError`` / ``TypeError`` / silent mis-parse.  All checks are offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.rule.engine import parse_page, select
from novel_downloader.plugins.rule.model import Field, RuleError, parse_rule

BASE: dict[str, Any] = {
    "site_key": "demo",
    "base_url": "https://demo.example",
    "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
}


def rule(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid rule with ``overrides`` merged in."""
    return {**BASE, **overrides}


# --------------------------------------------------------------------------- #
# missing required fields
# --------------------------------------------------------------------------- #
def test_missing_site_key_is_reported() -> None:
    with pytest.raises(RuleError, match="missing 'site_key'"):
        parse_rule({"chapter": {"url": "u"}})


def test_search_book_chapter_all_require_a_url() -> None:
    with pytest.raises(RuleError, match="search section requires a 'url'"):
        parse_rule(rule(search={"item": "dl"}))
    with pytest.raises(RuleError, match="book section requires a 'url'"):
        parse_rule(rule(book={"chapters": {"item": "a"}}))
    with pytest.raises(RuleError, match="chapter section requires a 'url'"):
        parse_rule({"site_key": "demo", "chapter": {"title": "h1"}})


def test_book_chapters_require_an_item_selector() -> None:
    with pytest.raises(RuleError, match="requires an 'item' selector"):
        parse_rule(rule(book={"url": "{base_url}/{book_id}/"}))


def test_pages_and_json_body_require_the_right_shape() -> None:
    with pytest.raises(RuleError, match="pages rule requires a 'url'"):
        parse_rule(
            rule(book={"url": "u", "pages": {"start": 2}, "chapters": {"item": "a"}})
        )
    with pytest.raises(RuleError, match="json_body must be an object"):
        parse_rule(rule(search={"url": "u", "json_body": ["kw"]}))


# --------------------------------------------------------------------------- #
# illegal types on integer fields -> RuleError, never ValueError/TypeError
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"priority": "high"}, "priority must be an integer"),
        ({"priority": [1]}, "priority must be an integer"),
        ({"priority": True}, "priority must be an integer"),
        (
            {"search": {"url": "u", "priority": "x"}},
            "search.priority must be an integer",
        ),
        (
            {"book": {"url": "u", "chapters": {"item": "a", "id_group": "x"}}},
            "book.chapters.id_group must be an integer",
        ),
        (
            {
                "book": {
                    "url": "u",
                    "pages": {"url": "p", "start": "x"},
                    "chapters": {"item": "a"},
                }
            },
            "pages.start must be an integer",
        ),
        (
            {
                "book": {
                    "url": "u",
                    "pages": {"url": "p", "max_pages": "x"},
                    "chapters": {"item": "a"},
                }
            },
            "pages.max_pages must be an integer",
        ),
    ],
)
def test_non_integer_numeric_fields_raise_rule_error(
    override: dict[str, Any], match: str
) -> None:
    with pytest.raises(RuleError, match=match):
        parse_rule(rule(**override))


def test_field_group_must_be_an_integer() -> None:
    with pytest.raises(RuleError, match="'group' must be an integer"):
        Field.from_dict({"selector": "a", "regex": r"(\d+)", "group": "one"})


def test_valid_integer_strings_are_accepted() -> None:
    parsed = parse_rule(rule(priority="12", search={"url": "u", "priority": "7"}))
    assert parsed.priority == 12
    assert parsed.search is not None
    assert parsed.search.priority == 7


# --------------------------------------------------------------------------- #
# illegal regexes -> RuleError at parse time
# --------------------------------------------------------------------------- #
def test_field_regex_must_be_a_string() -> None:
    with pytest.raises(RuleError, match="invalid field regex"):
        Field.from_dict({"selector": "a", "regex": 123})
    with pytest.raises(RuleError, match="invalid field regex"):
        Field.from_dict({"selector": "a", "regex": ["("]})


@pytest.mark.parametrize(
    ("override", "match"),
    [
        (
            {"book": {"url": "u", "chapters": {"item": "a", "id_regex": "([unclosed"}}},
            "book.chapters.id_regex: invalid regex",
        ),
        (
            {"search": {"url": "u", "book_id_regex": "([unclosed"}},
            "search.book_id_regex: invalid regex",
        ),
        (
            {"chapter": {"url": "u", "drop": ["([unclosed"]}},
            "chapter.drop: invalid regex",
        ),
        ({"resolve": {"book": "([unclosed"}}, "resolve.book: invalid regex"),
        ({"resolve": {"chapter": "([unclosed"}}, "resolve.chapter: invalid regex"),
    ],
)
def test_invalid_section_regex_is_rejected(
    override: dict[str, Any], match: str
) -> None:
    with pytest.raises(RuleError, match=match):
        parse_rule(rule(**override))


def test_regex_fields_must_be_strings_not_other_types() -> None:
    with pytest.raises(
        RuleError, match="book.chapters.id_regex must be a regex string"
    ):
        parse_rule(rule(book={"url": "u", "chapters": {"item": "a", "id_regex": 5}}))
    with pytest.raises(RuleError, match="resolve.book must be a regex string"):
        parse_rule(rule(resolve={"book": ["("]}))


# --------------------------------------------------------------------------- #
# illegal container / scalar types -> no silent character-splitting
# --------------------------------------------------------------------------- #
def test_hosts_must_be_a_list_not_a_string() -> None:
    # A bare string used to be silently iterated into one entry per character.
    with pytest.raises(RuleError, match="hosts must be a list"):
        parse_rule(rule(hosts="demo.example"))
    with pytest.raises(RuleError, match="hosts must be a list"):
        parse_rule(rule(hosts={"a": 1}))


def test_drop_must_be_a_list_not_a_string() -> None:
    with pytest.raises(RuleError, match="chapter.drop must be a list"):
        parse_rule(rule(chapter={"url": "u", "drop": r"^第\d+页$"}))


def test_resolve_must_be_an_object() -> None:
    with pytest.raises(RuleError, match="'resolve' must be an object"):
        parse_rule(rule(resolve=["^/x/"]))


def test_search_data_must_be_an_object() -> None:
    with pytest.raises(RuleError, match="search.data must be an object"):
        parse_rule(rule(search={"url": "u", "data": ["q"]}))


def test_search_method_must_be_a_string() -> None:
    with pytest.raises(RuleError, match="search.method must be a string"):
        parse_rule(rule(search={"url": "u", "method": 1}))


def test_field_selector_and_attr_must_be_strings() -> None:
    with pytest.raises(RuleError, match="'selector' must be a string"):
        Field.from_dict({"selector": ["a"]})
    with pytest.raises(RuleError, match="'attr' must be a string"):
        Field.from_dict({"selector": "a", "attr": ["href"]})


# --------------------------------------------------------------------------- #
# valid documents keep loading (backward compatibility)
# --------------------------------------------------------------------------- #
def test_valid_document_with_every_section_still_parses() -> None:
    parsed = parse_rule(
        rule(
            hosts=["Demo.Example"],
            priority=45,
            search={
                "url": "https://demo.example/s?q={keyword}",
                "item": "div.r",
                "book_id_regex": r"/(\d+)/",
                "fields": {"title": "h3", "url": {"attr": "href"}},
            },
            book={
                "url": "{base_url}/{book_id}/",
                "pages": "{base_url}/{book_id}/index_{page}.html",
                "fields": {"book_name": {"selector": "h1"}},
                "chapters": {"item": "a", "id_regex": r"/(\d+)\.html"},
            },
            chapter={
                "url": "{base_url}/{book_id}/{chapter_id}.html",
                "drop": [r"^第\(\d+/\d+\)页$"],
            },
            resolve={"book": r"^/(\d+)/$", "chapter": r"^/(\d+)/(\d+)\.html$"},
        )
    )
    assert parsed.hosts == ["demo.example"]
    assert parsed.priority == 45
    assert parsed.book is not None
    assert parsed.book.chapters.id_regex == r"/(\d+)\.html"
    assert parsed.chapter is not None
    assert parsed.chapter.drop == [r"^第\(\d+/\d+\)页$"]


# --------------------------------------------------------------------------- #
# engine selector errors
# --------------------------------------------------------------------------- #
def test_invalid_xpath_selector_raises_rule_error() -> None:
    tree = parse_page("<div><a href='/x'>t</a></div>")
    with pytest.raises(RuleError, match="invalid selector"):
        select(tree, "//div[")


def test_invalid_css_selector_raises_rule_error() -> None:
    tree = parse_page("<div><a href='/x'>t</a></div>")
    with pytest.raises(RuleError, match="invalid selector"):
        select(tree, "div[[")
