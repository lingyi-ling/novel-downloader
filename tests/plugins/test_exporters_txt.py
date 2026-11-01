#!/usr/bin/env python3
"""Offline tests for the TXT exporter (``plugins/mixins/export_txt.py``)."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import pytest

from novel_downloader.libs.filesystem import sanitize_filename
from novel_downloader.schemas import ClientConfig, ExporterConfig
from tests.plugins.exporters_helpers import (
    HarnessClient,
    Scenario,
    make_book_info,
    make_chapter,
    make_chapter_info,
    make_volume,
)


def _stage_simple_book(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            name="我的书",
            author="张三",
            summary="这是简介",
            serial_status="连载",
            word_count="12.3万字",
            tags=["玄幻", "热血"],
            update_time="2025-01-02 03:04:05",
            volumes=[
                make_volume(
                    "第一卷 初入江湖",
                    [
                        make_chapter_info("1", "第一章 起点"),
                        make_chapter_info("2", "第二章 试炼"),
                    ],
                )
            ],
        )
    )
    scenario.write_chapters(
        [
            make_chapter("1", "第一段\n\n第二段", title="第一章 起点"),
            make_chapter("2", "内容二", title="第二章 试炼"),
        ]
    )


# ---------------------------------------------------------------------------
# Normal export
# ---------------------------------------------------------------------------


def test_export_book_txt_metadata_and_body(scenario: Scenario) -> None:
    _stage_simple_book(scenario)

    result = scenario.export("txt", ExporterConfig(append_timestamp=False))

    assert len(result["txt"]) == 1
    path = result["txt"][0]
    assert path.exists()
    assert path.suffix == ".txt"
    assert path.parent == scenario.output_dir

    text = path.read_text("utf-8")
    assert text.startswith("我的书\n")
    assert "作者：张三" in text
    assert "状态：连载" in text
    assert "字数：12.3万字" in text
    assert "标签：玄幻、热血" in text
    assert "更新：2025-01-02 03:04:05" in text
    assert "这是简介" in text
    assert "=== 第一卷 初入江湖 ===" in text
    assert "第一章 起点" in text
    assert "第一段" in text and "第二段" in text
    assert "第二章 试炼" in text

    # Ordering: header -> volume heading -> chapters.
    assert text.index("我的书") < text.index("=== 第一卷") < text.index("第一章 起点")


def test_export_book_txt_uses_lf_and_utf8(scenario: Scenario) -> None:
    _stage_simple_book(scenario)
    path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]

    raw = path.read_bytes()
    # Deterministic LF newlines regardless of platform.
    assert b"\r\n" not in raw
    assert b"\r" not in raw
    # UTF-8 round-trip.
    assert raw.decode("utf-8") == path.read_text("utf-8")


def test_export_book_txt_blank_lines_are_dropped(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "标题")])])
    )
    scenario.write_chapters([make_chapter("1", "  a  \n\n\n   b   \n", title="标题")])

    path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]
    text = path.read_text("utf-8")
    assert "a\nb" in text


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_export_book_txt_without_volumes_returns_empty(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info(volumes=[]))
    scenario.write_chapters([])

    assert scenario.export("txt", ExporterConfig())["txt"] == []


def test_export_book_txt_all_chapters_filtered_returns_empty(
    scenario: Scenario,
) -> None:
    _stage_simple_book(scenario)
    scenario.start_id = "999"  # points at a chapter that does not exist

    assert scenario.export("txt", ExporterConfig())["txt"] == []


def test_export_book_txt_single_chapter(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "独章")])])
    )
    scenario.write_chapters([make_chapter("1", "唯一内容", title="独章")])

    path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]
    text = path.read_text("utf-8")
    assert "独章" in text and "唯一内容" in text


def test_export_book_txt_illegal_book_name_is_sanitized(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            name='bad:name/with*illegal?"chars',
            author="作者",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容", title="章")])

    path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]
    name = path.name
    assert name == sanitize_filename(name)
    # sanitize_filename filters the platform-specific set only.
    invalid = r'<>:"/\|?*' if sys.platform == "win32" else "/"
    for ch in invalid:
        assert ch not in name


def test_export_book_txt_overlong_title_truncates_filename(
    scenario: Scenario,
) -> None:
    # A 400-character title used to overflow the Windows 260-character path
    # limit and abort the export entirely. The filename must now be shortened
    # so the file is really written (not just stubbed out in a monkeypatch).
    long_name = "长" * 400
    scenario.write_book_info(
        make_book_info(
            name=long_name,
            author="作者",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容", title="章")])

    result = scenario.export("txt", ExporterConfig(append_timestamp=False))
    assert len(result["txt"]) == 1
    path = result["txt"][0]

    assert path.exists()
    assert path.read_text("utf-8").startswith(long_name)
    # Truncated below the Windows limit, extension preserved.
    assert path.name.endswith(".txt")
    assert len(str(path)) <= 240
    assert len(path.name) < len(long_name)


def test_export_book_txt_deep_output_dir_stays_writable(
    tmp_path: Path,
) -> None:
    # The 240-character budget must account for the output directory itself:
    # with a 200-character deep destination the shortened name has to keep the
    # full path writeable on Windows (260-character limit).
    deep = tmp_path / ("d" * (200 - len(str(tmp_path)) - 1))
    assert len(str(deep)) == 200
    deep.mkdir(parents=True, exist_ok=True)

    raw = tmp_path / "raw"
    client = HarnessClient(
        "site", ClientConfig(raw_data_dir=str(raw), output_dir=str(deep))
    )
    sc = Scenario(client=client, raw_dir=raw / "site", output_dir=deep)
    long_name = "长" * 400
    sc.write_book_info(
        make_book_info(
            name=long_name,
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    sc.write_chapters([make_chapter("1", "内容", title="章")])

    result = sc.export("txt", ExporterConfig(append_timestamp=False))

    assert len(result["txt"]) == 1
    path = result["txt"][0]
    assert path.exists()
    assert len(str(path)) <= 240
    assert path.name.endswith(".txt")
    assert path.read_text("utf-8").startswith(long_name)


def test_export_book_txt_truncated_names_stay_distinct(
    scenario: Scenario,
) -> None:
    # Two 400-character titles that only differ at the end must not collapse
    # onto a single output file once both are truncated.
    head = "长" * 400

    names: list[str] = []
    for suffix in ("甲", "乙"):
        scenario.write_book_info(
            make_book_info(
                name=head + suffix,
                author="作者",
                volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
            )
        )
        scenario.write_chapters([make_chapter("1", "内容", title="章")])
        path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]
        names.append(path.name)

    assert names[0] != names[1]


def test_export_book_txt_preserves_book_order_over_numeric_name_order(
    scenario: Scenario,
) -> None:
    # Chapter ids sort differently as strings than the book does: the export
    # must follow the volume/chapter order in the metadata, never a filename
    # or id ordering.
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume(
                    "卷",
                    [
                        make_chapter_info("10", "第十章"),
                        make_chapter_info("2", "第二章"),
                        make_chapter_info("1", "第一章"),
                    ],
                )
            ]
        )
    )
    scenario.write_chapters(
        [
            make_chapter("10", "内容十", title="第十章"),
            make_chapter("2", "内容二", title="第二章"),
            make_chapter("1", "内容一", title="第一章"),
        ]
    )

    text = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][
        0
    ].read_text("utf-8")

    assert text.index("第十章") < text.index("第二章") < text.index("第一章")


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


def _tmp_leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob(".*.tmp"))


def test_txt_export_interrupted_write_keeps_previous_file(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _stage_simple_book(scenario)
    cfg = ExporterConfig(append_timestamp=False)
    target = scenario.export("txt", cfg)["txt"][0]
    before = target.read_bytes()

    # Change the book, then make the atomic writer fail after the temporary
    # file exists but before it can replace the old one.
    scenario.write_chapters([make_chapter("1", "新内容", title="第一章 起点")])

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "fsync", boom)

    with caplog.at_level(logging.WARNING):
        result = scenario.export("txt", cfg)

    # The export fails as a whole ...
    assert result["txt"] == []
    # ... but the previously exported file is untouched.
    assert target.read_bytes() == before
    # ... and no half-written temporary file is left behind.
    assert _tmp_leftovers(scenario.output_dir) == []


def test_txt_export_replace_failure_keeps_previous_file_and_raises(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _stage_simple_book(scenario)
    cfg = ExporterConfig(append_timestamp=False)
    target = scenario.export("txt", cfg)["txt"][0]
    before = target.read_bytes()

    # Change the book, then make the atomic rename impossible (locked target,
    # read-only filesystem, ...). Writing directly instead would truncate the
    # old export, so the writer must fail and leave it untouched.
    scenario.write_chapters([make_chapter("1", "新内容", title="第一章 起点")])

    def no_replace(src: object, dst: object) -> None:
        raise PermissionError("target locked")

    monkeypatch.setattr(os, "replace", no_replace)

    with caplog.at_level(logging.WARNING):
        result = scenario.export("txt", cfg)

    # The export reports failure ...
    assert result["txt"] == []
    # ... the previous artifact is byte-for-byte intact ...
    assert target.read_bytes() == before
    # ... no stray temporary file is left ...
    assert _tmp_leftovers(scenario.output_dir) == []
    # ... and the failure names the destination (diagnosable, not silent).
    messages = [record.getMessage() for record in caplog.records]
    assert any("Atomic replace failed" in message for message in messages)
    assert any(str(target) in message for message in messages)


def test_atomic_write_closes_fd_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If ``os.fdopen`` fails before taking ownership of the descriptor, the
    # raw fd must still be closed -- otherwise Windows refuses to delete the
    # temporary file and every failed export leaks a ``.tmp`` next to the
    # output.
    from novel_downloader.plugins.mixins import export_txt

    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")

    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("bad encoding")

    monkeypatch.setattr(export_txt.os, "fdopen", boom)

    with pytest.raises(ValueError):
        export_txt._atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_txt_export_duplicate_cid_is_deduplicated(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume(
                    "卷",
                    [
                        make_chapter_info("1", "重复章"),
                        make_chapter_info("1", "重复章"),
                    ],
                )
            ]
        )
    )
    scenario.write_chapters([make_chapter("1", "唯一内容", title="重复章")])

    path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]
    text = path.read_text("utf-8")
    assert text.count("重复章") == 1
    assert text.count("唯一内容") == 1


def test_export_book_txt_duplicate_title_kept(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume(
                    "卷",
                    [
                        make_chapter_info("1", "正文"),
                        make_chapter_info("2", "正文"),
                    ],
                )
            ]
        )
    )
    scenario.write_chapters(
        [
            make_chapter("1", "内容一", title="正文"),
            make_chapter("2", "内容二", title="正文"),
        ]
    )

    path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]
    text = path.read_text("utf-8")
    # Distinct chapters sharing a title must both survive.
    assert text.count("正文") == 2
    assert "内容一" in text and "内容二" in text


def test_export_book_txt_empty_content_chapter(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "空章")])])
    )
    scenario.write_chapters([make_chapter("1", "   ", title="空章")])

    path = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][0]
    text = path.read_text("utf-8")
    assert "空章" in text


def test_export_book_txt_missing_chapter_placeholder(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume(
                    "卷",
                    [
                        make_chapter_info("1", "有内容"),
                        make_chapter_info("2", "缺失章"),
                    ],
                )
            ]
        )
    )
    scenario.write_chapters([make_chapter("1", "内容", title="有内容")])

    # Placeholder enabled (default).
    text = scenario.export("txt", ExporterConfig(append_timestamp=False))["txt"][
        0
    ].read_text("utf-8")
    assert "本章内容暂不可用" in text

    # Placeholder disabled -> the missing chapter is simply omitted.
    text2 = scenario.export(
        "txt", ExporterConfig(append_timestamp=False, render_missing_chapter=False)
    )["txt"][0].read_text("utf-8")
    assert "本章内容暂不可用" not in text2


# ---------------------------------------------------------------------------
# Naming configuration / idempotency
# ---------------------------------------------------------------------------


def test_export_book_txt_filename_template(scenario: Scenario) -> None:
    _stage_simple_book(scenario)

    path = scenario.export(
        "txt",
        ExporterConfig(
            filename_template="{author}-{title}-{unknown}", append_timestamp=False
        ),
    )["txt"][0]
    # Unknown placeholders are kept verbatim (SafeDict behaviour).
    assert path.name == "张三-我的书-{unknown}.txt"


def test_export_book_txt_append_timestamp(scenario: Scenario) -> None:
    _stage_simple_book(scenario)

    path = scenario.export("txt", ExporterConfig(append_timestamp=True))["txt"][0]
    assert re.search(r"_\d{8}_\d{6}\.txt$", path.name)


def test_export_book_txt_idempotent_overwrite(scenario: Scenario) -> None:
    _stage_simple_book(scenario)
    cfg = ExporterConfig(append_timestamp=False)

    first = scenario.export("txt", cfg)["txt"][0]
    before = first.read_bytes()
    second = scenario.export("txt", cfg)["txt"][0]

    assert first == second
    assert second.read_bytes() == before
    # No stray temporary files left behind by the atomic writer.
    leftovers = [p for p in scenario.output_dir.iterdir() if p.name != first.name]
    assert leftovers == []


def test_txt_consistency_mismatch_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A silently dropped chapter must not be silent: the helpers only have the
    # log channel (the export API returns paths, not diagnostics), so assert a
    # warning really reaches the configured handlers.
    from novel_downloader.plugins.mixins.export_txt import _check_export_consistency

    with caplog.at_level(logging.WARNING):
        _check_export_consistency(
            fmt="TXT",
            site="site",
            book_id="b1",
            planned=3,
            rendered=2,
            skipped=0,
        )

    assert any(
        "planned=3" in record.getMessage()
        and "rendered=2" in record.getMessage()
        and "TXT" in record.getMessage()
        for record in caplog.records
    )


def test_txt_consistent_counts_do_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from novel_downloader.plugins.mixins.export_txt import _check_export_consistency

    with caplog.at_level(logging.WARNING):
        _check_export_consistency(
            fmt="TXT",
            site="site",
            book_id="b1",
            planned=3,
            rendered=2,
            skipped=1,
        )

    assert not [r for r in caplog.records if "consistency" in r.getMessage().lower()]


# ---------------------------------------------------------------------------
# Single-chapter export
# ---------------------------------------------------------------------------


def test_export_chapter_txt(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "单章内容", title="第一章")])

    result = scenario.export_chapter("1", "txt", ExporterConfig(append_timestamp=False))
    path = result["txt"]
    assert path is not None
    assert path.exists()
    assert "单章内容" in path.read_text("utf-8")
    assert path.name == "第一章_Unknown.txt"


def test_export_chapter_txt_illegal_title_sanitized(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "内容", title="a/b:c*d?e")])

    result = scenario.export_chapter("1", "txt", ExporterConfig(append_timestamp=False))
    path = result["txt"]
    assert path is not None
    assert path.name == sanitize_filename("a/b:c*d?e_Unknown.txt")
    # sanitize_filename filters the platform-specific set only.
    invalid = r'<>:"/\|?*' if sys.platform == "win32" else "/"
    for ch in invalid:
        assert ch not in path.name


def test_export_chapter_txt_missing_id_returns_none(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "内容")])

    assert scenario.export_chapter("does-not-exist", "txt")["txt"] is None


# ---------------------------------------------------------------------------
# Robustness: chapter dicts with None fields
# ---------------------------------------------------------------------------


def test_xp_txt_chapter_tolerates_none_title_and_content(
    scenario: Scenario,
) -> None:
    chapter: dict[str, object] = {
        "id": "1",
        "title": None,
        "content": None,
        "extra": {},
    }

    text = scenario.client._xp_txt_chapter(None, chapter)  # type: ignore[arg-type]

    assert text == "\n\n\n\n"
