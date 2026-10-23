#!/usr/bin/env python3
"""Offline tests for the HTML exporter (``plugins/mixins/export_html.py``)."""

from __future__ import annotations

import re

from novel_downloader.libs.filesystem import sanitize_filename
from novel_downloader.libs.html_builder import HtmlBuilder
from novel_downloader.schemas import ExporterConfig
from tests.plugins.exporters_helpers import (
    PNG_BYTES,
    Scenario,
    make_book_info,
    make_chapter,
    make_chapter_info,
    make_volume,
)


def _stage_book(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            name="我的书",
            author="张三",
            summary="这是简介",
            serial_status="连载",
            word_count="100",
            tags=["玄幻"],
            volumes=[
                make_volume(
                    "第一卷",
                    [
                        make_chapter_info("1", "第一章"),
                        make_chapter_info("2", "第二章"),
                    ],
                )
            ],
        )
    )
    scenario.write_chapters(
        [
            make_chapter("1", "内容一", title="第一章"),
            make_chapter("2", "内容二", title="第二章"),
        ]
    )


# ---------------------------------------------------------------------------
# Normal export / directory structure
# ---------------------------------------------------------------------------


def test_export_book_html_directory_structure(scenario: Scenario) -> None:
    _stage_book(scenario)

    result = scenario.export("html", ExporterConfig(append_timestamp=False))
    assert len(result["html"]) == 1
    folder = result["html"][0]
    assert folder.is_dir()
    assert folder == scenario.output_dir / "我的书_张三"

    assert (folder / "index.html").is_file()
    assert (folder / "css" / "index.css").is_file()
    assert (folder / "css" / "chapter.css").is_file()
    assert (folder / "js" / "main.js").is_file()
    assert (folder / "chapters" / "c1.html").is_file()
    assert (folder / "chapters" / "c2.html").is_file()


def test_export_book_html_index_and_links(scenario: Scenario) -> None:
    _stage_book(scenario)
    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]

    index = (folder / "index.html").read_text("utf-8")
    assert "<title>我的书</title>" in index
    assert "我的书" in index
    assert "张三" in index
    links = re.findall(r'href="(chapters/[^"]+)"', index)
    assert links == ["chapters/c1.html", "chapters/c2.html"]


def test_export_book_html_chapter_content_escaped(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "标题<&>")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文 <b>粗</b> & 符号", title="标题<&>")]
    )

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    chapter = (folder / "chapters" / "c1.html").read_text("utf-8")

    assert "标题&lt;&amp;&gt;" in chapter
    assert "&lt;b&gt;粗&lt;/b&gt;" in chapter
    assert "&amp; 符号" in chapter
    assert "<title>标题&lt;&amp;&gt;</title>" in chapter


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_export_book_html_without_volumes_returns_empty(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info(volumes=[]))
    scenario.write_chapters([])

    assert scenario.export("html", ExporterConfig())["html"] == []


def test_export_book_html_duplicate_cid_is_deduplicated(scenario: Scenario) -> None:
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
    scenario.write_chapters([make_chapter("1", "唯一内容")])

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    index = (folder / "index.html").read_text("utf-8")
    assert index.count('href="chapters/c1.html"') == 1
    assert sorted(p.name for p in (folder / "chapters").iterdir()) == ["c1.html"]


def test_export_book_html_duplicate_title_kept(scenario: Scenario) -> None:
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

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    index = (folder / "index.html").read_text("utf-8")
    assert index.count('href="chapters/c1.html"') == 1
    assert index.count('href="chapters/c2.html"') == 1


def test_export_book_html_illegal_cid_stays_flat(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("a/b", "斜杠")])])
    )
    scenario.write_chapters([make_chapter("a/b", "内容", title="斜杠")])

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    chap_dir = folder / "chapters"

    assert (chap_dir / "ca_b.html").is_file()
    # Only files directly under chapters/, no accidental sub-directory.
    assert all(p.is_file() for p in chap_dir.iterdir())


def test_export_book_html_illegal_book_name_sanitized(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            name='bad:name/with*illegal?"chars',
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    assert folder.name == sanitize_filename(folder.name)
    for ch in '<>:"/\\|?*':
        assert ch not in folder.name


def test_export_book_html_overlong_title_folder_truncated(
    scenario: Scenario, monkeypatch
) -> None:
    long_name = "长" * 400
    scenario.write_book_info(
        make_book_info(
            name=long_name,
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    # Skip real filesystem writes so the over-long name is inspected directly.
    for method in (
        "_prepare_output_dir",
        "_write_media",
        "_write_fonts",
        "_build_index",
        "_build_chapters",
    ):
        monkeypatch.setattr(HtmlBuilder, method, lambda self, d: None)

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    assert len(folder.name) <= 255
    assert len(folder.name) < len(long_name)


def test_export_book_html_missing_chapter_placeholder(scenario: Scenario) -> None:
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

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    missing = (folder / "chapters" / "c2.html").read_text("utf-8")
    assert "本章内容暂不可用" in missing


def test_export_book_html_cover_written(scenario: Scenario) -> None:
    scenario.write_media("cover.png", PNG_BYTES)
    scenario.write_book_info(
        make_book_info(
            cover_url="https://example.com/cover.png",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    assert (folder / "media" / "cover.png").is_file()


# ---------------------------------------------------------------------------
# Naming / idempotency / single-chapter export
# ---------------------------------------------------------------------------


def test_export_book_html_filename_template(scenario: Scenario) -> None:
    _stage_book(scenario)

    cfg = ExporterConfig(filename_template="{author}--{title}", append_timestamp=False)
    folder = scenario.export("html", cfg)["html"][0]
    assert folder.name == "张三--我的书"


def test_export_book_html_idempotent(scenario: Scenario) -> None:
    _stage_book(scenario)
    cfg = ExporterConfig(append_timestamp=False)

    first = scenario.export("html", cfg)["html"][0]
    second = scenario.export("html", cfg)["html"][0]

    assert first == second
    assert (second / "chapters" / "c1.html").is_file()
    leftovers = [p for p in scenario.output_dir.iterdir() if p != first]
    assert leftovers == []


def test_export_chapter_html(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "单章内容", title="第一章")])

    folder = scenario.export_chapter(
        "1", "html", ExporterConfig(append_timestamp=False)
    )["html"]
    assert folder is not None
    assert folder.is_dir()
    assert folder.name == "第一章_Unknown"
    assert (folder / "index.html").is_file()
    chapter = (folder / "chapters" / "c1.html").read_text("utf-8")
    assert "单章内容" in chapter


def test_export_chapter_html_missing_id_returns_none(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "内容")])

    assert scenario.export_chapter("nope", "html")["html"] is None
