#!/usr/bin/env python3
"""
Illegal-input and boundary tests for the rule DSL.

``tests/plugins/test_rule_validation.py`` covers the happy path of the *error
type* (``RuleError`` instead of a raw ``ValueError``); this module is the
adversarial complement:

* every section of the document (missing / unknown / wrongly typed keys);
* duplicate site keys (JSON-level and loader-level precedence);
* URL templates (schemes, malformed IPv6 literals, page-supplied links);
* selectors (syntax errors, empty/whitespace, very long expressions);
* template variables and the deliberately unsupported ``$ref``;
* numeric boundaries on ``limits`` / ``pages`` (0, negative, huge, NaN/inf);
* catastrophic-backtracking samples (ReDoS), with a wall-clock bound;
* determinism of the parse result and of error messages;
* error messages carry the rule location and do not echo secrets.

All checks are offline and never perform a request.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.plugins.rule as rule_pkg
from novel_downloader.plugins.rule.engine import (
    apply_steps,
    http_url,
    is_fetchable_url,
    parse_page,
    render,
    select,
    select_json,
)
from novel_downloader.plugins.rule.model import (
    Field,
    RuleError,
    Step,
    loads_rule_json,
    parse_rule,
    regex_backtracking_risk,
)
from novel_downloader.plugins.rule.parser import RuleParser

BASE_URL = "https://demo.example"

BASE: dict[str, Any] = {
    "site_key": "demo",
    "base_url": BASE_URL,
    "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
}

TREE = parse_page("<div><a href='/x'>t</a></div>")


def rule(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid rule with ``overrides`` merged in."""
    return {**BASE, **overrides}


def _raised(call: Any) -> RuleError:
    """Run ``call``, assert it raises ``RuleError``, and return the error."""
    with pytest.raises(RuleError) as excinfo:
        call()
    return excinfo.value


def _error_message(doc: dict[str, Any], *, source: str = "<memory>") -> str:
    with pytest.raises(RuleError) as excinfo:
        parse_rule(doc, source=source)
    return str(excinfo.value)


# --------------------------------------------------------------------------- #
# document shape: required keys, empty rules, unknown keys
# --------------------------------------------------------------------------- #
def test_empty_document_reports_the_source_and_the_missing_key() -> None:
    message = _error_message({}, source="empty.json")

    assert "empty.json" in message
    assert "site_key" in message


def test_comment_only_document_is_not_a_valid_rule() -> None:
    message = _error_message({"_note": "just a comment"})

    assert "site_key" in message


def test_site_key_without_book_or_chapter_is_rejected() -> None:
    message = _error_message({"site_key": "demo"})

    assert "at least a 'book' or 'chapter'" in message


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        pytest.param(
            {"search": {}},
            "at least a 'book' or 'chapter'",
            id="empty-search-is-absent",
        ),
        pytest.param(
            {"search": {"url": ""}},
            "search section requires a 'url'",
            id="search-empty-url",
        ),
        pytest.param(
            {"book": {"url": "", "chapters": {"item": "a"}}},
            "book section requires a 'url'",
            id="book-empty-url",
        ),
        pytest.param(
            {"chapter": {"url": ""}},
            "chapter section requires a 'url'",
            id="chapter-empty-url",
        ),
        pytest.param(
            {"book": {"url": "u", "chapters": {"url": "u"}}},
            "book.chapters requires an 'item' selector",
            id="chapters-missing-item",
        ),
        pytest.param(
            {"book": {"url": "u", "pages": {"start": 2}, "chapters": {"item": "a"}}},
            "pages rule requires a 'url'",
            id="pages-missing-url",
        ),
    ],
)
def test_required_section_keys_are_enforced(
    section: dict[str, Any], expected: str
) -> None:
    doc = {"site_key": "demo", **section}

    assert expected in _error_message(doc)


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        pytest.param(
            {"site_key": "demo", "prioroty": 1},
            "unknown key 'prioroty'",
            id="top-level-typo",
        ),
        pytest.param(
            {
                "site_key": "demo",
                "search": {"url": "u", "fomrat": "json"},
            },
            "search: unknown key 'fomrat'",
            id="search-typo",
        ),
        pytest.param(
            {
                "site_key": "demo",
                "book": {"url": "u", "chapterss": {"item": "a"}},
            },
            "book: unknown key 'chapterss'",
            id="book-typo",
        ),
        pytest.param(
            {
                "site_key": "demo",
                "book": {"url": "u", "chapters": {"item": "a", "idregex": "x"}},
            },
            "book.chapters: unknown key 'idregex'",
            id="chapters-typo",
        ),
        pytest.param(
            {
                "site_key": "demo",
                "book": {
                    "url": "u",
                    "fields": {"book_nme": "h1"},
                    "chapters": {"item": "a"},
                },
            },
            "book.fields: unknown field 'book_nme'",
            id="book-field-typo",
        ),
        pytest.param(
            {
                "site_key": "demo",
                "search": {"url": "u", "fields": {"tital": "h2"}},
            },
            "search.fields: unknown field 'tital'",
            id="search-field-typo",
        ),
    ],
)
def test_unknown_keys_name_their_location(doc: dict[str, Any], expected: str) -> None:
    assert expected in _error_message(doc)


def test_unknown_field_spec_key_is_rejected() -> None:
    message = _raised(lambda: Field.from_dict({"selectr": "div"}))

    assert "unknown key 'selectr'" in str(message)


# --------------------------------------------------------------------------- #
# wrong types: every bad shape must stay a RuleError
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"priority": True}, "priority must be an integer", id="bool"),
        pytest.param({"priority": 1.5}, "priority must be an integer", id="float"),
        pytest.param({"priority": [1]}, "priority must be an integer", id="list"),
        pytest.param(
            {"hosts": "demo.example"}, "hosts must be a list", id="hosts-string"
        ),
        pytest.param(
            {"headers": "X-Api-Key: t"}, "headers must be an object", id="headers-str"
        ),
        pytest.param(
            {"headers": ["X-Api-Key: t"]},
            "headers must be an object",
            id="headers-list",
        ),
        pytest.param(
            {"search": {"url": "u", "headers": ["a"]}},
            "search.headers must be an object",
            id="search-headers",
        ),
        pytest.param(
            {
                "book": {
                    "url": "u",
                    "headers": "X: y",
                    "chapters": {"item": "a"},
                }
            },
            "book.headers must be an object",
            id="book-headers",
        ),
        pytest.param(
            {"chapter": {"url": "u", "headers": 3}},
            "chapter.headers must be an object",
            id="chapter-headers",
        ),
        pytest.param(
            {"chapter": {"url": "u", "drop": r"^\d+$"}},
            "chapter.drop must be a list",
            id="drop-string",
        ),
        pytest.param(
            {"search": {"url": "u", "data": ["q"]}},
            "search.data must be an object",
            id="data-list",
        ),
        pytest.param(
            {"search": {"url": "u", "json_body": ["q"]}},
            "json_body must be an object",
            id="json-body-list",
        ),
        pytest.param(
            {"chapter": {"url": "u", "content": {"selector": "a", "multi": "yes"}}},
            "must be a boolean",
            id="multi-string",
        ),
        pytest.param(
            {"chapter": {"url": "u", "content": {"selector": "a", "group": 1.5}}},
            "'group' must be an integer",
            id="group-float",
        ),
        pytest.param({"format": "xml"}, "unsupported format", id="format"),
        pytest.param(
            {"encoding": "utf8-bom"}, "not a known text encoding", id="encoding"
        ),
        pytest.param(
            {"book": {"url": 5, "chapters": {"item": "a"}}},
            "book.url must be a URL string",
            id="book-url-int",
        ),
    ],
)
def test_wrong_types_are_rule_errors(overrides: dict[str, Any], expected: str) -> None:
    assert expected in _error_message(rule(**overrides))


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param([], id="list-root"),
        pytest.param("just text", id="string-root"),
        pytest.param(None, id="none-root"),
    ],
)
def test_non_object_root_is_rejected(doc: Any) -> None:
    message = _raised(lambda: parse_rule(doc))

    assert "rule must be a JSON object" in str(message)


@pytest.mark.parametrize(
    ("dumped", "expected"),
    [
        pytest.param(
            '{"site_key": "dup", "site_key": "dup2", "chapter": {"url": "u"}}',
            "duplicate key 'site_key'",
            id="top-level-site-key",
        ),
        pytest.param(
            '{"site_key": "x", "chapter": {"url": "u", "url": "v"}}',
            "duplicate key 'url'",
            id="nested-url",
        ),
        pytest.param(
            '{"site_key": "x", "book": {"url": "u", "chapters": '
            '{"item": "a", "item": "b"}}}',
            "duplicate key 'item'",
            id="nested-item",
        ),
    ],
)
def test_duplicate_json_keys_are_rejected(dumped: str, expected: str) -> None:
    message = _raised(lambda: loads_rule_json(dumped, source="dup.json"))

    assert expected in str(message)


# --------------------------------------------------------------------------- #
# duplicate site keys: loader precedence is deterministic
# --------------------------------------------------------------------------- #
@pytest.fixture()
def clean_rule_registry() -> Iterator[None]:
    """Isolate ``_load_payload`` writes from the shared module-level registry."""
    rule_pkg.reload_rules()
    yield
    rule_pkg.reload_rules()


def _rule_payload(site_key: str, site_name: str) -> str:
    return json.dumps(
        {
            "site_key": site_key,
            "site_name": site_name,
            "chapter": {"url": "u"},
        }
    )


def test_duplicate_builtin_site_key_keeps_the_first(
    clean_rule_registry: None,
) -> None:
    rule_pkg._load_payload(
        _rule_payload("dsl-dup-builtin", "first"), "builtin", "one.json"
    )
    rule_pkg._load_payload(
        _rule_payload("dsl-dup-builtin", "second"), "builtin", "two.json"
    )

    assert rule_pkg._rules["dsl-dup-builtin"].site_name == "first"


def test_later_user_rule_with_the_same_key_wins(clean_rule_registry: None) -> None:
    rule_pkg._load_payload(_rule_payload("dsl-dup-user", "first"), "user", "one.json")
    rule_pkg._load_payload(_rule_payload("dsl-dup-user", "second"), "user", "two.json")

    assert rule_pkg._rules["dsl-dup-user"].site_name == "second"


def test_site_key_casing_collapses_to_one_registry_entry(
    clean_rule_registry: None,
) -> None:
    rule_pkg._load_payload(_rule_payload("DSL-Dup-Case", "first"), "user", "one.json")
    rule_pkg._load_payload(_rule_payload("dsl-dup-case", "second"), "user", "two.json")

    assert rule_pkg._rules["dsl-dup-case"].site_name == "second"
    assert "DSL-Dup-Case" not in rule_pkg._rules


# --------------------------------------------------------------------------- #
# URL templates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sections",
    [
        pytest.param({"chapter": {"url": "file:///etc/passwd"}}, id="chapter-file"),
        pytest.param(
            {"search": {"url": "javascript:alert(1)"}},
            id="search-javascript",
        ),
        pytest.param(
            {"book": {"url": "ftp://host/x", "chapters": {"item": "a"}}},
            id="book-ftp",
        ),
        pytest.param(
            {
                "book": {
                    "url": "u",
                    "chapters": {"item": "a"},
                    "pages": {"url": "ftp://host/x?p={page}"},
                }
            },
            id="pages-ftp",
        ),
        pytest.param(
            {"base_url": "file:///tmp", "chapter": {"url": "u"}},
            id="base-url-file",
        ),
    ],
)
def test_non_http_url_templates_are_rejected(sections: dict[str, Any]) -> None:
    message = _error_message({"site_key": "demo", **sections})

    assert "unsupported URL scheme" in message


@pytest.mark.parametrize(
    ("sections", "location"),
    [
        pytest.param(
            {"chapter": {"url": "http://[::1/x"}}, "chapter.url", id="chapter"
        ),
        pytest.param(
            {"base_url": "https://[::1", "chapter": {"url": "u"}},
            "base_url",
            id="base-url",
        ),
        pytest.param(
            {"chapter": {"url": "http://{host}[bad/x"}},
            "chapter.url",
            id="after-placeholder",
        ),
        pytest.param(
            {"book": {"url": "http://[bad/x", "chapters": {"item": "a"}}},
            "book.url",
            id="book",
        ),
    ],
)
def test_malformed_ipv6_url_templates_are_rule_errors(
    sections: dict[str, Any], location: str
) -> None:
    message = _error_message({"site_key": "demo", **sections})

    assert location in message
    assert "malformed URL template" in message


def test_url_template_with_control_characters_is_rejected() -> None:
    message = _error_message(rule(chapter={"url": "https://x/a\r\nX: y"}))

    assert "must not contain control characters" in message


def test_relative_url_templates_stay_valid() -> None:
    parsed = parse_rule(rule(chapter={"url": "/c/{chapter_id}.html"}))

    assert parsed.chapter is not None
    assert parsed.chapter.url == "/c/{chapter_id}.html"


@pytest.mark.parametrize(
    "link",
    [
        "HTTP://[x/a",
        "http://[::1/x",
        "https://[",
        "//[",
        "http://]x",
    ],
)
def test_page_supplied_malformed_links_are_dropped_not_raised(link: str) -> None:
    # A hostile href used to make ``urlsplit`` raise a bare ValueError inside
    # ``abs_url``/``is_fetchable_url``, aborting the whole chapter parse.
    assert http_url(BASE_URL, link) == ""
    assert is_fetchable_url(link) is False


@pytest.mark.parametrize("link", ["http:[x", "http:"])
def test_http_url_requires_a_host(link: str) -> None:
    assert http_url(BASE_URL, link) == ""


def test_normal_links_still_resolve_after_hardening() -> None:
    assert http_url(BASE_URL, "/c/1.html") == f"{BASE_URL}/c/1.html"
    assert http_url(BASE_URL, "http://other.example/a") == "http://other.example/a"
    assert http_url(BASE_URL, "//cdn.example/a") == "https://cdn.example/a"
    assert http_url(BASE_URL, "javascript:alert(1)") == ""


# --------------------------------------------------------------------------- #
# selectors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "selector",
    [
        pytest.param("//div[", id="xpath-unclosed-predicate"),
        pytest.param("div[[", id="css-unclosed-attribute"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("\t\n", id="blank"),
    ],
)
def test_invalid_selector_syntax_raises_rule_error(selector: str) -> None:
    message = _raised(lambda: select(TREE, selector))

    assert "invalid selector" in str(message)


def test_empty_selector_means_the_current_node() -> None:
    assert select(TREE, "") == [TREE]


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param("a" * 100_000, id="css"),
        pytest.param("//div[" * 20_000, id="xpath"),
        pytest.param("[" * 100_000, id="css-brackets"),
    ],
)
def test_very_long_selectors_are_rejected_quickly(selector: str) -> None:
    started = time.perf_counter()
    message = _raised(lambda: select(TREE, selector))

    assert time.perf_counter() - started < 1.0
    assert "invalid selector" in str(message)
    # The preview is clipped, so one bad selector cannot flood a log line.
    assert len(str(message)) < 500


def test_field_selector_spec_type_is_validated() -> None:
    message = _raised(lambda: Field.from_dict({"selector": 5}))

    assert "'selector' must be a string" in str(message)


def test_json_path_miss_is_empty_not_an_error() -> None:
    assert select_json({"data": [{"name": "a"}]}, "data[*].missing") == []
    assert select_json({"data": [{"name": "a"}]}, "") == [{"data": [{"name": "a"}]}]


# --------------------------------------------------------------------------- #
# template variables and the unsupported ``$ref``
# --------------------------------------------------------------------------- #
def test_unknown_template_variable_is_left_untouched() -> None:
    assert render("{unknown}/x", known="1") == "{unknown}/x"


def test_recursive_template_is_not_rescanned() -> None:
    # Single-pass substitution: a value that looks like a placeholder is never
    # expanded again, so ``{a} -> {a}`` cannot recurse.
    assert render("{a}", a="{a}") == "{a}"
    assert render("{a}", a="{b}", b="boom") == "{b}"


def test_unknown_variable_in_a_url_template_parses_but_stays_literal() -> None:
    # Unknown ``{name}`` placeholders are documented as passed through, so the
    # loader accepts them; rendering is what leaves them visible.
    parsed = parse_rule(rule(chapter={"url": "{base_url}/{book_id}/{unknown}.html"}))

    assert parsed.chapter is not None
    rendered = render(parsed.chapter.url, base_url=BASE_URL, book_id="42")
    assert rendered == f"{BASE_URL}/42/{{unknown}}.html"


@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(
            lambda: parse_rule(rule(**{"$ref": "#/book/fields/book_name"})),
            id="top-level",
        ),
        pytest.param(
            lambda: parse_rule(
                rule(
                    book={
                        "url": "u",
                        "fields": {"book_name": {"$ref": "#/title"}},
                        "chapters": {"item": "a"},
                    }
                )
            ),
            id="field",
        ),
    ],
)
def test_dollar_ref_is_unsupported_and_rejected(builder: Any) -> None:
    # The DSL has no ``$ref``; it must surface as an unknown key instead of
    # being ignored (a silently dangling reference would yield empty fields).
    message = _raised(builder)

    assert "unknown key '$ref'" in str(message)


# --------------------------------------------------------------------------- #
# numeric boundaries: limits / pages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        pytest.param({"concurrency": 0}, "concurrency must be >= 1", id="conc-zero"),
        pytest.param({"concurrency": -3}, "concurrency must be >= 1", id="conc-neg"),
        pytest.param({"concurrency": "0"}, "concurrency must be >= 1", id="conc-str"),
        pytest.param({"max_attempts": 0}, "max_attempts must be >= 1", id="att-zero"),
        pytest.param({"max_attempts": -1}, "max_attempts must be >= 1", id="att-neg"),
        pytest.param(
            {"max_attempts": 1.5}, "max_attempts must be an integer", id="att-float"
        ),
        pytest.param(
            {"min_interval": -0.5},
            "min_interval must be non-negative",
            id="interval-neg",
        ),
        pytest.param(
            {"min_interval": True}, "min_interval must be a number", id="interval-bool"
        ),
        pytest.param(
            {"min_interval": "nan"},
            "min_interval must be a finite number",
            id="interval-nan",
        ),
        pytest.param(
            {"min_interval": "inf"},
            "min_interval must be a finite number",
            id="interval-inf",
        ),
        pytest.param(
            {"min_interval": "1e400"},
            "min_interval must be a finite number",
            id="interval-overflow",
        ),
        pytest.param(
            {"min_interval": 2.0, "max_interval": 1.0},
            "greater than max",
            id="inverted-band",
        ),
        pytest.param(
            {"retry_min_interval": 5.0, "retry_max_interval": 1.0},
            "greater than max",
            id="inverted-retry-band",
        ),
    ],
)
def test_limits_reject_invalid_boundaries(
    limits: dict[str, Any], expected: str
) -> None:
    assert expected in _error_message(rule(limits=limits))


def test_zero_intervals_are_allowed_and_stay_zero() -> None:
    parsed = parse_rule(
        rule(
            limits={
                "min_interval": 0,
                "max_interval": 0,
                "retry_min_interval": 0,
                "retry_max_interval": 0,
            }
        )
    )

    assert parsed.limits is not None
    assert parsed.limits.min_interval == 0.0
    assert parsed.limits.max_interval == 0.0


def test_huge_finite_values_parse_without_crashing() -> None:
    # A huge interval is not rejected (no upper bound is documented); the parse
    # itself must stay finite and not turn into inf.
    parsed = parse_rule(rule(limits={"min_interval": 1e300, "max_attempts": 10**9}))

    assert parsed.limits is not None
    assert parsed.limits.min_interval == 1e300
    assert parsed.limits.max_attempts == 10**9


def test_limits_requires_an_object() -> None:
    message = _error_message(rule(limits=[1]))

    assert "limits must be an object" in message


def test_timeout_is_not_a_dsl_limit_and_is_rejected() -> None:
    # There is no per-rule timeout knob; silently ignoring it would leave the
    # author believing the request is bounded.
    message = _error_message(rule(limits={"timeout": 5}))

    assert "unknown key 'timeout'" in message


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        pytest.param({"url": "p", "max_pages": 51}, "must be <= 50", id="51"),
        pytest.param({"url": "p", "max_pages": 10**9}, "must be <= 50", id="1e9"),
        pytest.param({"url": "p", "max_pages": "51"}, "must be <= 50", id="str-51"),
        pytest.param(
            {"url": "p", "max_pages": 2.5},
            "max_pages must be an integer",
            id="float",
        ),
    ],
)
def test_pages_max_pages_upper_and_type_boundaries(
    pages: dict[str, Any], expected: str
) -> None:
    doc = rule(book={"url": "u", "chapters": {"item": "a"}, "pages": pages})

    assert expected in _error_message(doc)


@pytest.mark.parametrize("max_pages", [0, -1])
def test_pages_non_positive_max_pages_means_no_extra_pages(max_pages: int) -> None:
    # Documented: ``max_pages < start`` fetches no extra page and is not an
    # error.  Pinned here so the boundary does not silently change.
    parsed = parse_rule(
        rule(
            book={
                "url": "u",
                "chapters": {"item": "a"},
                "pages": {"url": "p", "max_pages": max_pages},
            }
        )
    )

    assert parsed.book is not None
    assert parsed.book.pages is not None
    assert parsed.book.pages.max_pages == max_pages


# --------------------------------------------------------------------------- #
# catastrophic backtracking (ReDoS)
# --------------------------------------------------------------------------- #
EVIL_PATTERNS = [
    r"(a+)+$",
    r"(a*)*",
    r"(\d+)+",
    r"(a*b?)+",
    r"((a+))+",
    r"(a|aa)+$",
    r"(?:ab|a)+$",
]

SAFE_PATTERNS = [
    r"^\d+$",
    r"(\d+\.)+",
    r"(?:foo|bar)+",
    r"(?:com|cn)+",
    r"(?P<id>\d+)",
]


@pytest.mark.parametrize("pattern", EVIL_PATTERNS)
def test_evil_regexes_are_rejected_at_parse_time(pattern: str) -> None:
    message = _raised(lambda: Field.from_dict({"selector": "a", "regex": pattern}))

    assert "catastrophic backtracking" in str(message)


@pytest.mark.parametrize("pattern", SAFE_PATTERNS)
def test_real_world_regexes_are_still_accepted(pattern: str) -> None:
    assert regex_backtracking_risk(pattern) is None
    assert Field.from_dict({"selector": "a", "regex": pattern}).regex == pattern


def test_canonical_redos_sample_is_rejected_without_hanging() -> None:
    started = time.perf_counter()
    message = _raised(
        lambda: parse_rule(rule(chapter={"url": "u", "content": {"regex": r"(a+)+$"}}))
    )

    assert time.perf_counter() - started < 1.0
    assert "catastrophic backtracking" in str(message)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: parse_rule(rule(chapter={"url": "u", "drop": [r"(a+)+$"]})),
            id="chapter.drop",
        ),
        pytest.param(
            lambda: parse_rule(
                rule(
                    book={
                        "url": "u",
                        "chapters": {"item": "a"},
                        "pages": {"url": "p", "stop_when": r"(a+)+$"},
                    }
                )
            ),
            id="pages.stop_when",
        ),
        pytest.param(
            lambda: parse_rule(rule(resolve={"book": r"(a+)+$"})),
            id="resolve.book",
        ),
        pytest.param(
            lambda: parse_rule(rule(search={"url": "u", "book_id_regex": r"(a+)+$"})),
            id="search.book_id_regex",
        ),
    ],
)
def test_every_regex_slot_rejects_the_redos_sample(build: Any) -> None:
    assert "catastrophic backtracking" in str(_raised(build))


def test_runtime_regex_helper_rejects_the_redos_sample_quickly() -> None:
    # ``regex_replace`` learns the pattern at run time; it must still refuse
    # instead of pinning a worker on 5000 characters of page text.
    step = Step.from_dict({"lang": "js", "code": r'regex_replace(input, "(a+)+$", "")'})

    started = time.perf_counter()
    message = _raised(lambda: apply_steps("a" * 5000, [step]))

    assert time.perf_counter() - started < 1.0
    assert "catastrophic backtracking" in str(message)


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_same_document_parses_to_the_same_result_every_time() -> None:
    doc = rule(
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
        resolve={"book": r"^/(\d+)/$"},
    )

    first = parse_rule(doc, source="deterministic.json")
    second = parse_rule(doc, source="deterministic.json")

    assert first == second
    assert repr(first) == repr(second)
    assert first.search is not None
    assert first.search.fields["title"].selector == "h3"


def test_error_messages_are_deterministic_regardless_of_key_order() -> None:
    doc_a = {**rule(), "zzz": 1, "aaa": 2}
    doc_b = {**rule(), "aaa": 2, "zzz": 1}

    message_a = _error_message(doc_a)
    message_b = _error_message(doc_b)

    assert message_a == message_b
    # ``_unknown_keys`` sorts, so the first reported typo is stable.
    assert "'aaa'" in message_a


def test_json_decode_and_parse_are_repeatable() -> None:
    dumped = json.dumps(
        {
            "site_key": "demo",
            "chapter": {"url": "u", "drop": [r"^第\d+页$"]},
        }
    )

    assert loads_rule_json(dumped, source="x.json") == loads_rule_json(
        dumped, source="x.json"
    )
    assert parse_rule(loads_rule_json(dumped)) == parse_rule(loads_rule_json(dumped))


# --------------------------------------------------------------------------- #
# error locations and secret redaction
# --------------------------------------------------------------------------- #
def test_nested_field_errors_name_the_field_path() -> None:
    message = _error_message(
        rule(
            book={
                "url": "u",
                "fields": {
                    "tags": {
                        "selector": "a",
                        "steps": [{"lang": "py", "name": "not_a_step"}],
                    }
                },
                "chapters": {"item": "a"},
            }
        )
    )

    assert "book.fields.tags.steps[0]" in message


def test_section_regex_errors_name_the_field_path() -> None:
    message = _error_message(rule(search={"url": "u", "book_id_regex": "([unclosed"}))

    assert "search.book_id_regex" in message


def test_deeply_nested_fallback_names_the_location() -> None:
    spec: Any = {"selector": "a"}
    for _ in range(20):
        spec = {"selector": "a", "fallback": [spec]}

    message = _raised(lambda: Field.from_dict(spec))

    assert "fallback" in str(message)
    assert "[0]" in str(message)


def test_regex_errors_do_not_echo_the_pattern() -> None:
    secret = "s3cr3t-token-value"

    section_message = _error_message(
        rule(search={"url": "u", "book_id_regex": f"([{secret}"})
    )
    field_message = str(
        _raised(lambda: Field.from_dict({"selector": "a", "regex": f"([{secret}"}))
    )

    assert "search.book_id_regex" in section_message
    assert secret not in section_message
    assert secret not in field_message


def test_combined_drop_error_does_not_echo_the_patterns() -> None:
    # Each pattern is valid alone; the joined expression is not (a global flag
    # may only appear at the start), which used to crash the parser.
    secret = "s3cr3t-token-value"
    message = _raised(
        lambda: parse_rule(rule(chapter={"url": "u", "drop": ["foo", f"(?i){secret}"]}))
    )

    assert "chapter.drop" in str(message)
    assert secret not in str(message)


def test_url_errors_do_not_echo_credentials() -> None:
    secret = "s3cr3t-token-value"
    message = _error_message(rule(chapter={"url": f"ftp://user:{secret}@host/x"}))

    assert "unsupported URL scheme" in message
    assert secret not in message


def test_malformed_url_error_does_not_echo_the_url() -> None:
    secret = "s3cr3t-token-value"
    message = _error_message(rule(chapter={"url": f"http://[host-{secret}/x"}))

    assert "malformed URL template" in message
    assert secret not in message


def test_top_level_error_includes_the_source_but_not_the_working_directory() -> None:
    message = _error_message({"chapter": {"url": "u"}}, source="rule.json")

    assert "rule.json" in message
    assert str(Path.cwd()) not in message


def test_parser_drop_lines_guards_a_programmatic_rule() -> None:
    # ``from_dict`` validates the union, but a hand-built ChapterRule is also
    # possible; the parser must still raise RuleError with a location.
    message = _raised(lambda: RuleParser._drop_lines("x", ["foo", "(?i)ad"]))

    assert "chapter.drop" in str(message)
    assert "combined regex" in str(message)


# --------------------------------------------------------------------------- #
# catch-all: hostile documents resolve to RuleError, never a crash
# --------------------------------------------------------------------------- #
def _hostile_documents() -> list[dict[str, Any]]:
    cyclic_body: dict[str, Any] = {}
    cyclic_body["self"] = cyclic_body
    return [
        {},
        {"site_key": "x"},
        {"site_key": "x", "chapter": {"url": "http://[::1/x"}},
        {"site_key": "x", "base_url": "https://[::1", "chapter": {"url": "u"}},
        {"site_key": "x", "chapter": {"url": "u", "drop": ["foo", "(?i)ad"]}},
        {"site_key": "x", "chapter": {"url": "u", "content": {"regex": "(a+)+$"}}},
        {"site_key": "x", "chapter": {"url": "u", "headers": ["a"]}},
        {"site_key": "x", "search": {"url": "u", "json_body": cyclic_body}},
        {"site_key": "x", "chapter": {"url": "u", "content": {"selector": 7}}},
        {
            "site_key": "x",
            "book": {"url": "u", "chapters": {"item": "a", "id_group": -1}},
        },
        {"site_key": "x", "chapter": {"url": "u", "drop": [r"(a|aa)+$"]}},
    ]


@pytest.mark.parametrize(
    "doc", _hostile_documents(), ids=[f"hostile-{i}" for i in range(11)]
)
def test_hostile_documents_only_raise_rule_error(doc: dict[str, Any]) -> None:
    # A raw ValueError/TypeError/RecursionError here would abort a whole rule
    # scan instead of skipping a single bad file.
    with contextlib.suppress(RuleError):
        parse_rule(doc, source="hostile.json")
