#!/usr/bin/env python3
"""tests.apps.test_history_files_unit
------------------------------------

Direct unit tests for :mod:`novel_downloader.apps.common.history_files`.

Existing coverage only reaches the module through the API/embedded
``mtime desc`` listing; the name/size sort branches, the empty-directory and
vanished-entry handling and the human-readable size formatter are untested.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.common import history_files
from novel_downloader.apps.common.history_files import (
    FileItem,
    _apply_sort,
    _format_size,
    _scan_files,
)


@pytest.fixture()
def output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module at a per-test output directory."""
    monkeypatch.setattr(history_files, "_output_dir", lambda: tmp_path)
    return tmp_path


def _item(name: str, size: int, mtime: float, ext: str | None = None) -> FileItem:
    return FileItem(
        path=Path("unused") / name,
        name=name,
        ext=ext if ext is not None else name.rsplit(".", 1)[-1],
        size=size,
        mtime=mtime,
    )


# --------------------------------------------------------------------------- #
# _scan_files()
# --------------------------------------------------------------------------- #


def test_scan_files_returns_empty_when_directory_missing(output_dir: Path) -> None:
    assert _scan_files() == []


def test_scan_files_collects_only_txt_and_epub_files(output_dir: Path) -> None:
    (output_dir / "a.txt").write_text("a")
    (output_dir / "b.epub").write_bytes(b"b")
    (output_dir / "c.pdf").write_bytes(b"c")
    (output_dir / "sub").mkdir()
    (output_dir / "sub" / "nested.txt").write_text("n")

    items = {item.name: item for item in _scan_files()}

    assert set(items) == {"a.txt", "b.epub"}
    assert items["a.txt"].ext == "txt"
    assert items["a.txt"].size == 1
    assert items["b.epub"].path == output_dir / "b.epub"
    # mtime is populated from the file system.
    assert items["a.txt"].mtime > 0


def test_scan_files_uppercase_extension_is_normalised(output_dir: Path) -> None:
    (output_dir / "Book.TXT").write_text("upper")

    items = _scan_files()

    assert [item.ext for item in items] == ["txt"]


def test_scan_files_skips_entries_vanishing_mid_scan(
    output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (output_dir / "stable.txt").write_text("s")

    class _VanishingEntry:
        name = "gone.txt"
        path = str(output_dir / "gone.txt")

        def is_file(self) -> bool:
            raise FileNotFoundError("deleted between scandir and is_file")

        def stat(self) -> os.stat_result:
            raise AssertionError("never reached")

    real_scandir = os.scandir

    def _scandir(path: Any) -> Any:
        if Path(path) == output_dir:
            return iter([_VanishingEntry()])
        return real_scandir(path)

    monkeypatch.setattr(history_files.os, "scandir", _scandir)

    assert _scan_files() == []


def test_scan_files_skips_entries_failing_stat(
    output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (output_dir / "stable.txt").write_text("s")

    class _BrokenStatEntry:
        name = "broken.txt"
        path = str(output_dir / "broken.txt")

        def is_file(self) -> bool:
            return True

        def stat(self) -> os.stat_result:
            raise FileNotFoundError("deleted before stat")

    real_scandir = os.scandir

    def _scandir(path: Any) -> Any:
        if Path(path) == output_dir:
            return iter([_BrokenStatEntry()])
        return real_scandir(path)

    monkeypatch.setattr(history_files.os, "scandir", _scandir)

    assert _scan_files() == []


# --------------------------------------------------------------------------- #
# _apply_sort()
# --------------------------------------------------------------------------- #


def test_apply_sort_by_name_is_case_insensitive() -> None:
    items = [_item("Zebra.txt", 1, 1.0), _item("apple.txt", 1, 1.0)]

    asc = _apply_sort(items, "name", "asc")
    desc = _apply_sort(items, "name", "desc")

    assert [item.name for item in asc] == ["apple.txt", "Zebra.txt"]
    assert [item.name for item in desc] == ["Zebra.txt", "apple.txt"]


def test_apply_sort_by_size_orders_both_ways() -> None:
    items = [_item("small.txt", 10, 1.0), _item("big.txt", 900, 1.0)]

    desc = _apply_sort(items, "size", "desc")
    asc = _apply_sort(items, "size", "asc")

    assert [item.name for item in desc] == ["big.txt", "small.txt"]
    assert [item.name for item in asc] == ["small.txt", "big.txt"]


def test_apply_sort_by_mtime_orders_both_ways() -> None:
    items = [_item("new.txt", 1, 200.0), _item("old.txt", 1, 100.0)]

    desc = _apply_sort(items, "mtime", "desc")
    asc = _apply_sort(items, "mtime", "asc")

    assert [item.name for item in desc] == ["new.txt", "old.txt"]
    assert [item.name for item in asc] == ["old.txt", "new.txt"]


def test_apply_sort_accepts_empty_input_for_every_key() -> None:
    for key in ("name", "mtime", "size"):
        for order in ("asc", "desc"):
            assert _apply_sort([], key, order) == []


# --------------------------------------------------------------------------- #
# _format_size()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (5 * 1024 + 100, "5 KB"),  # 5244 -> int(5.1) = 5
        (1024**2, "1.0 MB"),
        (int(1.5 * 1024**2), "1.5 MB"),
        (1024**3, "1.0 GB"),
        (int(3.2 * 1024**3), "3.2 GB"),
    ],
)
def test_format_size_renders_units(size: int, expected: str) -> None:
    assert _format_size(size) == expected
