#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.plugins.mixins.cleanup`.

Everything runs against a real ``CommonClient`` confined to ``tmp_path``; no
site plugin or network is involved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.schemas import BookConfig, BookInfoDict, ClientConfig

SITE = "demo"
BOOK_ID = "b1"


def _book_info() -> BookInfoDict:
    return {
        "book_name": "Demo Book",
        "author": "Author",
        "cover_url": "",
        "update_time": "",
        "summary": "",
        "extra": {},
        "volumes": [
            {
                "volume_name": "v1",
                "chapters": [
                    {"title": "c1", "url": "", "chapterId": "c1"},
                    {"title": "c2", "url": "", "chapterId": "c2"},
                ],
            }
        ],
    }


def _client(tmp_path: Path) -> CommonClient:
    return CommonClient(
        SITE,
        ClientConfig(
            raw_data_dir=str(tmp_path / "raw"),
            cache_dir=str(tmp_path / "cache"),
            output_dir=str(tmp_path / "out"),
        ),
    )


def _book_path(tmp_path: Path) -> Path:
    return tmp_path / "raw" / SITE / BOOK_ID


def _book_dir(tmp_path: Path) -> Path:
    d = _book_path(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_book(tmp_path: Path, chapters: int = 2) -> None:
    d = _book_dir(tmp_path)
    (d / "book_info.raw.json").write_text(
        json.dumps(_book_info(), ensure_ascii=False), encoding="utf-8"
    )
    (d / "media").mkdir(exist_ok=True)
    (d / "media" / "img.png").write_bytes(b"png")
    with ChapterStorage(d, "chapter.raw.sqlite") as store:
        for i in range(chapters):
            store.upsert_chapter(
                {"id": f"c{i + 1}", "title": f"T{i + 1}", "content": "x", "extra": {}}
            )


def _book() -> BookConfig:
    return BookConfig(book_id=BOOK_ID)


# --------------------------------------------------------------------------- #
# cleanup_book
# --------------------------------------------------------------------------- #


def test_cleanup_book_remove_all_deletes_whole_directory(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    client = _client(tmp_path)

    client.cleanup_book(_book(), remove_all=True)

    assert not _book_path(tmp_path).exists()


def test_cleanup_book_remove_all_when_directory_missing(tmp_path: Path) -> None:
    client = _client(tmp_path)
    # Must not raise when there is nothing to remove.
    client.cleanup_book(_book(), remove_all=True)
    assert not _book_path(tmp_path).exists()


def test_cleanup_book_selective_default_flags(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    client = _client(tmp_path)

    client.cleanup_book(_book())  # chapters + metadata, media kept

    d = _book_path(tmp_path)
    assert not (d / "book_info.raw.json").exists()
    # Chapter rows are deleted; the sqlite file itself is kept in place.
    with ChapterStorage(d, "chapter.raw.sqlite") as store:
        assert store.existing_ids() == set()
    assert (d / "media" / "img.png").exists()


def test_cleanup_book_media_only(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    client = _client(tmp_path)

    client.cleanup_book(
        _book(), remove_metadata=False, remove_chapters=False, remove_media=True
    )

    d = _book_dir(tmp_path)
    assert (d / "book_info.raw.json").exists()
    assert (d / "chapter.raw.sqlite").exists()
    assert not (d / "media").exists()


# --------------------------------------------------------------------------- #
# cleanup_metadata
# --------------------------------------------------------------------------- #


def test_cleanup_metadata_deletes_only_requested_stage(tmp_path: Path) -> None:
    d = _book_dir(tmp_path)
    (d / "book_info.raw.json").write_text("{}", encoding="utf-8")
    (d / "book_info.cleaner.json").write_text("{}", encoding="utf-8")
    client = _client(tmp_path)

    client.cleanup_metadata(BOOK_ID, stage="raw")

    assert not (d / "book_info.raw.json").exists()
    assert (d / "book_info.cleaner.json").exists()


def test_cleanup_metadata_missing_file_is_a_noop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(tmp_path)
    with caplog.at_level(logging.DEBUG):
        client.cleanup_metadata(BOOK_ID)  # nothing exists
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_cleanup_metadata_unlink_failure_is_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = _book_dir(tmp_path)
    (d / "book_info.raw.json").write_text("{}", encoding="utf-8")

    def _boom(self: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", _boom)
    client = _client(tmp_path)
    with caplog.at_level(logging.WARNING):
        client.cleanup_metadata(BOOK_ID)

    assert any("Failed to remove metadata" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# cleanup_chapters
# --------------------------------------------------------------------------- #


def test_cleanup_chapters_deletes_rows_and_vacuums(tmp_path: Path) -> None:
    _seed_book(tmp_path, chapters=2)
    client = _client(tmp_path)
    db_path = _book_path(tmp_path) / "chapter.raw.sqlite"
    assert db_path.is_file()

    client.cleanup_chapters(BOOK_ID)

    with ChapterStorage(_book_path(tmp_path), "chapter.raw.sqlite") as store:
        assert store.existing_ids() == set()


def test_cleanup_chapters_respects_range_and_ignore(tmp_path: Path) -> None:
    _seed_book(tmp_path, chapters=4)
    client = _client(tmp_path)

    client.cleanup_chapters(
        BOOK_ID, start_id="c2", end_id="c3", ignore_ids=frozenset({"c3"})
    )

    with ChapterStorage(_book_path(tmp_path), "chapter.raw.sqlite") as store:
        assert store.existing_ids() == {"c1", "c3", "c4"}


def test_cleanup_chapters_without_db_is_a_noop(tmp_path: Path) -> None:
    d = _book_dir(tmp_path)  # metadata present, but no sqlite file
    (d / "book_info.raw.json").write_text(
        json.dumps(_book_info(), ensure_ascii=False), encoding="utf-8"
    )
    client = _client(tmp_path)
    # Must not raise.
    client.cleanup_chapters(BOOK_ID)
    assert not (_book_path(tmp_path) / "chapter.raw.sqlite").exists()


# --------------------------------------------------------------------------- #
# cleanup_media
# --------------------------------------------------------------------------- #


def test_cleanup_media_removes_media_dir(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    client = _client(tmp_path)

    client.cleanup_media(BOOK_ID)
    assert not (_book_path(tmp_path) / "media").exists()


def test_cleanup_media_missing_dir_is_a_noop(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.cleanup_media(BOOK_ID)  # nothing to do
    assert not (_book_path(tmp_path) / "media").exists()
