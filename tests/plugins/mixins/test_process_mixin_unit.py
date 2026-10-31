#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.plugins.mixins.process`.

A real ``CommonClient`` is used (constructor only touches the filesystem), the
processor registry is monkeypatched with an in-memory double, and all SQLite
storage stays inside ``tmp_path``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.mixins import process as process_mod
from novel_downloader.schemas import (
    BookConfig,
    BookInfoDict,
    ChapterDict,
    ClientConfig,
    ProcessorConfig,
)

SITE = "demo"
BOOK_ID = "b1"


class FakeUI:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: list[str] = []
        self.progress: list[tuple[str, int, int]] = []
        self.missing: list[tuple[str, str]] = []

    def on_stage_start(self, book: BookConfig, stage: str) -> None:
        self.started.append(stage)

    def on_stage_complete(self, book: BookConfig, stage: str) -> None:
        self.completed.append(stage)

    def on_stage_progress(
        self, book: BookConfig, stage: str, done: int, total: int
    ) -> None:
        self.progress.append((stage, done, total))

    def on_missing(self, book: BookConfig, what: str, path: Path) -> None:
        self.missing.append((what, str(path)))


class FakeProcessor:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.book_info_calls = 0
        self.chapter_calls: list[str] = []

    def process_book_info(self, info: BookInfoDict) -> BookInfoDict:
        self.book_info_calls += 1
        if "info" in self.fail_on:
            raise RuntimeError("info boom")
        return info

    def process_chapter(self, chap: ChapterDict) -> ChapterDict:
        cid = chap["id"]
        self.chapter_calls.append(cid)
        if cid in self.fail_on:
            raise RuntimeError(f"chapter boom: {cid}")
        return {**chap, "content": chap["content"] + "-done"}


class FakeRegistry:
    def __init__(self, processor: FakeProcessor) -> None:
        self._processor = processor
        self.requested: list[tuple[str, dict[str, Any]]] = []

    def get_processor(self, name: str, config: dict[str, Any]) -> FakeProcessor:
        self.requested.append((name, config))
        return self._processor


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


def _book_dir(tmp_path: Path) -> Path:
    d = tmp_path / "raw" / SITE / BOOK_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client(tmp_path: Path) -> CommonClient:
    return CommonClient(
        SITE,
        ClientConfig(
            raw_data_dir=str(tmp_path / "raw"),
            cache_dir=str(tmp_path / "cache"),
            output_dir=str(tmp_path / "out"),
        ),
    )


def _seed_raw(tmp_path: Path, chapters: list[tuple[str, bool]]) -> None:
    d = _book_dir(tmp_path)
    (d / "book_info.raw.json").write_text(
        json.dumps(_book_info(), ensure_ascii=False), encoding="utf-8"
    )
    with ChapterStorage(d, "chapter.raw.sqlite") as store:
        for cid, need_refetch in chapters:
            store.upsert_chapter(
                {
                    "id": cid,
                    "title": f"T {cid}",
                    "content": f"raw {cid}",
                    "extra": {},
                },
                need_refetch=need_refetch,
            )


# --------------------------------------------------------------------------- #
# process_book orchestration
# --------------------------------------------------------------------------- #


def test_process_book_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_raw(tmp_path, [("c1", False), ("c2", False)])
    processor = FakeProcessor()
    monkeypatch.setattr(process_mod, "registrar", FakeRegistry(processor))

    client = _client(tmp_path)
    ui = FakeUI()
    book = BookConfig(book_id=BOOK_ID)
    processors = [
        ProcessorConfig(name="cleaner", options={"a": 1}),
        ProcessorConfig(name="corrector", options={"b": 2}),
    ]

    client.process_book(book, processors, ui=ui)

    assert ui.started == ["cleaner", "corrector"]
    assert ui.completed == ["cleaner", "corrector"]
    assert sorted(processor.chapter_calls) == ["c1", "c1", "c2", "c2"]

    d = _book_dir(tmp_path)
    assert (d / "chapter.cleaner.sqlite").is_file()
    assert (d / "chapter.corrector.sqlite").is_file()
    assert (d / "book_info.corrector.json").is_file()

    with ChapterStorage(d, "chapter.corrector.sqlite") as store:
        c1 = store.get_chapter("c1")
        assert c1 is not None
        assert c1["content"] == "raw c1-done-done"

    meta = json.loads((d / "pipeline.json").read_text(encoding="utf-8"))
    assert meta["pipeline"] == ["cleaner", "corrector"]
    assert set(meta["executed"].keys()) == {"cleaner", "corrector"}
    assert meta["executed"]["corrector"]["depends_on"] == ["cleaner"]
    assert len(meta["executed"]["cleaner"]["config_hash"]) == 12


def test_process_book_missing_raw_data_reports_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = FakeProcessor()
    monkeypatch.setattr(process_mod, "registrar", FakeRegistry(processor))

    client = _client(tmp_path)
    ui = FakeUI()
    book = BookConfig(book_id=BOOK_ID)

    client.process_book(book, [ProcessorConfig(name="cleaner")], ui=ui)

    assert processor.book_info_calls == 0
    assert len(ui.missing) == 1
    kind, path = ui.missing[0]
    assert kind == "raw"
    assert path.endswith("chapter.raw.sqlite")
    assert ui.started == []


def test_process_book_stage_failure_is_contained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_raw(tmp_path, [("c1", False)])
    processor = FakeProcessor(fail_on={"c1"})
    monkeypatch.setattr(process_mod, "registrar", FakeRegistry(processor))

    client = _client(tmp_path)
    ui = FakeUI()
    book = BookConfig(book_id=BOOK_ID)

    with caplog.at_level(logging.WARNING):
        client.process_book(
            book,
            [ProcessorConfig(name="cleaner"), ProcessorConfig(name="corrector")],
            ui=ui,
        )

    # The failing stage is logged, its completion event never fires and the
    # remaining stages do not run.
    assert ui.started == ["cleaner"]
    assert ui.completed == []
    assert any("cleaner" in rec.message for rec in caplog.records)
    assert not (_book_dir(tmp_path) / "chapter.corrector.sqlite").exists()


def test_process_book_without_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_raw(tmp_path, [("c1", False)])
    monkeypatch.setattr(process_mod, "registrar", FakeRegistry(FakeProcessor()))

    client = _client(tmp_path)
    client.process_book(BookConfig(book_id=BOOK_ID), [ProcessorConfig(name="cleaner")])
    assert (_book_dir(tmp_path) / "chapter.cleaner.sqlite").is_file()


# --------------------------------------------------------------------------- #
# incremental reuse
# --------------------------------------------------------------------------- #


def test_incremental_mode_reuses_clean_downstream_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_raw(tmp_path, [("c1", False), ("c2", False)])

    d = _book_dir(tmp_path)
    with ChapterStorage(d, "chapter.cleaner.sqlite") as outstore:
        # c1 was already processed cleanly in an earlier run.
        outstore.upsert_chapter(
            {"id": "c1", "title": "T c1", "content": "cleaned c1", "extra": {}},
            need_refetch=False,
        )

    # Pipeline metadata records a matching previous cleaner execution.
    processor = FakeProcessor()
    registry = FakeRegistry(processor)
    monkeypatch.setattr(process_mod, "registrar", registry)

    client = _client(tmp_path)
    client._save_book_info(BOOK_ID, _book_info())
    book = BookConfig(book_id=BOOK_ID)
    pconf = ProcessorConfig(name="cleaner", options={"x": 1})
    client._pc_init_pipeline(BOOK_ID, [pconf])
    client._pc_record_execution(BOOK_ID, pconf, [])

    ui = FakeUI()
    client.process_book(book, [pconf], ui=ui)

    # Only c2 is re-processed; c1 is reused from the previous output.
    assert processor.chapter_calls == ["c2"]
    with ChapterStorage(d, "chapter.cleaner.sqlite") as store:
        c1 = store.get_chapter("c1")
        assert c1 is not None
        assert c1["content"] == "cleaned c1"


def test_incremental_reprocesses_when_upstream_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_raw(tmp_path, [("c1", True), ("c2", False)])

    d = _book_dir(tmp_path)
    with ChapterStorage(d, "chapter.cleaner.sqlite") as outstore:
        outstore.upsert_chapter(
            {"id": "c1", "title": "T c1", "content": "stale", "extra": {}},
            need_refetch=False,
        )

    processor = FakeProcessor()
    monkeypatch.setattr(process_mod, "registrar", FakeRegistry(processor))

    client = _client(tmp_path)
    client._save_book_info(BOOK_ID, _book_info())
    book = BookConfig(book_id=BOOK_ID)
    pconf = ProcessorConfig(name="cleaner", options={"x": 1})
    client._pc_init_pipeline(BOOK_ID, [pconf])
    client._pc_record_execution(BOOK_ID, pconf, [])

    client.process_book(book, [pconf])

    # c1 is dirty upstream, so it must not be reused.
    assert sorted(processor.chapter_calls) == ["c1", "c2"]


# --------------------------------------------------------------------------- #
# _pc_is_incremental decision table
# --------------------------------------------------------------------------- #


def _prepare_incremental(
    tmp_path: Path, client: CommonClient, options: dict[str, Any]
) -> None:
    pconf = ProcessorConfig(name="cleaner", options=options)
    client._pc_init_pipeline(BOOK_ID, [pconf])
    client._pc_record_execution(BOOK_ID, pconf, [])
    (_book_dir(tmp_path) / "chapter.cleaner.sqlite").touch()


def test_is_incremental_decision_table(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._save_book_info(BOOK_ID, _book_info())

    same = ProcessorConfig(name="cleaner", options={"x": 1})
    changed = ProcessorConfig(name="cleaner", options={"x": 2})

    # No record at all -> not incremental.
    assert not client._pc_is_incremental(BOOK_ID, same, [])

    _prepare_incremental(tmp_path, client, {"x": 1})

    # Everything matches.
    assert client._pc_is_incremental(BOOK_ID, same, [])
    # Overwrite requested -> never incremental.
    overwrite = ProcessorConfig(name="cleaner", overwrite=True, options={"x": 1})
    assert not client._pc_is_incremental(BOOK_ID, overwrite, [])
    # Config changed -> hash mismatch.
    assert not client._pc_is_incremental(BOOK_ID, changed, [])
    # Dependency chain changed.
    assert not client._pc_is_incremental(BOOK_ID, same, ["other"])
    # Output file removed.
    (_book_dir(tmp_path) / "chapter.cleaner.sqlite").unlink()
    assert not client._pc_is_incremental(BOOK_ID, same, [])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_hash_config_is_order_insensitive() -> None:
    assert CommonClient._pc_hash_config(
        {"a": 1, "b": [1, 2]}
    ) == CommonClient._pc_hash_config({"b": [1, 2], "a": 1})
    assert len(CommonClient._pc_hash_config({"a": 1})) == 12


def test_utc_now_iso_format() -> None:
    stamp = CommonClient._utc_now_iso()
    assert len(stamp) == 20
    assert stamp.endswith("Z")
    assert stamp[4] == "-" and stamp[7] == "-" and stamp[10] == "T"


def test_init_pipeline_and_record_execution(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    client._save_book_info(BOOK_ID, _book_info())

    pconf = ProcessorConfig(name="cleaner", options={"k": "v"})
    client._pc_init_pipeline(BOOK_ID, [pconf])
    client._pc_record_execution(BOOK_ID, pconf, ["prev"])

    meta = client._load_pipeline_meta(BOOK_ID)
    assert meta["pipeline"] == ["cleaner"]
    rec = meta["executed"]["cleaner"]
    assert rec["depends_on"] == ["prev"]
    assert rec["config_hash"] == CommonClient._pc_hash_config({"k": "v"})
    assert rec["processed_at"].endswith("Z")


def test_stage_input_missing_raises_file_not_found(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    book = BookConfig(book_id=BOOK_ID)
    info = _book_info()

    # "chapter.ghost.sqlite" does not exist -> the stage must fail loudly.
    with pytest.raises(FileNotFoundError, match="Upstream stage output missing"):
        client._pc_run_stage(
            book,
            info,
            ProcessorConfig(name="cleaner"),
            ["c1"],
            {"c1"},
            "chapter.ghost.sqlite",
            [],
            None,
        )


def test_need_refetch_flag_is_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_raw(tmp_path, [("c1", True), ("c2", False)])
    monkeypatch.setattr(process_mod, "registrar", FakeRegistry(FakeProcessor()))

    client = _client(tmp_path)
    client.process_book(BookConfig(book_id=BOOK_ID), [ProcessorConfig(name="cleaner")])

    d = _book_dir(tmp_path)
    with ChapterStorage(d, "chapter.cleaner.sqlite") as store:
        assert store.need_refetch("c1") is True
        assert store.need_refetch("c2") is False
