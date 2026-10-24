"""Boundary and hardening tests for :func:`parse_cookies`."""

from __future__ import annotations

import logging

import pytest

from novel_downloader.infra.cookies import parse_cookies


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ("   \t  ", {}),
        (";;;", {}),
        ("a=1;", {"a": "1"}),
        ("  a = 1 ; b= 2 ; ", {"a": "1", "b": "2"}),
        ("=; a=1; =2", {"a": "1"}),
        ("a=;b=2", {"a": "", "b": "2"}),
        ("a=1; x=1; b=2; x=2", {"a": "1", "b": "2", "x": "2"}),
        ("x=1; invalid; y=2", {"x": "1", "y": "2"}),
        # CR/LF pasted from multi-line values
        ("a=1\r\nb=2", {"a": "1", "b": "2"}),
        ("a=1;\r\n b=2\r\n", {"a": "1", "b": "2"}),
        ("a=1\nb=2\nc=3", {"a": "1", "b": "2", "c": "3"}),
        # mixed separators
        ("a=1; b=2, c=3", {"a": "1", "b": "2", "c": "3"}),
        ("a=1,b=2;c=3\r\nd=4", {"a": "1", "b": "2", "c": "3", "d": "4"}),
        # commas inside a value must survive
        (
            "expires=Wed, 21 Oct 2015 07:28:00 GMT; id=1",
            {"expires": "Wed, 21 Oct 2015 07:28:00 GMT", "id": "1"},
        ),
        ("a=1, 2, b=3", {"a": "1, 2", "b": "3"}),
        # '=' inside values keeps everything after the first separator
        ("sig=abc==; x=1", {"sig": "abc==", "x": "1"}),
        ("a=x=y=z", {"a": "x=y=z"}),
        # whitespace and unicode inside values
        ("a=hello world; b=你好", {"a": "hello world", "b": "你好"}),
        # BOM produced by some editors
        ("\ufeffa=1", {"a": "1"}),
        # NUL / control characters in values are removed
        ("a=x\x00y\tz", {"a": "xyz"}),
        # invalid names are dropped, not silently merged
        ("bad name=1; good=2", {"good": "2"}),
        ("bad\tname=1; good=2", {"good": "2"}),
        ("bad\x00name=1; good=2", {"good": "2"}),
        ("bad,name=1; good=2", {"name": "1", "good": "2"}),
        ("bad;name=1; good=2", {"name": "1", "good": "2"}),
        # a pasted header prefix does not create a broken cookie name
        ("Cookie: a=1", {}),
    ],
)
def test_parse_cookies_boundaries(raw: str, expected: dict[str, str]) -> None:
    assert parse_cookies(raw) == expected


def test_parse_cookies_preserves_very_long_value() -> None:
    value = "a" * 100_000
    assert parse_cookies(f"token={value}") == {"token": value}


def test_parse_cookies_many_pairs() -> None:
    raw = "; ".join(f"k{i}=v{i}" for i in range(1_000))
    parsed = parse_cookies(raw)
    assert len(parsed) == 1_000
    assert parsed["k0"] == "v0"
    assert parsed["k999"] == "v999"


def test_parse_cookies_mapping_is_normalized_and_copied() -> None:
    source = {" A ": " 1 ", "b": "x=y", "bad name": "2", "": "3"}
    result = parse_cookies(source)
    assert result == {"A": "1", "b": "x=y"}
    assert result is not source
    assert source == {" A ": " 1 ", "b": "x=y", "bad name": "2", "": "3"}


def test_parse_cookies_mapping_strips_controls_and_converts_types() -> None:
    result = parse_cookies({123: 456, "x": "a\x00b", "y": True})
    assert result == {"123": "456", "x": "ab", "y": "True"}


@pytest.mark.parametrize("bad_input", [123, 3.14, None, object(), [("a", "b")]])
def test_parse_cookies_invalid_type(bad_input: object) -> None:
    with pytest.raises(TypeError, match="Unsupported cookie format"):
        parse_cookies(bad_input)  # type: ignore[arg-type]


def test_parse_cookies_does_not_log_values(caplog: pytest.LogCaptureFixture) -> None:
    secret = "SUPER-SECRET-SESSION-TOKEN"
    with caplog.at_level(logging.DEBUG, logger="novel_downloader.infra"):
        parse_cookies(f"sessionid={secret}; other=1")
    assert secret not in caplog.text
    assert all(record.name.startswith("novel_downloader") for record in caplog.records)
