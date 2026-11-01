#!/usr/bin/env python3
"""tests.libs.test_mini_js_utils
-------------------------------

Direct unit tests for :mod:`novel_downloader.libs.mini_js.utils`.

The existing ``test_mini_js.py`` suite drives the interpreter end-to-end, so
several escape-sequence branches of ``unescape_js_string`` and most of the
ECMAScript conversion helpers (``to_int32``/``to_uint32``/``js_truthy``/
``typeof_value``/``js_nullish``) are never exercised directly.
"""

from __future__ import annotations

import math

import pytest

from novel_downloader.libs.mini_js.utils import (
    js_nullish,
    js_truthy,
    to_int32,
    to_uint32,
    typeof_value,
    unescape_js_string,
)

# --------------------------------------------------------------------------- #
# unescape_js_string()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("quoted", "expected"),
    [
        ("'plain'", "plain"),  # no escapes: body returned untouched
        ('"a\\nb"', "a\nb"),
        ('"a\\rb"', "a\rb"),
        ('"a\\tb"', "a\tb"),
        ('"a\\bb"', "a\bb"),
        ('"a\\fb"', "a\fb"),
        ('"a\\vb"', "a\vb"),
        ('"a\\0b"', "a\x00b"),
        ("'it\\'s'", "it's"),
        ('"say \\"hi\\""', 'say "hi"'),
        ("'a\\\\b'", "a\\b"),
        ('"\\x41\\x42"', "AB"),
        ('"\\u0041\\u00e9"', "Aé"),
        ('"\\u{1F600}"', "\U0001f600"),
        ('"\\u{41}"', "A"),
        # Unknown escapes are lenient: the backslash is dropped.
        ("'\\q\\d'", "qd"),
    ],
)
def test_unescape_js_string(quoted: str, expected: str) -> None:
    assert unescape_js_string(quoted) == expected


def test_unescape_js_string_preserves_literal_content() -> None:
    # A body without backslashes is returned verbatim (no re-processing).
    assert unescape_js_string("'100% done'") == "100% done"


def test_unescape_js_string_multiple_escapes_in_one_literal() -> None:
    assert unescape_js_string('"\\t\\u0041\\x2e"') == "\tA."


# --------------------------------------------------------------------------- #
# to_int32() / to_uint32()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (42, 42),
        (2147483647, 2147483647),
        (-1, -1),
        (2147483648, -2147483648),  # wraps to INT32_MIN
        (4294967295, -1),  # 0xFFFFFFFF
        (4294967296, 0),  # wraps modulo 2**32
        (-4294967296, 0),
        (3.9, 3),  # floats are truncated first
        (-3.9, -3),
    ],
)
def test_to_int32(value: float, expected: int) -> None:
    assert to_int32(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (42, 42),
        (-1, 4294967295),
        (-2, 4294967294),
        (4294967296, 0),  # wraps
        (2.9, 2),
    ],
)
def test_to_uint32(value: float, expected: int) -> None:
    assert to_uint32(value) == expected


# --------------------------------------------------------------------------- #
# js_truthy()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        (True, True),
        (False, False),
        (0, False),
        (0.0, False),
        (5, True),
        (-0.5, True),
        (math.nan, False),  # NaN is falsy
        (math.inf, True),
        ("", False),
        ("text", True),
        ([], True),  # JS objects (even empty) are truthy
        ({}, True),
    ],
)
def test_js_truthy(value: object, expected: bool) -> None:
    assert js_truthy(value) is expected


# --------------------------------------------------------------------------- #
# js_nullish() / typeof_value()
# --------------------------------------------------------------------------- #


def test_js_nullish_only_matches_none() -> None:
    assert js_nullish(None) is True
    # 0, "" and False are falsy but not nullish.
    assert js_nullish(0) is False
    assert js_nullish("") is False
    assert js_nullish(False) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "undefined"),
        (True, "boolean"),
        (False, "boolean"),
        (0, "number"),
        (1.5, "number"),
        ("s", "string"),
        ([], "object"),
        ({}, "object"),
    ],
)
def test_typeof_value(value: object, expected: str) -> None:
    assert typeof_value(value) == expected
