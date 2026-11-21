#!/usr/bin/env python3
"""
tests.plugins.test_rule_html_scan
---------------------------------

Regression tests for the linear-time HTML strippers in the rule engine.

A page with many unterminated ``<!--`` / ``<`` openers used to make the lazy
regex scan quadratic (a 128 KB body of ``<!--`` took tens of seconds). The
implementation now finds each closer with ``str.find``; these tests pin both
the linear timing and the equivalence with the previous regex behaviour.
"""

from __future__ import annotations

import re
import time

import pytest

from novel_downloader.plugins.rule import engine


def _old_strip_html(value: str) -> str:
    """The previous regex implementation, for equivalence checks."""
    return re.sub(r"<[^>]*>", "", re.sub(r"<!--.*?-->", "", value, flags=re.S))


def _old_strip_nested(value: str) -> str:
    return re.sub(r"<!DOCTYPE.*?>", "", value, flags=re.I | re.S)


@pytest.mark.parametrize(
    "value",
    [
        "<p>hello <b>world</b></p>",
        "before<!-- comment -->after",
        "a<!-- multi\nline -->b<p>x</p>",
        "<!DOCTYPE html><html><body>text</body></html>",
        "no markup at all",
        "<p>unclosed tag",
        "<!-- unterminated comment",
        "",
        "x < y > z",
    ],
)
def test_strip_html_matches_the_regex_behaviour(value: str) -> None:
    assert engine._py_strip_html(value) == _old_strip_html(value)


@pytest.mark.parametrize(
    "value",
    [
        "<!DOCTYPE html><html>text</html>",
        "<!doctype HTML><body>x</body>",
        "no doctype",
        "<!DOCTYPE unterminated",
    ],
)
def test_strip_nested_html_matches_the_regex_behaviour(value: str) -> None:
    assert engine.strip_nested_html(value) == _old_strip_nested(value).replace(
        "<html>", ""
    ).replace("</html>", "").replace("<body>", "").replace("</body>", "")


def test_many_unterminated_comment_openers_are_linear() -> None:
    """128 KB of ``<!--`` must not trigger the quadratic retry path."""
    value = "<!--" * 32_768
    started = time.perf_counter()
    assert engine._py_strip_html(value) == value
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"stripping took {elapsed:.2f}s"


def test_many_unterminated_tags_are_linear() -> None:
    value = "<" * 131_072
    started = time.perf_counter()
    assert engine._py_strip_html(value) == value
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"stripping took {elapsed:.2f}s"


def test_many_unterminated_doctype_openers_are_linear() -> None:
    value = "<!DOCTYPE " * 13_000
    started = time.perf_counter()
    assert engine.strip_nested_html(value) == value
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"stripping took {elapsed:.2f}s"


def test_strip_delimited_keeps_text_when_closer_is_missing() -> None:
    assert engine._strip_delimited("a<!--b", re.compile("<!--"), "-->") == "a<!--b"
    assert engine._strip_delimited("a<!--b-->c", re.compile("<!--"), "-->") == "ac"
    assert (
        engine._strip_delimited("a<!--b-->c<!--d-->e", re.compile("<!--"), "-->")
        == "ace"
    )
