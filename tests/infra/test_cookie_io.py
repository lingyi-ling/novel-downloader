"""cookie_io hardening: path traversal guard, tolerant loader, atomic writer."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra.sessions.cookie_io import (
    load_cookie_items,
    resolve_cookie_path,
    write_cookie_items,
)

VALID_ITEM: dict[str, str] = {"name": "session", "value": "abc"}


# ---------------------------------------------------------------------------
# resolve_cookie_path: path traversal protection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected_name"),
    [
        ("cookies.json", "cookies.json"),
        ("..hidden.json", "..hidden.json"),  # dotted name is not traversal
        ("cookies.v2.backup.json", "cookies.v2.backup.json"),
    ],
)
def test_resolve_accepts_plain_filenames(
    tmp_path: Path, filename: str, expected_name: str
) -> None:
    resolved = resolve_cookie_path(tmp_path, filename)
    assert resolved == tmp_path / expected_name


@pytest.mark.parametrize(
    "filename",
    [
        "",  # empty
        ".",  # current dir
        "..",  # parent dir
        "sub/cookies.json",  # forward separator
        "sub\\cookies.json",  # back separator (Windows-style / absolute)
        "a\x00b",  # NUL byte
        "/etc/passwd",  # absolute POSIX path
        "C:\\tmp\\cookies.json",  # absolute Windows path
    ],
)
def test_resolve_rejects_traversal_and_unsafe_names(
    tmp_path: Path, filename: str
) -> None:
    with pytest.raises(ValueError, match="Invalid cookie filename"):
        resolve_cookie_path(tmp_path, filename)


# ---------------------------------------------------------------------------
# load_cookie_items: tolerant JSON reader
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_cookie_items(tmp_path / "absent.json") is None


def test_load_returns_none_for_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "cookies.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_cookie_items(bad) is None


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"name": "a", "value": "b"}),  # dict, not list
        json.dumps("just a string"),
        json.dumps(42),
        json.dumps(None),
    ],
)
def test_load_returns_none_for_non_list_root(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(raw, encoding="utf-8")
    assert load_cookie_items(path) is None


def test_load_empty_list(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "cookies.json", [])
    assert load_cookie_items(path) == []


def test_load_normalizes_full_entry(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "cookies.json",
        [{"name": "sess", "value": "v1", "domain": ".example.com", "path": "/book/1"}],
    )
    assert load_cookie_items(path) == [
        {"name": "sess", "value": "v1", "domain": ".example.com", "path": "/book/1"}
    ]


def test_load_ignores_unknown_keys(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "cookies.json",
        [{"name": "sess", "value": "v1", "secure": True, "httpOnly": 1, "extra": "x"}],
    )
    assert load_cookie_items(path) == [{"name": "sess", "value": "v1"}]


def test_load_skips_non_mapping_entries(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "cookies.json",
        [None, 1, "name=value", ["name", "value"], {"name": "ok", "value": "kept"}],
    )
    assert load_cookie_items(path) == [{"name": "ok", "value": "kept"}]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (1, "v"),  # non-string name
        (None, "v"),
        ("n", 1),  # non-string value
        ("n", None),
        (None, None),
    ],
)
def test_load_skips_entries_with_non_string_name_or_value(
    tmp_path: Path, name: Any, value: Any
) -> None:
    path = _write_json(tmp_path / "cookies.json", [{"name": name, "value": value}])
    assert load_cookie_items(path) == []


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "a;b",  # separator
        "a,b",  # separator
        "a b",  # whitespace
        "a\tb",  # whitespace
        "a\x00b",  # control char
        "a\x7fb",  # DEL
    ],
)
def test_load_skips_invalid_cookie_names(tmp_path: Path, name: str) -> None:
    path = _write_json(tmp_path / "cookies.json", [{"name": name, "value": "v"}])
    assert load_cookie_items(path) == []


def test_load_strips_control_chars_from_value(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "cookies.json",
        [{"name": "sess", "value": "a\x00b\x07c\rd\ne"}],
    )
    assert load_cookie_items(path) == [{"name": "sess", "value": "abcde"}]


def test_load_omits_empty_or_non_string_domain(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "cookies.json",
        [
            {"name": "a", "value": "v", "domain": ""},
            {"name": "b", "value": "v", "domain": 123},
            {"name": "c", "value": "v", "domain": None},
            {"name": "d", "value": "v", "domain": ".ok.com"},
        ],
    )
    items = load_cookie_items(path)
    assert items is not None
    assert [item.get("domain") for item in items] == [None, None, None, ".ok.com"]


@pytest.mark.parametrize(
    ("path_value", "kept"),
    [
        ("/chapter/1", True),
        ("chapter/1", False),  # must start with "/"
        ("", False),
        (None, False),
        (123, False),
    ],
)
def test_load_path_field_rules(tmp_path: Path, path_value: Any, kept: bool) -> None:
    path = _write_json(
        tmp_path / "cookies.json",
        [{"name": "a", "value": "v", "path": path_value}],
    )
    items = load_cookie_items(path)
    assert items is not None
    assert ("path" in items[0]) is kept
    if kept:
        assert items[0]["path"] == path_value


# ---------------------------------------------------------------------------
# write_cookie_items: atomic writer
# ---------------------------------------------------------------------------


def test_write_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    items = [
        {"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
        {"name": "b", "value": "2"},
    ]
    write_cookie_items(target, items)
    assert load_cookie_items(target) == items


def test_write_accepts_any_iterable(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    write_cookie_items(target, (item for item in [{"name": "a", "value": "1"}]))
    assert load_cookie_items(target) == [{"name": "a", "value": "1"}]


def test_write_empty_items(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    write_cookie_items(target, [])
    assert target.read_text(encoding="utf-8") == "[]"
    assert load_cookie_items(target) == []


def test_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "dir" / "cookies.json"
    write_cookie_items(target, [VALID_ITEM])
    assert target.is_file()
    assert load_cookie_items(target) == [VALID_ITEM]


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    write_cookie_items(target, [{"name": "old", "value": "1"}])
    write_cookie_items(target, [{"name": "new", "value": "2"}])
    assert load_cookie_items(target) == [{"name": "new", "value": "2"}]


def test_write_is_utf8_without_ascii_escaping_and_uses_lf(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    write_cookie_items(target, [{"name": "sess", "value": "值"}])

    raw = target.read_bytes()
    assert "值".encode() in raw  # not escaped to \uXXXX
    assert b"\\u" not in raw
    assert b"\r\n" not in raw  # newline="\n", no platform translation


def test_write_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    write_cookie_items(target, [VALID_ITEM])
    leftovers = list(tmp_path.glob(".cookies.json.*.tmp"))
    assert leftovers == []


def test_write_failure_replaces_nothing_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cookies.json"
    target.write_text('{"sentinel": true}', encoding="utf-8")

    def boom(*args: Any, **kwargs: Any) -> None:
        msg = "replace failed"
        raise OSError(msg)

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="replace failed"):
        write_cookie_items(target, [VALID_ITEM])

    # destination untouched, temp file removed
    assert json.loads(target.read_text(encoding="utf-8")) == {"sentinel": True}
    assert list(tmp_path.glob(".cookies.json.*.tmp")) == []


def test_write_serialization_failure_leaves_no_file(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    with pytest.raises(TypeError):
        write_cookie_items(target, [{"name": "a", "value": {1, 2}}])  # unserializable
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_write_restricts_file_to_owner_on_posix(tmp_path: Path) -> None:
    target = tmp_path / "cookies.json"
    write_cookie_items(target, [VALID_ITEM])
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600
