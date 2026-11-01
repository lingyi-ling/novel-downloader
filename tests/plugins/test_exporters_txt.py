#!/usr/bin/env python3
"""Offline tests for the TXT exporter (``plugins/mixins/export_txt.py``)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from novel_downloader.libs.filesystem import sanitize_filename
from novel_downloader.schemas import ExporterConfig
from tests.plugins.exporters_helpers import (
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
    invalid = '<>:"/\|?*' if sys.platform == "win32" else "/"
    for ch in invalid:
        assert ch not in name


def test_export_book_txt_overlong_title_truncates_filename(
    scenario: Scenario, monkeypatch
) -> None:
    long_name = "长" * 400
    scenario.write_book_info(
        make_book_info(
            name=long_name,
            author="作者",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容", title="章")])

    captured: dict[str, Path] = {}

    def fake_write(content, filepath, **kwargs):  # type: ignore[no-untyped-def]
        captured["path"] = filepath
        return filepath

    monkeypatch.setattr(
        "novel_downloader.plugins.mixins.export_txt.write_file", fake_write
    )

    result = scenario.export("txt", ExporterConfig(append_timestamp=False))
    path = result["txt"][0]
    assert path == captured["path"]
    # Truncated to the 255-char sanitize limit, extension preserved.
    assert len(path.name) <= 255
    assert path.name.endswith(".txt")
    assert len(path.name) < len(long_name)


def test_export_book_txt_duplicate_cid_is_deduplicated(scenario: Scenario) -> None:
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
    invalid = '<>:"/\|?*' if sys.platform == "win32" else "/"
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
