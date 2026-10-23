#!/usr/bin/env python3
"""Offline tests for the EPUB exporter (``plugins/mixins/export_epub.py``)."""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path

import pytest

from novel_downloader.libs.epub_builder import EpubBuilder, EpubChapter
from novel_downloader.libs.filesystem import image_filename
from novel_downloader.schemas import ExporterConfig
from tests.plugins.exporters_helpers import (
    PNG_BYTES,
    Scenario,
    make_book_info,
    make_chapter,
    make_chapter_info,
    make_volume,
)


def _stage_two_volume_book(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            name="我的书",
            author="张三",
            summary="这是简介",
            tags=["玄幻"],
            volumes=[
                make_volume(
                    "第一卷",
                    [
                        make_chapter_info("1", "第一章"),
                        make_chapter_info("2", "第二章"),
                    ],
                ),
                make_volume("第二卷", [make_chapter_info("3", "第三章")]),
            ],
        )
    )
    scenario.write_chapters(
        [
            make_chapter("1", "内容一", title="第一章"),
            make_chapter("2", "内容二", title="第二章"),
            make_chapter("3", "内容三", title="第三章"),
        ]
    )


def _open_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


# ---------------------------------------------------------------------------
# Normal export / format validity
# ---------------------------------------------------------------------------


def test_export_book_epub_is_valid_zip(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)

    result = scenario.export("epub", ExporterConfig(append_timestamp=False))
    assert len(result["epub"]) == 1
    path = result["epub"][0]
    assert path.exists() and path.suffix == ".epub"

    names = _open_names(path)
    # Mandatory EPUB members.
    assert "mimetype" in names
    assert "META-INF/container.xml" in names
    assert "OEBPS/content.opf" in names
    assert "OEBPS/nav.xhtml" in names
    assert "OEBPS/toc.ncx" in names
    # Chapter documents.
    assert "OEBPS/Text/c1.xhtml" in names
    assert "OEBPS/Text/c2.xhtml" in names
    assert "OEBPS/Text/c3.xhtml" in names


def test_export_book_epub_mimetype_first_and_stored(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)
    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]

    with zipfile.ZipFile(path) as zf:
        assert zf.namelist()[0] == "mimetype"
        info = zf.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"


def test_export_book_epub_metadata_in_opf(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)
    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]

    with zipfile.ZipFile(path) as zf:
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")

    assert "<dc:title>我的书</dc:title>" in opf
    assert '<dc:creator id="creator">张三</dc:creator>' in opf
    assert "<dc:language>zh-Hans</dc:language>" in opf
    assert "第一章" in chapter
    assert "<p>内容一</p>" in chapter


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_export_book_epub_without_volumes_returns_empty(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info(volumes=[]))
    scenario.write_chapters([])

    assert scenario.export("epub", ExporterConfig())["epub"] == []


def test_export_book_epub_duplicate_cid_is_deduplicated(scenario: Scenario) -> None:
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

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    names = _open_names(path)
    assert names.count("OEBPS/Text/c1.xhtml") == 1
    assert len(names) == len(set(names))


def test_export_book_epub_duplicate_title_kept(scenario: Scenario) -> None:
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

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    names = _open_names(path)
    assert "OEBPS/Text/c1.xhtml" in names
    assert "OEBPS/Text/c2.xhtml" in names


def test_export_book_epub_illegal_cid_stays_flat(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            volumes=[make_volume("卷", [make_chapter_info("a/b", "斜杠章")])]
        )
    )
    scenario.write_chapters([make_chapter("a/b", "内容", title="斜杠章")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    names = _open_names(path)

    assert "OEBPS/Text/ca_b.xhtml" in names
    # No chapter entry may introduce an extra directory level.
    text_entries = [n for n in names if n.startswith("OEBPS/Text/")]
    assert all(n.count("/") == 2 for n in text_entries)


def test_export_book_epub_missing_chapter_placeholder(scenario: Scenario) -> None:
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

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        assert "OEBPS/Text/c2.xhtml" in zf.namelist()
        assert "本章内容暂不可用" in zf.read("OEBPS/Text/c2.xhtml").decode("utf-8")


def test_export_book_epub_empty_content_chapter(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "空章")])])
    )
    scenario.write_chapters([make_chapter("1", "   ", title="空章")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
    assert "空章" in chapter


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def test_export_book_epub_includes_base64_image(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "图章")])])
    )
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mime": "image/png",
        "alt": "插图",
    }
    scenario.write_chapters(
        [make_chapter("1", "正文", title="图章", extra={"resources": [resource]})]
    )

    path = scenario.export(
        "epub", ExporterConfig(append_timestamp=False, include_picture=True)
    )["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
    assert "OEBPS/Images/img_0.png" in names
    assert '<img src="../Images/img_0.png"' in chapter

    # include_picture=False -> no image is embedded.
    path2 = scenario.export(
        "epub", ExporterConfig(append_timestamp=False, include_picture=False)
    )["epub"][0]
    assert not [n for n in _open_names(path2) if n.startswith("OEBPS/Images/")]


def test_export_book_epub_includes_url_image(scenario: Scenario) -> None:
    url = "https://example.com/pic/1.png"
    scenario.write_media(image_filename(url), PNG_BYTES)
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "图章")])])
    )
    resource = {"type": "image", "paragraph_index": 0, "url": url, "alt": "图"}
    scenario.write_chapters(
        [make_chapter("1", "正文", title="图章", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    assert "OEBPS/Images/img_0.png" in _open_names(path)


def test_export_book_epub_cover(scenario: Scenario) -> None:
    scenario.write_media("cover.png", PNG_BYTES)
    scenario.write_book_info(
        make_book_info(
            cover_url="https://example.com/cover.png",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
    assert "OEBPS/Images/cover.png" in names
    assert 'properties="cover-image"' in opf


# ---------------------------------------------------------------------------
# Split modes and naming
# ---------------------------------------------------------------------------


def test_export_volume_epub_split(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)

    result = scenario.export(
        "epub", ExporterConfig(append_timestamp=False, split_mode="volume")
    )
    paths = result["epub"]
    assert len(paths) == 2
    assert {p.name for p in paths} == {"第一卷_张三.epub", "第二卷_张三.epub"}
    for path in paths:
        assert path.exists()
        assert "mimetype" in _open_names(path)


def test_export_volume_epub_name_collision_keeps_all(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            author="张三",
            volumes=[
                make_volume("正文", [make_chapter_info("1", "一")]),
                make_volume("正文", [make_chapter_info("2", "二")]),
            ],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容一"), make_chapter("2", "内容二")])

    paths = scenario.export(
        "epub", ExporterConfig(append_timestamp=False, split_mode="volume")
    )["epub"]

    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert {p.name for p in paths} == {"正文_张三.epub", "正文_张三_2.epub"}
    for path in paths:
        assert "mimetype" in _open_names(path)


def test_export_book_epub_filename_template(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)

    path = scenario.export(
        "epub",
        ExporterConfig(filename_template="{author}__{title}", append_timestamp=False),
    )["epub"][0]
    assert path.name == "张三__我的书.epub"


# ---------------------------------------------------------------------------
# Idempotency / overwrite safety
# ---------------------------------------------------------------------------


def test_export_book_epub_repeated_export_is_clean(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)
    cfg = ExporterConfig(append_timestamp=False)

    first = scenario.export("epub", cfg)["epub"][0]
    second = scenario.export("epub", cfg)["epub"][0]

    assert first == second
    names = _open_names(second)
    assert len(names) == len(set(names))
    leftovers = [p for p in scenario.output_dir.iterdir() if p.name != first.name]
    assert leftovers == []


def test_epub_export_failure_preserves_previous_archive(tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    good = EpubBuilder(title="Good")
    good.add_chapter(EpubChapter(id="c1", filename="c1.xhtml", title="ok"))
    good.export(out)
    before = out.read_bytes()

    class BoomChapter(EpubChapter):
        def to_xhtml(self, lang: str = "zh-Hans") -> str:
            raise RuntimeError("boom")

    bad = EpubBuilder(title="Bad")
    bad.add_chapter(BoomChapter(id="c2", filename="c2.xhtml", title="bad"))

    with pytest.raises(RuntimeError):
        bad.export(out)

    # The previously exported archive must be untouched and no temp leaks.
    assert out.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Single-chapter export
# ---------------------------------------------------------------------------


def test_export_chapter_epub(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "单章内容", title="第一章")])

    path = scenario.export_chapter("1", "epub", ExporterConfig(append_timestamp=False))[
        "epub"
    ]
    assert path is not None
    assert path.exists()
    names = _open_names(path)
    assert "OEBPS/Text/c1.xhtml" in names
    with zipfile.ZipFile(path) as zf:
        assert "单章内容" in zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
