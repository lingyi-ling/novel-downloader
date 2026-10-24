"""Snapshot tests for :mod:`novel_downloader.infra.http_defaults`.

These are deliberately exact-value tests: the header dictionaries are part of
the network contract (site parsers rely on ``Accept-Language`` and the shared
User-Agent), so any change should be a visible, reviewed diff.
"""

from __future__ import annotations

from novel_downloader.infra import http_defaults

EXPECTED_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

EXPECTED_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
)

EXPECTED_ACCEPT_LANGUAGE = "en,zh;q=0.9,zh-CN;q=0.8"


def test_default_user_agent_snapshot() -> None:
    assert http_defaults.DEFAULT_USER_AGENT == EXPECTED_USER_AGENT


def test_default_headers_snapshot() -> None:
    assert http_defaults.DEFAULT_HEADERS == {
        "User-Agent": EXPECTED_USER_AGENT,
    }


def test_default_accept_snapshot() -> None:
    assert http_defaults.DEFAULT_ACCEPT == EXPECTED_ACCEPT


def test_image_headers_snapshot() -> None:
    assert http_defaults.IMAGE_HEADERS == {
        "Accept": ("image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": EXPECTED_ACCEPT_LANGUAGE,
        "User-Agent": EXPECTED_USER_AGENT,
        "Connection": "keep-alive",
    }


def test_default_user_headers_snapshot() -> None:
    assert http_defaults.DEFAULT_USER_HEADERS == {
        "Accept": EXPECTED_ACCEPT,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": EXPECTED_ACCEPT_LANGUAGE,
        "User-Agent": EXPECTED_USER_AGENT,
        "Connection": "keep-alive",
    }


def test_header_dicts_share_the_user_agent() -> None:
    assert (
        http_defaults.DEFAULT_HEADERS["User-Agent"]
        == http_defaults.IMAGE_HEADERS["User-Agent"]
        == http_defaults.DEFAULT_USER_HEADERS["User-Agent"]
        == http_defaults.DEFAULT_USER_AGENT
    )


def test_header_dicts_are_separate_objects() -> None:
    assert http_defaults.DEFAULT_HEADERS is not http_defaults.IMAGE_HEADERS
    assert http_defaults.IMAGE_HEADERS is not http_defaults.DEFAULT_USER_HEADERS
    assert http_defaults.DEFAULT_USER_HEADERS is not http_defaults.DEFAULT_HEADERS


def test_header_keys_are_explicit() -> None:
    """Fail loudly if a header is silently dropped or added."""
    assert set(http_defaults.DEFAULT_HEADERS) == {"User-Agent"}
    assert set(http_defaults.IMAGE_HEADERS) == {
        "Accept",
        "Accept-Encoding",
        "Accept-Language",
        "User-Agent",
        "Connection",
    }
    assert set(http_defaults.DEFAULT_USER_HEADERS) == {
        "Accept",
        "Accept-Encoding",
        "Accept-Language",
        "User-Agent",
        "Connection",
    }


def test_no_referer_is_sent_by_default() -> None:
    """Site parsers add ``Referer`` themselves; a global value would leak."""
    for headers in (
        http_defaults.DEFAULT_HEADERS,
        http_defaults.IMAGE_HEADERS,
        http_defaults.DEFAULT_USER_HEADERS,
    ):
        assert "Referer" not in headers
