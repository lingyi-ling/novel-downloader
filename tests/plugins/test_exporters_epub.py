#!/usr/bin/env python3
"""Offline tests for the EPUB exporter (``plugins/mixins/export_epub.py``)."""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import xml.etree.ElementTree as ET
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


# ---------------------------------------------------------------------------
# Sanitization collisions & robustness (chapter ids that sanitize alike)
# ---------------------------------------------------------------------------


def test_export_book_epub_sanitized_cid_collision_keeps_both(
    scenario: Scenario,
) -> None:
    # "a/b" and "a_b" both sanitize to "ca_b": the second chapter must not
    # silently overwrite the first inside the archive.
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume(
                    "卷",
                    [make_chapter_info("a/b", "章A"), make_chapter_info("a_b", "章B")],
                )
            ]
        )
    )
    scenario.write_chapters(
        [
            make_chapter("a/b", "内容A", title="章A"),
            make_chapter("a_b", "内容B", title="章B"),
        ]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names))
        chapters = [n for n in names if n.startswith("OEBPS/Text/ca_b")]
        assert len(chapters) == 2
        bodies = "\n".join(zf.read(n).decode("utf-8") for n in chapters)
        assert "内容A" in bodies
        assert "内容B" in bodies


def test_xp_epub_chapter_tolerates_none_title_and_content(
    scenario: Scenario,
) -> None:
    # A chapter dict whose title/content are None (e.g. hand-written JSON
    # metadata) must render a placeholder, not raise AttributeError.
    chapter: dict[str, object] = {
        "id": "1",
        "title": None,
        "content": None,
        "extra": {},
    }

    chapter_obj = scenario.client._xp_epub_chapter(
        book=None,  # type: ignore[arg-type]
        cid="1",
        chap_title=None,
        chap=chapter,  # type: ignore[arg-type]
        media_dir=scenario.output_dir,
    )

    assert chapter_obj.filename == "c1.xhtml"
    assert chapter_obj.title == ""
    assert chapter_obj.content == ""


# ---------------------------------------------------------------------------
# Table of contents: volume -> chapter hierarchy, NCX + nav
# ---------------------------------------------------------------------------

XHTML_NS = "http://www.w3.org/1999/xhtml"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"


def _nav_top_items(nav_xml: bytes) -> list[ET.Element]:
    root = ET.fromstring(nav_xml)
    nav = root.find(f".//{{{XHTML_NS}}}nav")
    assert nav is not None
    top = nav.find(f"{{{XHTML_NS}}}ol")
    assert top is not None
    return list(top.findall(f"{{{XHTML_NS}}}li"))


def _li_label(li: ET.Element) -> str:
    link = li.find(f"{{{XHTML_NS}}}a")
    assert link is not None
    return "".join(link.itertext())


def _li_children(li: ET.Element) -> list[ET.Element]:
    child_ol = li.find(f"{{{XHTML_NS}}}ol")
    if child_ol is None:
        return []
    return list(child_ol.findall(f"{{{XHTML_NS}}}li"))


def test_export_book_epub_nav_has_volume_hierarchy(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)
    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]

    with zipfile.ZipFile(path) as zf:
        items = _nav_top_items(zf.read("OEBPS/nav.xhtml"))

    # intro + two volumes at the top level.
    assert [_li_label(li) for li in items] == ["书籍简介", "第一卷", "第二卷"]
    assert [_li_label(c) for c in _li_children(items[1])] == ["第一章", "第二章"]
    assert [_li_label(c) for c in _li_children(items[2])] == ["第三章"]


def test_export_book_epub_ncx_has_volume_hierarchy(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)
    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]

    with zipfile.ZipFile(path) as zf:
        assert "OEBPS/toc.ncx" in zf.namelist()
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
        ncx = ET.fromstring(zf.read("OEBPS/toc.ncx"))

    # Older readers only understand the NCX: the spine must point at it.
    assert 'toc="ncx"' in opf
    assert 'media-type="application/x-dtbncx+xml"' in opf

    nav_map = ncx.find(f"{{{NCX_NS}}}navMap")
    assert nav_map is not None
    top = list(nav_map.findall(f"{{{NCX_NS}}}navPoint"))
    assert len(top) == 3  # intro + 2 volumes

    labels = [point.findtext(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text") for point in top]
    assert labels == ["书籍简介", "第一卷", "第二卷"]

    first_volume_children = list(top[1].findall(f"{{{NCX_NS}}}navPoint"))
    assert len(first_volume_children) == 2
    # playOrder is a single, strictly increasing sequence across the tree.
    orders = [int(p.get("playOrder", "0")) for p in ncx.iter(f"{{{NCX_NS}}}navPoint")]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)


def test_export_book_epub_empty_volume_is_a_leaf(scenario: Scenario) -> None:
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume("空卷", []),
                make_volume("实卷", [make_chapter_info("1", "第一章")]),
            ]
        )
    )
    scenario.write_chapters([make_chapter("1", "内容", title="第一章")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        nav_items = _nav_top_items(zf.read("OEBPS/nav.xhtml"))
        ncx = ET.fromstring(zf.read("OEBPS/toc.ncx"))

    empty = nav_items[1]
    assert _li_label(empty) == "空卷"
    # No empty <ol> that strict EPUB validators reject.
    assert _li_children(empty) == []
    raw_nav = ET.tostring(empty, encoding="unicode")
    assert "<ol" not in raw_nav

    nav_map = ncx.find(f"{{{NCX_NS}}}navMap")
    assert nav_map is not None
    empty_point = list(nav_map.findall(f"{{{NCX_NS}}}navPoint"))[1]
    assert empty_point.find(f"{{{NCX_NS}}}navPoint") is None


# ---------------------------------------------------------------------------
# Chapter name safety inside the archive
# ---------------------------------------------------------------------------


def test_export_book_epub_long_cid_is_truncated(scenario: Scenario) -> None:
    cid = "9" * 400
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info(cid, "长id")])])
    )
    scenario.write_chapters([make_chapter(cid, "长id内容", title="长id")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        chapters = [name for name in zf.namelist() if name.startswith("OEBPS/Text/c")]
        assert len(chapters) == 1
        # Bounded so extracting the archive on Windows cannot exceed the
        # 255-character component limit.
        assert len(Path(chapters[0]).name) <= 72
        assert "长id内容" in zf.read(chapters[0]).decode("utf-8")


# ---------------------------------------------------------------------------
# Consistency diagnostics
# ---------------------------------------------------------------------------


def test_epub_consistency_mismatch_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from novel_downloader.plugins.mixins.export_epub import _check_export_consistency

    with caplog.at_level(logging.WARNING):
        _check_export_consistency(
            fmt="EPUB",
            site="site",
            book_id="b1",
            planned=3,
            rendered=2,
            skipped=0,
        )

    assert any(
        "planned=3" in record.getMessage() and "rendered=2" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_export_book_epub_preserves_book_order(scenario: Scenario) -> None:
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

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        items = _nav_top_items(zf.read("OEBPS/nav.xhtml"))

    assert [_li_label(c) for c in _li_children(items[1])] == [
        "第十章",
        "第二章",
        "第一章",
    ]


# ---------------------------------------------------------------------------
# Atomic writes: degradation paths
# ---------------------------------------------------------------------------


def test_epub_export_replace_failure_keeps_previous_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "book.epub"
    good = EpubBuilder(title="Good")
    good.add_chapter(EpubChapter(id="c1", filename="c1.xhtml", title="ok"))
    good.export(out)
    before = out.read_bytes()

    def no_replace(src: object, dst: object) -> None:
        raise PermissionError("target locked")

    monkeypatch.setattr(os, "replace", no_replace)

    builder = EpubBuilder(title="书")
    builder.add_chapter(EpubChapter(id="c2", filename="c2.xhtml", title="new"))

    with pytest.raises(OSError) as excinfo:
        builder.export(out)

    # Writing directly would have truncated the good archive; the export must
    # fail instead and keep the previous file byte-for-byte.
    assert "Atomic replace failed" in str(excinfo.value)
    assert str(out) in str(excinfo.value)
    assert out.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_epub_export_temp_file_failure_raises_with_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "book.epub"
    out.write_bytes(b"EXISTING")

    def no_temp(*args: object, **kwargs: object) -> None:
        raise PermissionError("no temp space")

    monkeypatch.setattr(tempfile, "mkstemp", no_temp)

    builder = EpubBuilder(title="书")
    builder.add_chapter(EpubChapter(id="c1", filename="c1.xhtml", title="ok"))

    with pytest.raises(OSError) as excinfo:
        builder.export(out)

    # The error must be diagnosable even though the temp file was never
    # created, and the existing archive must not be touched.
    assert str(out) in str(excinfo.value)
    assert "temporary file" in str(excinfo.value)
    assert out.read_bytes() == b"EXISTING"
    assert list(tmp_path.glob("*.tmp")) == []
