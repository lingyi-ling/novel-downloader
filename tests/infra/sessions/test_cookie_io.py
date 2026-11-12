"""Cookie persistence: full attribute round-trip and tolerant loading.

The legacy on-disk format only stored ``name``/``value``/``domain``/``path``.
That is enough to replay a Cookie header, but it silently turns a session
cookie into a persistent one and drops ``Secure``/``HttpOnly``/``SameSite``.
These tests pin both the backward-compatible reader and the richer writer.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra.sessions.cookie_io import (
    DEFAULT_SAME_SITE,
    MAX_COOKIE_FILE_BYTES,
    load_cookie_items,
    resolve_cookie_path,
    write_cookie_items,
)

RICH_ITEM: dict[str, Any] = {
    "name": "sid",
    "value": "s3cret",
    "domain": ".example.com",
    "path": "/book",
    "secure": True,
    "http_only": True,
    "same_site": "Strict",
    "expires": 4_102_444_800.0,
    "max_age": 3600,
    "domain_specified": True,
    "domain_initial_dot": True,
    "path_specified": True,
    "rest": {"Priority": "High"},
}


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Legacy files keep loading with the legacy shape
# ---------------------------------------------------------------------------


def test_legacy_file_still_loads_with_the_legacy_shape(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "aiohttp.cookies",
        [
            {"name": "a", "value": "1"},
            {"name": "b", "value": "2", "domain": ".example.com", "path": "/x"},
        ],
    )

    assert load_cookie_items(path) == [
        {"name": "a", "value": "1"},
        {"name": "b", "value": "2", "domain": ".example.com", "path": "/x"},
    ]


def test_legacy_file_is_not_given_a_fake_expiry(tmp_path: Path) -> None:
    """A legacy entry carries no expiry, so it must stay a session cookie."""
    path = _write(tmp_path / "httpx.cookies", [{"name": "sid", "value": "v"}])

    items = load_cookie_items(path)
    assert items is not None
    assert len(items) == 1
    assert "expires" not in items[0]


def test_top_level_document_with_cookies_key_is_accepted(tmp_path: Path) -> None:
    """Be liberal in what we accept: some tools wrap the list in an object."""
    path = _write(
        tmp_path / "cookies.json",
        {
            "version": 1,
            "cookies": [{"name": "sid", "value": "v", "version": 1, "secure": True}],
        },
    )

    items = load_cookie_items(path)
    assert items is not None
    assert items[0]["name"] == "sid"
    assert items[0]["secure"] is True


# ---------------------------------------------------------------------------
# Rich entries
# ---------------------------------------------------------------------------


def test_rich_entry_round_trips_every_security_attribute(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    write_cookie_items(path, [RICH_ITEM])

    loaded = load_cookie_items(path)
    assert loaded is not None
    assert len(loaded) == 1
    item = loaded[0]
    assert item["name"] == "sid"
    assert item["value"] == "s3cret"
    assert item["version"] == 1
    assert item["domain"] == ".example.com"
    assert item["path"] == "/book"
    assert item["secure"] is True
    assert item["http_only"] is True
    assert item["same_site"] == "Strict"
    assert item["expires"] == 4_102_444_800.0
    assert item["max_age"] == 3600
    assert item["domain_specified"] is True
    assert item["domain_initial_dot"] is True
    assert item["path_specified"] is True
    assert item["rest"] == {"Priority": "High"}


def test_writer_always_emits_a_versioned_list_entry(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    write_cookie_items(path, [{"name": "sid", "value": "v"}])

    raw = json.loads(path.read_text(encoding="utf-8"))
    # Still a bare JSON list: CookieStore and older readers keep working.
    assert isinstance(raw, list)
    entry = raw[0]
    assert entry["version"] == 1
    for field in (
        "secure",
        "http_only",
        "same_site",
        "expires",
        "max_age",
        "domain_specified",
        "domain_initial_dot",
        "path_specified",
        "rest",
    ):
        assert field in entry


def test_session_cookie_is_written_without_an_expiry(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    write_cookie_items(path, [{"name": "sid", "value": "v"}])

    loaded = load_cookie_items(path)
    assert loaded is not None
    assert loaded[0]["expires"] is None
    assert loaded[0]["max_age"] is None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expired_cookies_are_dropped_on_load(tmp_path: Path) -> None:
    now = time.time()
    path = _write(
        tmp_path / "cookies.json",
        [
            {"name": "past", "value": "1", "version": 1, "expires": now - 60},
            {"name": "zero", "value": "1", "version": 1, "expires": 0},
            {"name": "future", "value": "1", "version": 1, "expires": now + 600},
        ],
    )

    items = load_cookie_items(path)
    assert items is not None
    assert [item["name"] for item in items] == ["future"]
    assert items[0]["expires"] == pytest.approx(now + 600)


@pytest.mark.parametrize("max_age", [-1, 0, "-5"])
def test_non_positive_max_age_is_treated_as_expired(
    tmp_path: Path, max_age: int | str
) -> None:
    path = _write(
        tmp_path / "cookies.json",
        [{"name": "sid", "value": "v", "version": 1, "max_age": max_age}],
    )
    assert load_cookie_items(path) == []


def test_max_age_without_expires_gets_an_absolute_deadline(tmp_path: Path) -> None:
    now = time.time()
    path = _write(
        tmp_path / "cookies.json",
        [{"name": "sid", "value": "v", "version": 1, "max_age": 3600}],
    )

    items = load_cookie_items(path)
    assert items is not None
    assert items[0]["max_age"] == 3600
    assert items[0]["expires"] == pytest.approx(now + 3600, abs=5)


def test_invalid_expiry_is_treated_as_a_session_cookie(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "cookies.json",
        [
            {"name": "a", "value": "1", "version": 1, "expires": "not-a-date"},
            {"name": "b", "value": "1", "version": 1, "expires": True},
            {"name": "c", "value": "1", "version": 1, "expires": None},
        ],
    )

    items = load_cookie_items(path)
    assert items is not None
    assert [item["name"] for item in items] == ["a", "b", "c"]
    assert all(item["expires"] is None for item in items)


def test_http_date_expiry_is_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "cookies.json",
        [
            {
                "name": "sid",
                "value": "v",
                "version": 1,
                "expires": "Wed, 21 Oct 2099 07:28:00 GMT",
            }
        ],
    )

    items = load_cookie_items(path)
    assert items is not None
    assert items[0]["expires"] is not None
    assert items[0]["expires"] > time.time()


# ---------------------------------------------------------------------------
# SameSite / booleans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("strict", "Strict"),
        ("LAX", "Lax"),
        ("None", "None"),
        ("bogus", DEFAULT_SAME_SITE),
        ("", None),
        (None, None),
        (1, None),
    ],
)
def test_same_site_is_normalized_and_never_raises(
    tmp_path: Path, raw: Any, expected: str | None
) -> None:
    path = _write(
        tmp_path / "cookies.json",
        [{"name": "sid", "value": "v", "version": 1, "same_site": raw}],
    )

    items = load_cookie_items(path)
    assert items is not None
    assert items[0]["same_site"] == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("yes", True),
        ("no", False),
        (None, False),
        ({}, False),
    ],
)
def test_boolean_attributes_are_tolerant(
    tmp_path: Path, raw: Any, expected: bool
) -> None:
    path = _write(
        tmp_path / "cookies.json",
        [{"name": "sid", "value": "v", "version": 1, "secure": raw}],
    )

    items = load_cookie_items(path)
    assert items is not None
    assert items[0]["secure"] is expected


def test_http_only_can_be_derived_from_rest(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "cookies.json",
        [
            {
                "name": "sid",
                "value": "v",
                "version": 1,
                "rest": {"HttpOnly": None, "SameSite": "Lax"},
            }
        ],
    )

    items = load_cookie_items(path)
    assert items is not None
    assert items[0]["http_only"] is True
    assert items[0]["same_site"] == "Lax"


# ---------------------------------------------------------------------------
# File hardening
# ---------------------------------------------------------------------------


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_bytes(b"[" + b" " * MAX_COOKIE_FILE_BYTES + b"]")

    assert load_cookie_items(path) is None


def test_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_bytes(
        b"\xef\xbb\xbf" + json.dumps([{"name": "sid", "value": "v"}]).encode("utf-8")
    )

    assert load_cookie_items(path) == [{"name": "sid", "value": "v"}]


def test_deeply_nested_json_is_refused_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text("[" * 5000 + "]" * 5000, encoding="utf-8")

    assert load_cookie_items(path) is None


@pytest.mark.parametrize(
    "filename",
    [
        "C:foo",
        "cookies.",
        "cookies ",
        "a:b",  # alternate data stream on Windows
        "CON",
        "con.txt",
        "LPT1.cookies",
    ],
)
def test_resolve_rejects_dangerous_windows_names(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError, match="Invalid cookie filename"):
        resolve_cookie_path(tmp_path, filename)


@pytest.mark.parametrize(
    "filename",
    ["cookies.json", "..hidden.json", "aiohttp.cookies", "CONSOLE.cookies"],
)
def test_resolve_accepts_plain_filenames(tmp_path: Path, filename: str) -> None:
    assert resolve_cookie_path(tmp_path, filename) == tmp_path / filename


def test_cookie_values_are_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "COOKIE-IO-SECRET-VALUE"
    path = tmp_path / "cookies.json"
    write_cookie_items(path, [{"name": "sid", "value": secret}])

    with caplog.at_level(logging.DEBUG):
        load_cookie_items(path)
        write_cookie_items(path, [{"name": "sid", "value": secret}])

    assert secret not in caplog.text


def test_writer_keeps_cookie_store_compatibility(tmp_path: Path) -> None:
    """CookieStore reads a bare list; the richer entries must stay in one."""
    from novel_downloader.infra.cookies import CookieStore

    path = tmp_path / "aiohttp.cookies"
    write_cookie_items(
        path,
        [{"name": "sid", "value": "v", "secure": True, "http_only": True}],
    )

    assert CookieStore(tmp_path).get("sid") == "v"
