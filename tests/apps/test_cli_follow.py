#!/usr/bin/env python3
"""
tests.apps.test_cli_follow
--------------------------

Offline tests for the ``follow``, ``doctor`` and ``storage`` commands.

Searches are replaced with small async fakes, so no network access happens.
The watchlist lives in a temporary ``NOVEL_HOME`` and doctor/storage paths are
monkeypatched, so the real user data is never touched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import doctor, follow, storage
from novel_downloader.apps.cli.commands.doctor import Check, DoctorCmd
from novel_downloader.apps.cli.commands.follow import (
    FollowAddCmd,
    FollowCheckCmd,
    FollowListCmd,
    FollowRemoveCmd,
)
from novel_downloader.apps.cli.commands.storage import StorageCmd
from novel_downloader.apps.core.watchlist import WatchlistStore


def _parser(*commands: type) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in commands:
        command.register(subparsers)
    return parser


def _capture_ui(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    messages: list[str] = []
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(module.ui, name, messages.append)
    return messages


def _result(
    site: str,
    book_id: str,
    title: str,
    latest_chapter: str,
) -> dict[str, Any]:
    return {
        "site": site,
        "book_id": book_id,
        "book_url": "",
        "cover_url": "",
        "title": title,
        "author": "",
        "latest_chapter": latest_chapter,
        "update_date": "",
        "word_count": "",
        "priority": 100,
    }


# ---------------------------------------------------------------------------
# follow add / remove / list
# ---------------------------------------------------------------------------


def test_follow_add_list_remove_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    parser = _parser(FollowAddCmd, FollowListCmd, FollowRemoveCmd)

    args = parser.parse_args(["add", "b520", "8_8187", "--title", "Book A"])
    args.func(args)

    args = parser.parse_args(["add", "b520", "8_8187", "--title", "Book A2"])
    args.func(args)

    args = parser.parse_args(["list", "--json"])
    args.func(args)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["site"] == "b520"
    assert payload[0]["book_id"] == "8_8187"
    assert payload[0]["title"] == "Book A2"

    args = parser.parse_args(["remove", "b520", "8_8187"])
    args.func(args)
    assert WatchlistStore().entries() == []


def test_follow_add_without_title_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowAddCmd)

    args = parser.parse_args(["add", "b520", "1"])
    args.func(args)

    assert any("No title set" in message for message in messages)


def test_follow_remove_unknown_book_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowRemoveCmd)

    args = parser.parse_args(["remove", "b520", "missing"])
    args.func(args)  # idempotent: no SystemExit

    assert any("Not in the follow list" in message for message in messages)


def test_follow_list_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowListCmd)

    args = parser.parse_args(["list"])
    args.func(args)

    assert any("No followed books." in message for message in messages)


# ---------------------------------------------------------------------------
# follow check
# ---------------------------------------------------------------------------


def _fake_search(
    results: dict[str, list[dict[str, Any]]],
    errors: dict[str, BaseException] | None = None,
) -> Any:
    async def search(
        keyword: str,
        sites: Any = None,
        limit: Any = None,
        per_site_limit: int = 5,
        timeout: float = 5.0,
        nsfw: bool = False,
        sort: str = "quality",
        order: Any = None,
    ) -> list[dict[str, Any]]:
        if errors and keyword in errors:
            raise errors[keyword]
        return list(results.get(keyword, []))

    return search


def test_follow_check_updates_chapters_and_prints_download_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    store = WatchlistStore()
    store.add("b520", "1", title="Book A", last_chapter="Chapter 1")
    store.add("qidian", "2", title="Book B")
    store.add("b520", "3", title="No Result", last_chapter="Unknown")

    monkeypatch.setattr(
        follow.search_module,
        "search",
        _fake_search(
            {
                "Book A": [_result("b520", "1", "Book A", "Chapter 2")],
                "Book B": [_result("qidian", "new-id", "Book B", "Chapter 1")],
                "No Result": [],
            }
        ),
    )
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--all", "--download"])
    args.func(args)

    assert store.get("b520", "1").last_chapter == "Chapter 2"
    assert store.get("qidian", "2").last_chapter == "Chapter 1"
    assert store.get("b520", "3").last_chapter == "Unknown"

    joined = "\n".join(messages)
    assert "novel-cli download --site b520 1" in joined
    assert "novel-cli download --site qidian 2" in joined
    assert "'follow check' does not start downloads" in joined
    assert "No result found for b520/3." in joined


def test_follow_check_prefers_exact_book_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    store = WatchlistStore()
    store.add("b520", "old", title="Same Title", last_chapter="Chapter 1")

    monkeypatch.setattr(
        follow.search_module,
        "search",
        _fake_search(
            {
                "Same Title": [
                    _result("b520", "different", "Same Title", "Wrong"),
                    _result("b520", "old", "Same Title", "Right"),
                ]
            }
        ),
    )
    _capture_ui(monkeypatch, follow)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check"])
    args.func(args)

    assert store.get("b520", "old").last_chapter == "Right"


def test_follow_check_all_shows_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    store = WatchlistStore()
    store.add("b520", "1", title="Book A", last_chapter="Chapter 1")

    monkeypatch.setattr(
        follow.search_module,
        "search",
        _fake_search({"Book A": [_result("b520", "1", "Book A", "Chapter 1")]}),
    )
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--all"])
    args.func(args)
    assert any("Up to date: b520/1" in message for message in messages)

    messages.clear()
    args = parser.parse_args(["check"])
    args.func(args)
    assert any("No new chapters found." in message for message in messages)
    assert not any("Up to date:" in message for message in messages)


def test_follow_check_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    store = WatchlistStore()
    store.add("b520", "1", title="Book A")

    monkeypatch.setattr(
        follow.search_module,
        "search",
        _fake_search({}, errors={"Book A": RuntimeError("site down")}),
    )
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--all"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    assert any("site down" in message for message in messages)
    assert store.get("b520", "1").last_chapter == ""


def test_follow_check_empty_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    called = False

    async def search(**_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(follow.search_module, "search", search)
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check"])
    args.func(args)

    assert called is False
    assert any("No followed books to check." in message for message in messages)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_settings_and_config_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))

    settings = doctor._check_settings_file()
    assert settings.ok is False
    assert "config init" in settings.suggestion

    config = doctor._check_config()
    assert config.ok is False
    assert "config init" in config.suggestion


def test_doctor_config_invalid_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))
    (tmp_path / "settings.toml").write_text("not = [valid", encoding="utf-8")

    assert doctor._check_settings_file().ok is True
    result = doctor._check_config()
    assert result.ok is False
    assert result.fatal is True
    assert "config init" in result.suggestion


def test_doctor_output_dir_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(doctor, "_output_dir", lambda: blocked)

    result = doctor._check_output_dir()
    assert result.ok is False
    assert result.fatal is True
    assert "permissions" in result.suggestion


def test_doctor_output_dir_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "downloads"
    monkeypatch.setattr(doctor, "_output_dir", lambda: target)

    assert doctor._check_output_dir().ok is True
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_doctor_searcher_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("registry broken")

    monkeypatch.setattr(doctor.registrar, "get_searcher_classes", boom)
    result = doctor._check_searchers()
    assert result.ok is False
    assert "registry broken" in result.detail


def test_doctor_music_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "MUSIC_DEPENDENCIES", ("module_that_does_not_exist",))
    result = doctor._check_music()
    assert result.ok is False
    assert result.fatal is False
    assert "novel-downloader[music]" in result.suggestion


def test_doctor_api_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_port_is_free", lambda _port: True)
    assert doctor._check_api_port(8081).ok is True

    monkeypatch.setattr(doctor, "_port_is_free", lambda _port: False)
    result = doctor._check_api_port(8081)
    assert result.ok is False
    assert "already in use" in result.detail


def test_doctor_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_ui(monkeypatch, doctor)
    parser = _parser(DoctorCmd)

    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda _port=8081: [
            Check("python", True),
            Check("music", False, "missing", fatal=False),
        ],
    )
    args = parser.parse_args(["doctor"])
    args.func(args)  # optional failure does not raise

    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda _port=8081: [Check("python", True), Check("config", False)],
    )
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)
    assert exc_info.value.code == 1

    monkeypatch.setattr(
        doctor, "collect_checks", lambda _port=8081: [Check("python", True)]
    )
    args.func(args)  # all pass


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _storage_paths(root: Path) -> dict[str, Path]:
    return {
        "raw": root / "raw",
        "cache": root / "cache",
        "downloads": root / "downloads",
        "logs": root / "logs",
    }


def test_storage_json_reports_sizes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _storage_paths(tmp_path)
    (paths["raw"] / "nested").mkdir(parents=True)
    (paths["raw"] / "a.txt").write_bytes(b"abc")
    (paths["raw"] / "nested" / "b.txt").write_bytes(b"12345")
    paths["cache"].mkdir()
    (paths["cache"] / "c.bin").write_bytes(b"1234567")
    paths["downloads"].mkdir()  # empty directory

    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    parser = _parser(StorageCmd)

    args = parser.parse_args(["storage", "--json"])
    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["raw"]["files"] == 2
    assert payload["raw"]["bytes"] == 8
    assert payload["cache"]["files"] == 1
    assert payload["cache"]["bytes"] == 7
    assert payload["downloads"]["files"] == 0
    assert payload["logs"]["files"] == 0
    assert payload["total"] == {"files": 3, "bytes": 15}
    assert payload["raw"]["path"] == str(paths["raw"])


def test_storage_renders_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _storage_paths(tmp_path)
    paths["raw"].mkdir()
    (paths["raw"] / "a.txt").write_bytes(b"x")

    captured: list[tuple[str, list[str], list[list[str]]]] = []

    def render(title: str, columns: list[str], rows: list[list[str]]) -> None:
        captured.append((title, list(columns), list(rows)))

    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    _capture_ui(monkeypatch, storage)
    monkeypatch.setattr(storage.ui, "render_table", render)
    parser = _parser(StorageCmd)

    args = parser.parse_args(["storage"])
    args.func(args)

    assert len(captured) == 1
    _title, _columns, rows = captured[0]
    assert rows[-1][0] == "Total"
    assert rows[-1][2] == "1"
    assert rows[-1][3] == "1 B"


def test_storage_clean_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _storage_paths(tmp_path)
    for path in paths.values():
        path.mkdir()
        (path / "file").write_text("x", encoding="utf-8")

    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    _capture_ui(monkeypatch, storage)
    parser = _parser(StorageCmd)

    args = parser.parse_args(["storage", "--clean", "--yes"])
    args.func(args)

    assert not any(path.exists() for path in paths.values())


def test_storage_clean_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _storage_paths(tmp_path)
    paths["logs"].mkdir()
    (paths["logs"] / "file").write_text("x", encoding="utf-8")

    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    messages = _capture_ui(monkeypatch, storage)
    monkeypatch.setattr(storage.ui, "confirm", lambda *_a, **_k: False)
    parser = _parser(StorageCmd)

    args = parser.parse_args(["storage", "--clean"])
    args.func(args)

    assert paths["logs"].exists()
    assert any("Cancelled." in message for message in messages)


def test_storage_json_and_clean_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(storage, "_resolve_paths", lambda: _storage_paths(tmp_path))
    _capture_ui(monkeypatch, storage)
    parser = _parser(StorageCmd)

    args = parser.parse_args(["storage", "--json", "--clean"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)
    assert exc_info.value.code == 2


def test_storage_missing_directories_are_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _storage_paths(tmp_path)
    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)

    stats = storage._collect(paths)
    assert [(stat.files, stat.bytes) for stat in stats] == [(0, 0)] * 4
