#!/usr/bin/env python3
"""
tests.apps.test_cli_clean_dry_run
---------------------------------

Dry-run contracts for ``clean cache``, ``clean book`` and ``storage --clean``.

Every test builds a temporary data tree, records each entry's size and mtime,
runs the dry run, and asserts the tree is byte-for-byte and timestamp-for-
timestamp untouched.  JSON reports must be exactly one line with the stable
fields ``dry_run``, ``site``, ``book_id``/``book_ids``, ``items``, ``paths``,
``files`` and ``bytes``.  The real cleanup is run afterwards where it proves
the reported paths are the ones actually removed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import clean, storage
from novel_downloader.apps.cli.commands.clean import CleanCmd
from novel_downloader.apps.cli.commands.storage import StorageCmd

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parser(*commands: type) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in commands:
        command.register(subparsers)
    return parser


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Map each entry under ``root`` (and ``root``) to ``(size, mtime_ns)``."""
    snapshot: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            info = os.lstat(path)
            snapshot[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    info = os.lstat(root)
    snapshot["."] = (info.st_size, info.st_mtime_ns)
    return snapshot


def _json_report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one JSON line, got: {out!r}"
    payload = json.loads(lines[0])
    assert payload["dry_run"] is True
    return payload


def _no_removal(*_a: Any, **_k: Any) -> None:
    raise AssertionError("dry-run must not call the removal helper")


def _general_config(tmp_path: Path, **general: Any) -> dict[str, Any]:
    return {"general": general}


def _make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink, or a junction on Windows without privileges."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode(errors="replace")
            pytest.skip(f"cannot create junction: {detail}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"cannot create directory symlink: {exc}")


# ---------------------------------------------------------------------------
# clean cache
# ---------------------------------------------------------------------------


def _seed_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    (cache / "b520").mkdir(parents=True)
    (cache / "b520" / "one.bin").write_bytes(b"abc")
    (cache / "b520" / "nested").mkdir()
    (cache / "b520" / "nested" / "two.bin").write_bytes(b"12345")
    (cache / "qidian").mkdir()
    (cache / "qidian" / "keep.bin").write_bytes(b"zzzzzzz")
    (cache / "sentinel.txt").write_bytes(b"keep-me")
    return cache


def test_clean_cache_dry_run_json_reports_path_size_and_keeps_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = _seed_cache(tmp_path)
    before = _snapshot(cache)
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, cache_dir=str(cache)),
    )
    monkeypatch.setattr(clean, "remove_path_safely", _no_removal)

    args = _parser(CleanCmd).parse_args(
        ["clean", "cache", "--site", "b520", "--dry-run", "--json"]
    )
    args.func(args)

    payload = _json_report(capsys)
    assert payload["command"] == "clean cache"
    assert payload["site"] == "b520"
    assert payload["book_id"] is None
    assert payload["book_ids"] == []
    assert payload["paths"] == [str(cache / "b520")]
    assert payload["items"] == [
        {
            "path": str(cache / "b520"),
            "kind": "directory",
            "files": 2,
            "bytes": 8,
        }
    ]
    assert payload["files"] == 2
    assert payload["bytes"] == 8

    assert _snapshot(cache) == before
    assert (cache / "b520" / "one.bin").read_bytes() == b"abc"


def test_clean_cache_dry_run_human_lists_every_path_and_total(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = _seed_cache(tmp_path)
    before = _snapshot(cache)
    messages: list[str] = []
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, cache_dir=str(cache)),
    )
    monkeypatch.setattr(clean, "remove_path_safely", _no_removal)
    monkeypatch.setattr(clean.ui, "info", messages.append)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--dry-run"])
    args.func(args)

    text = "\n".join(messages)
    assert str(cache) in text
    assert "4 file(s)" in text
    assert "22 B" in text
    assert any("Dry-run mode" in message for message in messages)
    assert _snapshot(cache) == before


def test_clean_cache_dry_run_missing_target_is_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, cache_dir=str(cache)),
    )
    monkeypatch.setattr(clean, "remove_path_safely", _no_removal)

    args = _parser(CleanCmd).parse_args(
        ["clean", "cache", "--site", "absent", "--dry-run", "--json"]
    )
    args.func(args)

    payload = _json_report(capsys)
    assert payload["paths"] == []
    assert payload["files"] == 0
    assert payload["bytes"] == 0


def test_clean_cache_dry_run_reports_link_without_following_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"payload")
    cache = tmp_path / "cache"
    cache.mkdir()
    _make_dir_link(cache / "b520", target)
    before_target = _snapshot(target)

    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, cache_dir=str(cache)),
    )
    parser = _parser(CleanCmd)

    args = parser.parse_args(
        ["clean", "cache", "--site", "b520", "--dry-run", "--json"]
    )
    args.func(args)

    payload = _json_report(capsys)
    assert payload["items"] == [
        {"path": str(cache / "b520"), "kind": "link", "files": 0, "bytes": 0}
    ]
    assert _snapshot(target) == before_target

    args = parser.parse_args(["clean", "cache", "--site", "b520", "--yes"])
    args.func(args)

    assert not os.path.lexists(cache / "b520")
    assert _snapshot(target) == before_target
    assert (target / "keep.bin").read_bytes() == b"payload"


def test_clean_cache_dry_run_then_real_removal_matches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, cache_dir=str(cache)),
    )
    parser = _parser(CleanCmd)

    args = parser.parse_args(
        ["clean", "cache", "--site", "b520", "--dry-run", "--json"]
    )
    args.func(args)
    payload = _json_report(capsys)
    assert (cache / "b520").is_dir()

    args = parser.parse_args(["clean", "cache", "--site", "b520", "--yes"])
    args.func(args)

    assert not (cache / "b520").exists()
    assert payload["paths"] == [str(cache / "b520")]
    assert (cache / "qidian" / "keep.bin").is_file()
    assert (cache / "sentinel.txt").is_file()


def test_clean_cache_dry_run_invalid_site_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", _no_removal)
    messages: list[str] = []
    monkeypatch.setattr(clean.ui, "error", messages.append)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--site", "..", "--dry-run"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in message for message in messages)


def test_clean_cache_dry_run_rejects_unsafe_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, cache_dir=str(cache)),
    )

    def _refuse(*_a: Any, **_k: Any) -> None:
        raise ValueError("refusing to remove protected directory")

    monkeypatch.setattr(clean, "plan_removal", _refuse)
    messages: list[str] = []
    monkeypatch.setattr(clean.ui, "error", messages.append)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--dry-run", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("protected directory" in message for message in messages)
    assert cache.exists()


def test_clean_cache_json_without_dry_run_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", _no_removal)
    args = _parser(CleanCmd).parse_args(["clean", "cache", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# clean book
# ---------------------------------------------------------------------------


def _seed_raw(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    (raw / "b520" / "b1").mkdir(parents=True)
    (raw / "b520" / "b1" / "one.bin").write_bytes(b"abcd")
    (raw / "b520" / "b2").mkdir()
    (raw / "b520" / "b2" / "nested").mkdir()
    (raw / "b520" / "b2" / "nested" / "two.bin").write_bytes(b"123456")
    (raw / "b520" / "sentinel.txt").write_bytes(b"keep")
    (raw / "qidian").mkdir()
    return raw


def test_clean_book_all_dry_run_json_and_real_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _seed_raw(tmp_path)
    before = _snapshot(raw)
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    # Dry-run must not need (or touch) a site client.
    monkeypatch.setattr(clean.registrar, "get_client", _no_removal)
    parser = _parser(CleanCmd)

    args = parser.parse_args(
        ["clean", "book", "--all", "--site", "b520", "--dry-run", "--json"]
    )
    args.func(args)

    payload = _json_report(capsys)
    assert payload["command"] == "clean book"
    assert payload["site"] == "b520"
    assert payload["paths"] == [str(raw / "b520")]
    assert payload["items"] == [
        {
            "path": str(raw / "b520"),
            "kind": "directory",
            "files": 3,
            "bytes": 14,
        }
    ]
    assert _snapshot(raw) == before

    args = parser.parse_args(["clean", "book", "--all", "--site", "b520", "--yes"])
    args.func(args)

    assert not (raw / "b520").exists()
    assert (raw / "qidian").exists()


def test_clean_book_all_dry_run_lists_each_book_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _seed_raw(tmp_path)
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    monkeypatch.setattr(clean.registrar, "get_client", _no_removal)

    args = _parser(CleanCmd).parse_args(
        [
            "clean",
            "book",
            "b1",
            "b2",
            "--all",
            "--site",
            "b520",
            "--dry-run",
            "--json",
        ]
    )
    args.func(args)

    payload = _json_report(capsys)
    assert payload["site"] == "b520"
    assert payload["book_id"] is None
    assert payload["book_ids"] == ["b1", "b2"]
    assert payload["paths"] == [
        str(raw / "b520" / "b1"),
        str(raw / "b520" / "b2"),
    ]
    assert payload["files"] == 2
    assert payload["bytes"] == 10
    assert (raw / "b520" / "b1" / "one.bin").is_file()


def test_clean_book_selective_dry_run_lists_metadata_media_and_chapter_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _seed_raw(tmp_path)
    book = raw / "b520" / "b1"
    (book / "book_info.raw.json").write_bytes(b"{}")
    (book / "chapter.raw.sqlite").write_bytes(b"sql")
    (book / "media").mkdir()
    (book / "media" / "img.png").write_bytes(b"png!")
    before = _snapshot(raw)

    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    monkeypatch.setattr(clean.registrar, "get_client", _no_removal)

    args = _parser(CleanCmd).parse_args(
        [
            "clean",
            "book",
            "b1",
            "--site",
            "b520",
            "--metadata",
            "--media",
            "--dry-run",
            "--json",
        ]
    )
    args.func(args)

    payload = _json_report(capsys)
    assert payload["book_id"] == "b1"
    assert payload["paths"] == [
        str(book / "chapter.raw.sqlite"),
        str(book / "book_info.raw.json"),
        str(book / "media"),
    ]
    assert [item["kind"] for item in payload["items"]] == ["rows", "file", "directory"]
    # The SQLite file stays; only its rows are removed.
    assert payload["files"] == 2
    assert payload["bytes"] == 6
    assert _snapshot(raw) == before
    assert (book / "chapter.raw.sqlite").read_bytes() == b"sql"


def test_clean_book_dry_run_human_lists_each_chosen_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = _seed_raw(tmp_path)
    book = raw / "b520" / "b1"
    (book / "book_info.raw.json").write_bytes(b"{}")
    (book / "media").mkdir()
    (book / "media" / "img.png").write_bytes(b"png!")
    messages: list[str] = []
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    monkeypatch.setattr(clean.registrar, "get_client", _no_removal)
    monkeypatch.setattr(clean.ui, "info", messages.append)

    args = _parser(CleanCmd).parse_args(
        [
            "clean",
            "book",
            "b1",
            "--site",
            "b520",
            "--no-chapters",
            "--metadata",
            "--media",
            "--dry-run",
        ]
    )
    args.func(args)

    text = "\n".join(messages)
    assert str(book / "book_info.raw.json") in text
    assert str(book / "media") in text
    assert "2 file(s)" in text
    assert any("Dry-run mode" in message for message in messages)


def test_clean_book_dry_run_invalid_stage_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", _no_removal)
    messages: list[str] = []
    monkeypatch.setattr(clean.ui, "error", messages.append)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "b1", "--site", "b520", "--stage", "..", "--dry-run"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported stage" in message for message in messages)


def test_clean_book_json_without_dry_run_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", _no_removal)
    args = _parser(CleanCmd).parse_args(["clean", "book", "b1", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# storage --clean
# ---------------------------------------------------------------------------


def _storage_paths(root: Path) -> dict[str, Path]:
    return {
        "raw": root / "raw",
        "cache": root / "cache",
        "downloads": root / "downloads",
        "logs": root / "logs",
    }


def test_storage_clean_dry_run_json_lists_targets_and_keeps_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _storage_paths(tmp_path)
    sizes: dict[str, int] = {}
    for key, path in paths.items():
        path.mkdir()
        (path / "file.bin").write_bytes(key.encode() * 3)
        sizes[key] = len(key) * 3
    (paths["raw"] / "nested").mkdir()
    (paths["raw"] / "nested" / "extra.bin").write_bytes(b"12")
    sizes["raw"] += 2
    before = {key: _snapshot(path) for key, path in paths.items()}

    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    monkeypatch.setattr(storage, "remove_path_safely", _no_removal)

    args = _parser(StorageCmd).parse_args(
        ["storage", "--clean", "--yes", "--dry-run", "--json"]
    )
    args.func(args)

    payload = _json_report(capsys)
    assert payload["command"] == "storage --clean"
    assert payload["site"] is None
    assert payload["paths"] == [
        str(paths[key]) for key in ("raw", "cache", "downloads", "logs")
    ]
    assert [item["kind"] for item in payload["items"]] == ["directory"] * 4
    assert payload["files"] == 5
    assert payload["bytes"] == sum(sizes.values())
    for key, path in paths.items():
        assert _snapshot(path) == before[key]


def test_storage_clean_dry_run_collapses_nested_targets_and_real_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _storage_paths(tmp_path)
    paths["raw"].mkdir()
    (paths["raw"] / "book.bin").write_bytes(b"raw")
    # ``downloads`` lives inside ``raw``; removing the parent covers it.
    paths["downloads"] = paths["raw"] / "downloads"
    paths["downloads"].mkdir()
    (paths["downloads"] / "out.bin").write_bytes(b"out")
    for key in ("cache", "logs"):
        paths[key].mkdir()
        (paths[key] / "file.bin").write_bytes(b"x")

    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    parser = _parser(StorageCmd)

    args = parser.parse_args(["storage", "--clean", "--yes", "--dry-run", "--json"])
    args.func(args)

    payload = _json_report(capsys)
    assert payload["paths"] == [
        str(paths["raw"]),
        str(paths["cache"]),
        str(paths["logs"]),
    ]
    assert payload["files"] == 4
    assert payload["bytes"] == len(b"raw") + len(b"out") + 1 + 1

    args = parser.parse_args(["storage", "--clean", "--yes"])
    args.func(args)

    assert not any(path.exists() for path in paths.values())


def test_storage_clean_dry_run_human_lists_every_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _storage_paths(tmp_path)
    for path in paths.values():
        path.mkdir()
        (path / "file.bin").write_bytes(b"x")
    messages: list[str] = []
    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    monkeypatch.setattr(storage, "remove_path_safely", _no_removal)
    monkeypatch.setattr(storage.ui, "info", messages.append)

    args = _parser(StorageCmd).parse_args(["storage", "--clean", "--dry-run"])
    args.func(args)

    text = "\n".join(messages)
    for path in paths.values():
        assert str(path) in text
    assert "4 file(s)" in text
    assert all(path.exists() for path in paths.values())


def test_storage_clean_dry_run_requires_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "_resolve_paths", lambda: {})
    args = _parser(StorageCmd).parse_args(["storage", "--dry-run"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert excinfo.value.code == 2


def test_storage_clean_dry_run_rejects_unsafe_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _storage_paths(tmp_path)
    paths["logs"].mkdir()
    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)

    def _refuse(*_a: Any, **_k: Any) -> None:
        raise ValueError("refusing to remove protected directory")

    monkeypatch.setattr(storage, "plan_removal", _refuse)
    messages: list[str] = []
    monkeypatch.setattr(storage.ui, "error", messages.append)

    args = _parser(StorageCmd).parse_args(
        ["storage", "--clean", "--yes", "--dry-run", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("protected directory" in message for message in messages)
    assert paths["logs"].exists()
