#!/usr/bin/env python3
"""Validation tests for the rule-driven book source data model."""

from __future__ import annotations

import pytest

from novel_downloader.plugins.rule.model import (
    Field,
    Rule,
    RuleError,
    parse_rule,
)

MINIMAL: dict[str, object] = {
    "site_key": "demo",
    "base_url": "https://demo.example",
    "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
}


# --------------------------------------------------------------------------- #
# parse_rule
# --------------------------------------------------------------------------- #
def test_parse_minimal_rule() -> None:
    rule = parse_rule(MINIMAL)

    assert isinstance(rule, Rule)
    assert rule.site_key == "demo"
    assert rule.display_name == "demo"
    assert rule.base_url == "https://demo.example"
    assert rule.encoding == "utf-8"
    assert rule.priority == 200
    assert rule.book is None
    assert rule.search is None
    assert rule.chapter is not None


def test_display_name_prefers_site_name() -> None:
    rule = parse_rule({**MINIMAL, "site_name": "示例书源"})
    assert rule.display_name == "示例书源"


def test_hosts_are_lowercased() -> None:
    rule = parse_rule({**MINIMAL, "hosts": ["Demo.Example", "www.demo.example"]})
    assert rule.hosts == ["demo.example", "www.demo.example"]


def test_headers_are_stringified() -> None:
    rule = parse_rule({**MINIMAL, "headers": {"Referer": 1}})
    assert rule.headers == {"Referer": "1"}


def test_rule_must_be_a_mapping() -> None:
    with pytest.raises(RuleError, match="must be a JSON object"):
        parse_rule([])  # type: ignore[arg-type]


def test_site_key_is_required() -> None:
    with pytest.raises(RuleError, match="missing 'site_key'"):
        parse_rule({"chapter": {"url": "u"}})


def test_rule_needs_book_or_chapter() -> None:
    with pytest.raises(RuleError, match="at least a 'book' or 'chapter'"):
        parse_rule({"site_key": "demo"})


def test_resolve_section_is_optional() -> None:
    assert parse_rule(MINIMAL).resolve is None

    rule = parse_rule(
        {
            **MINIMAL,
            "resolve": {
                "book": r"^/(\d+)/$",
                "chapter": r"^/(\d+)/(\d+)\.html$",
            },
        }
    )
    assert rule.resolve is not None
    assert rule.resolve.book == r"^/(\d+)/$"
    assert rule.resolve.chapter == r"^/(\d+)/(\d+)\.html$"


# --------------------------------------------------------------------------- #
# Field
# --------------------------------------------------------------------------- #
def test_field_from_string_uses_default_attr() -> None:
    fld = Field.from_dict("div#content")
    assert fld.selector == "div#content"
    assert fld.attr == "text"
    assert fld.regex is None
    assert fld.multi is False
    assert fld.replace == []


def test_field_from_dict_reads_every_option() -> None:
    fld = Field.from_dict(
        {
            "selector": "a",
            "attr": "href",
            "regex": r"/(\d+)\.html",
            "group": 1,
            "multi": True,
            "replace": [["\u00a0", ""], ["广告", ""]],
            "default": "unknown",
        }
    )
    assert fld.attr == "href"
    assert fld.group == 1
    assert fld.multi is True
    assert fld.replace == [("\u00a0", ""), ("广告", "")]
    assert fld.default == "unknown"
    assert fld._pattern is not None


def test_field_rejects_unknown_type() -> None:
    with pytest.raises(RuleError, match="must be a string or object"):
        Field.from_dict(123)


def test_field_rejects_invalid_regex() -> None:
    with pytest.raises(RuleError, match="invalid field regex"):
        Field(regex="([unclosed")


def test_apply_replace_applies_every_pair() -> None:
    fld = Field(replace=[("a", "b"), ("b", "c")])
    assert fld.apply_replace("a") == "c"


def test_replace_pairs_ignore_malformed_entries() -> None:
    fld = Field.from_dict({"selector": "a", "replace": [["x"], "y", ["k", "v"]]})
    assert fld.replace == [("k", "v")]


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #
def test_search_requires_url() -> None:
    with pytest.raises(RuleError, match="requires a 'url'"):
        parse_rule({**MINIMAL, "search": {"item": "dl"}})


def test_search_defaults_and_overrides() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "search": {
                "url": "https://demo.example/search?q={keyword}",
                "method": "post",
                "data": {"q": "{keyword}"},
                "priority": 12,
                "book_id_regex": r"/(\d+)/",
                "fields": {"title": "dd h3 a", "url": {"attr": "href"}},
            },
        }
    )
    assert rule.search is not None
    assert rule.search.method == "POST"
    assert rule.search.data == {"q": "{keyword}"}
    assert rule.search.priority == 12
    assert rule.search.book_id_group == 1
    assert rule.search.fields["title"].selector == "dd h3 a"
    assert rule.search.fields["url"].attr == "href"


def test_book_requires_url_and_chapters_item() -> None:
    with pytest.raises(RuleError, match="book section requires a 'url'"):
        parse_rule({**MINIMAL, "book": {"chapters": {"item": "a"}}})

    with pytest.raises(RuleError, match="requires an 'item' selector"):
        parse_rule({**MINIMAL, "book": {"url": "/book/{book_id}/"}})


def test_chapters_defaults() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "book": {
                "url": "{base_url}/{book_id}/",
                "chapters": {"item": "div.list a"},
            },
        }
    )
    assert rule.book is not None
    chapters = rule.book.chapters
    assert chapters.title.attr == "text"
    assert chapters.url.attr == "href"
    assert chapters.id_group == 1
    assert chapters.volume_name == "正文"
    assert rule.book.pages is None


def test_pages_accepts_bare_string() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "book": {
                "url": "{base_url}/{book_id}/",
                "pages": "{base_url}/{book_id}/index_{page}.html",
                "chapters": {"item": "a"},
            },
        }
    )
    assert rule.book is not None
    assert rule.book.pages is not None
    assert rule.book.pages.start == 2


def test_empty_attr_is_rejected() -> None:
    with pytest.raises(RuleError, match=r"search\.fields\.title: empty 'attr'"):
        parse_rule(
            {
                **MINIMAL,
                "search": {
                    "url": "https://demo.example/s",
                    "fields": {"title": {"selector": "a", "attr": ""}},
                },
            }
        )


def test_custom_attr_names_are_allowed() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "book": {
                "url": "{base_url}/{book_id}/",
                "fields": {"cover_url": {"selector": "img", "attr": "src"}},
                "chapters": {"item": "a"},
            },
        }
    )
    assert rule.book is not None
    assert rule.book.fields["cover_url"].attr == "src"


def test_section_headers_are_parsed() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "book": {
                "url": "{base_url}/{book_id}/",
                "headers": {"Referer": "https://demo.example/"},
                "chapters": {"item": "a"},
            },
        }
    )

    assert rule.book is not None
    assert rule.book.headers == {"Referer": "https://demo.example/"}
    assert rule.chapter is not None
    assert rule.chapter.headers == {}


def test_chapter_defaults() -> None:
    rule = parse_rule(MINIMAL)
    assert rule.chapter is not None
    assert rule.chapter.content.selector == "body"
    assert rule.chapter.title is None
    assert rule.chapter.pages is None
    assert rule.chapter.drop == []


# --------------------------------------------------------------------------- #
# response format
# --------------------------------------------------------------------------- #
def test_format_defaults_to_html() -> None:
    rule = parse_rule(MINIMAL)

    assert rule.format == "html"
    assert rule.search_format == "html"
    assert rule.book_format == "html"
    assert rule.chapter_format == "html"


def test_top_level_format_is_normalized_and_inherited() -> None:
    rule = parse_rule({**MINIMAL, "format": "JSON"})

    assert rule.format == "json"
    assert rule.chapter_format == "json"


def test_invalid_format_is_rejected() -> None:
    with pytest.raises(RuleError, match="unsupported format"):
        parse_rule({**MINIMAL, "format": "xml"})


def test_section_format_overrides_the_top_level() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "format": "json",
            "search": {"url": "https://demo.example/s", "format": "HTML"},
            "book": {
                "url": "{base_url}/{book_id}/",
                "chapters": {"item": "a"},
            },
        }
    )

    assert rule.search_format == "html"
    assert rule.book_format == "json"
    assert rule.chapter_format == "json"


def test_blank_section_format_inherits() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": "https://demo.example",
            "format": "json",
            "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html", "format": ""},
        }
    )

    assert rule.chapter is not None
    assert rule.chapter.format is None
    assert rule.chapter_format == "json"


def test_invalid_section_format_is_rejected() -> None:
    with pytest.raises(RuleError, match="unsupported format"):
        parse_rule(
            {
                **MINIMAL,
                "book": {
                    "url": "{base_url}/{book_id}/",
                    "format": "yaml",
                    "chapters": {"item": "a"},
                },
            }
        )


# --------------------------------------------------------------------------- #
# json_body
# --------------------------------------------------------------------------- #
def test_search_json_body_is_kept_verbatim() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "search": {
                "url": "https://api.demo.example/search",
                "method": "POST",
                "format": "json",
                "json_body": {"kw": "{raw_keyword}", "page": "{page}"},
            },
        }
    )

    assert rule.search is not None
    assert rule.search.json_body == {"kw": "{raw_keyword}", "page": "{page}"}
    assert rule.search.data == {}


def test_search_json_body_must_be_an_object() -> None:
    with pytest.raises(RuleError, match=r"json_body must be an object"):
        parse_rule(
            {
                **MINIMAL,
                "search": {
                    "url": "https://api.demo.example/search",
                    "json_body": ["kw"],
                },
            }
        )


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
def test_pages_max_pages_is_parsed() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "chapter": {
                "url": "{base_url}/{book_id}/{chapter_id}.html",
                "pages": {
                    "url": "https://api.demo.example/{book_id}?page={page}",
                    "start": 3,
                    "max_pages": 20,
                },
            },
        }
    )

    assert rule.chapter is not None
    assert rule.chapter.pages is not None
    assert rule.chapter.pages.start == 3
    assert rule.chapter.pages.max_pages == 20


def test_pages_max_pages_defaults_to_none() -> None:
    rule = parse_rule(
        {
            **MINIMAL,
            "book": {
                "url": "{base_url}/{book_id}/",
                "pages": {"url": "{base_url}/{book_id}/index_{page}.html"},
                "chapters": {"item": "a"},
            },
        }
    )

    assert rule.book is not None
    assert rule.book.pages is not None
    assert rule.book.pages.start == 2
    assert rule.book.pages.max_pages is None
