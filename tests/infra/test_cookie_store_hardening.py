"""Hardening tests for :class:`CookieStore` file parsing."""

from __future__ import annotations

import json
import logging
import os

import pytest

from novel_downloader.infra.cookies import CookieStore


def _write(path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("payload", [{}, "not-a-list", 123, None, True])
def test_top_level_non_list_is_ignored(tmp_path, payload: object) -> None:
    _write(tmp_path / "aiohttp.cookies", payload)
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {}


def test_non_mapping_entries_are_skipped(tmp_path) -> None:
    _write(
        tmp_path / "aiohttp.cookies",
        [1, "x", [], {"name": "ok", "value": "1"}],
    )
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {"ok": "1"}


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "a"},
        {"value": "1"},
        {"name": None, "value": "1"},
        {"name": "a", "value": None},
        {"name": 1, "value": 2},
        {"name": "", "value": "1"},
        {"name": "bad name", "value": "1"},
        {"name": "bad\x00name", "value": "1"},
        {"name": "bad;name", "value": "1"},
    ],
)
def test_invalid_entries_are_skipped(tmp_path, entry: object) -> None:
    _write(tmp_path / "aiohttp.cookies", [entry])
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {}


def test_empty_and_long_values_are_preserved(tmp_path) -> None:
    long_value = "v" * 50_000
    _write(
        tmp_path / "aiohttp.cookies",
        [
            {"name": "empty", "value": ""},
            {"name": "long", "value": long_value},
        ],
    )
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {"empty": "", "long": long_value}


def test_control_characters_are_stripped_from_values(tmp_path) -> None:
    _write(tmp_path / "aiohttp.cookies", [{"name": "a", "value": "x\x00y\nz"}])
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {"a": "xyz"}


def test_changed_file_drops_removed_cookies(tmp_path) -> None:
    path = tmp_path / "aiohttp.cookies"
    _write(path, [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}])

    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {"a": "1", "b": "2"}

    _write(path, [{"name": "a", "value": "9"}])
    # Force a different mtime even on filesystems with coarse timestamps.
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
    store.load_all()

    assert store.cache == {"a": "9"}


def test_removed_file_drops_its_cookies(tmp_path) -> None:
    path = tmp_path / "aiohttp.cookies"
    _write(path, [{"name": "a", "value": "1"}])
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {"a": "1"}

    path.unlink()
    store.load_all()
    assert store.cache == {}


def test_later_file_overrides_earlier_file(tmp_path) -> None:
    _write(tmp_path / "aiohttp.cookies", [{"name": "x", "value": "first"}])
    _write(tmp_path / "httpx.cookies", [{"name": "x", "value": "last"}])

    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {"x": "last"}


def test_malformed_json_does_not_raise(tmp_path) -> None:
    (tmp_path / "aiohttp.cookies").write_text("{not-json", encoding="utf-8")
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {}


def test_cookie_store_does_not_log_values(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "COOKIE-STORE-SECRET"
    _write(tmp_path / "aiohttp.cookies", [{"name": "token", "value": secret}])

    store = CookieStore(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="novel_downloader.infra"):
        assert store.get("token") == secret

    assert secret not in caplog.text
    assert all(record.name.startswith("novel_downloader") for record in caplog.records)


def test_corrupt_rewrite_drops_the_previous_cookies(tmp_path) -> None:
    """A half-written file must not keep serving the old credentials."""
    path = tmp_path / "aiohttp.cookies"
    _write(path, [{"name": "sid", "value": "old"}])
    store = CookieStore(tmp_path)
    store.load_all()
    assert store.cache == {"sid": "old"}

    # Simulate a crash mid-write: valid JSON is gone.  Move the mtime forward
    # explicitly: a rewrite that lands in the same filesystem timestamp tick is
    # (correctly) treated as cached, which would make this test clock-dependent.
    path.write_text('{"broken":', encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 1.0))
    store.load_all()

    assert store.cache == {}
    # The broken file's mtime is recorded, so it is not re-read every call.
    assert store.mtimes[str(path.name)] == path.stat().st_mtime


def test_corrupt_file_is_not_reread_on_every_access(tmp_path, monkeypatch) -> None:
    path = tmp_path / "aiohttp.cookies"
    path.write_text("{", encoding="utf-8")
    store = CookieStore(tmp_path)
    store.load_all()

    reads = 0
    original = CookieStore._load_file if hasattr(CookieStore, "_load_file") else None
    real_read_text = type(path).read_text

    def _counting_read(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", _counting_read)
    store.load_all()
    store.load_all()

    assert reads == 0
    assert original is None or callable(original)
