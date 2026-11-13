#!/usr/bin/env python3
"""Offline tests for the EPUB exporter (``plugins/mixins/export_epub.py``)."""

from __future__ import annotations

import base64
import logging
import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from novel_downloader.libs.epub_builder import EpubBuilder, EpubChapter
from novel_downloader.libs.filesystem import image_filename
from novel_downloader.plugins.mixins import export_epub as export_epub_module
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


def test_export_book_epub_includes_protocol_relative_image(
    scenario: Scenario,
) -> None:
    # The downloader upgrades ``//host/x`` to ``https://host/x`` before hashing
    # the cache filename; the exporter must use the same form or the download
    # is invisible to it.
    url = "//cdn.example.com/pic/1.png"
    scenario.write_media(image_filename("https://cdn.example.com/pic/1.png"), PNG_BYTES)
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "图章")])])
    )
    resource = {"type": "image", "paragraph_index": 0, "url": url, "alt": "图"}
    scenario.write_chapters(
        [make_chapter("1", "正文", title="图章", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/img_0.png" in names
    assert '<img src="../Images/img_0.png"' in chapter


@pytest.mark.parametrize(
    "url",
    [
        "/uploads/pic.png",
        "./images/pic.png",
        "../images/pic.png",
    ],
)
def test_export_book_epub_includes_relative_image(scenario: Scenario, url: str) -> None:
    scenario.write_media(image_filename(url), PNG_BYTES)
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "图章")])])
    )
    resource = {"type": "image", "paragraph_index": 0, "url": url}
    scenario.write_chapters(
        [make_chapter("1", "正文", title="图章", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/img_0.png" in names
    assert '<img src="../Images/img_0.png"' in chapter


def test_export_book_epub_missing_relative_image_leaves_no_reference(
    scenario: Scenario,
) -> None:
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "图章")])])
    )
    resources = [
        {"type": "image", "paragraph_index": 0, "url": "/uploads/gone.png"},
        {"type": "image", "paragraph_index": 0, "url": "../gone.png"},
        {"type": "image", "paragraph_index": 0, "url": "//cdn.example.com/gone.png"},
    ]
    scenario.write_chapters(
        [make_chapter("1", "正文", title="图章", extra={"resources": resources})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert not [n for n in names if n.startswith("OEBPS/Images/")]
    assert "<img" not in chapter
    assert "正文" in chapter


def test_export_book_epub_invalid_image_references_are_omitted(
    scenario: Scenario,
) -> None:
    # A reference with an unterminated IPv6 host cannot be parsed, and an
    # unsupported scheme can never have been downloaded. Both must be dropped
    # without breaking the rest of the chapter.
    scenario.write_media(image_filename("ftp://example.com/pic.png"), PNG_BYTES)
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "图章")])])
    )
    resources = [
        {"type": "image", "paragraph_index": 0, "url": "https://[bad/x.png"},
        {"type": "image", "paragraph_index": 0, "url": "ftp://example.com/pic.png"},
        {
            "type": "image",
            "paragraph_index": 0,
            "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "mime": "image/png",
        },
    ]
    scenario.write_chapters(
        [make_chapter("1", "正文", title="图章", extra={"resources": resources})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    # Only the inline payload is embedded; the rejected URLs leave no mark.
    assert [n for n in names if n.startswith("OEBPS/Images/")] == [
        "OEBPS/Images/img_0.png"
    ]
    assert chapter.count("<img") == 1
    assert "正文" in chapter


def test_export_book_epub_protocol_relative_volume_cover(
    scenario: Scenario,
) -> None:
    # Volume covers are cached without an explicit name, so a protocol-relative
    # one is hashed in its upgraded form by the downloader.
    url = "//cdn.example.com/v1.png"
    scenario.write_media(image_filename("https://cdn.example.com/v1.png"), PNG_BYTES)
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume(
                    "第一卷",
                    [make_chapter_info("1", "章")],
                    volume_cover=url,
                )
            ]
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        _assert_image_links_resolve(zf)

    assert [n for n in names if n.startswith("OEBPS/Images/")]


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


# ---------------------------------------------------------------------------
# Filename safety: case-insensitive collisions and traversal
# ---------------------------------------------------------------------------

DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


def _opf_title(zf: zipfile.ZipFile) -> str:
    opf = ET.fromstring(zf.read("OEBPS/content.opf"))
    node = opf.find(f".//{{{DC_NS}}}title")
    assert node is not None
    return node.text or ""


def test_export_book_epub_case_insensitive_cid_collision_keeps_both(
    scenario: Scenario,
) -> None:
    # "A" and "a" produce archive entries that differ only by case. The ZIP
    # itself is case-sensitive, but extracting it on Windows/macOS overwrites
    # one chapter with the other, so the export must keep the names distinct.
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume(
                    "卷",
                    [
                        make_chapter_info("A", "大写章"),
                        make_chapter_info("a", "小写章"),
                    ],
                )
            ]
        )
    )
    scenario.write_chapters(
        [
            make_chapter("A", "大写内容", title="大写章"),
            make_chapter("a", "小写内容", title="小写章"),
        ]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        chapters = [n for n in zf.namelist() if n.startswith("OEBPS/Text/c")]
        assert len(chapters) == 2
        assert len({name.casefold() for name in chapters}) == 2
        bodies = "\n".join(zf.read(name).decode("utf-8") for name in chapters)
        assert "大写内容" in bodies
        assert "小写内容" in bodies


def test_export_book_epub_cid_cannot_escape_text_dir(scenario: Scenario) -> None:
    cid = "../../../evil"
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info(cid, "越界")])])
    )
    scenario.write_chapters([make_chapter(cid, "越界内容", title="越界")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        chapters = [n for n in zf.namelist() if n.startswith("OEBPS/Text/c")]

    assert len(chapters) == 1
    parts = Path(chapters[0]).parts
    assert ".." not in parts
    assert len(parts) == 3


# ---------------------------------------------------------------------------
# Metadata validity (empty / illegal titles, missing book_name)
# ---------------------------------------------------------------------------


def test_export_book_epub_empty_book_name_gets_valid_title(
    scenario: Scenario,
) -> None:
    scenario.write_book_info(
        make_book_info(
            name="",
            author="",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        title = _opf_title(zf)
        nav_title = ET.fromstring(zf.read("OEBPS/nav.xhtml")).find(
            f"{{{XHTML_NS}}}head/{{{XHTML_NS}}}title"
        )

    # An empty <dc:title> fails strict EPUB validation; fall back to the
    # book id instead.
    assert title.strip() == scenario.book_id
    assert nav_title is not None
    assert (nav_title.text or "").strip()


def test_export_book_epub_title_with_illegal_xml_chars_is_valid(
    scenario: Scenario,
) -> None:
    scenario.write_book_info(
        make_book_info(
            name="A\x00B\x01<&>",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        # Must still be well-formed XML, with the illegal bytes dropped and
        # markup escaped exactly once.
        assert _opf_title(zf) == "AB<&>"


def test_export_book_epub_missing_book_name_still_exports(scenario: Scenario) -> None:
    scenario.write_book_info(
        {
            "author": "作者",
            "volumes": [make_volume("卷", [make_chapter_info("1", "章")])],
        }
    )
    scenario.write_chapters([make_chapter("1", "内容", title="章")])

    result = scenario.export("epub", ExporterConfig(append_timestamp=False))

    assert len(result["epub"]) == 1
    with zipfile.ZipFile(result["epub"][0]) as zf:
        assert _opf_title(zf).strip()


def test_export_chapter_epub_illegal_title_yields_valid_metadata(
    scenario: Scenario,
) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "内容", title="\x01")])

    result = scenario.export_chapter(
        "1", "epub", ExporterConfig(append_timestamp=False)
    )
    path = result["epub"]
    assert path is not None
    with zipfile.ZipFile(path) as zf:
        assert _opf_title(zf).strip()


# ---------------------------------------------------------------------------
# Accessibility metadata (EPUB 3 / schema.org)
# ---------------------------------------------------------------------------

_OPF_MODIFIED_RE = re.compile(rb'<meta property="dcterms:modified">[^<]*</meta>')


def _opf_meta(zf: zipfile.ZipFile) -> dict[str, list[str]]:
    """All ``<meta property=...>`` values in the OPF, keyed by property."""
    root = ET.fromstring(zf.read("OEBPS/content.opf"))
    values: dict[str, list[str]] = {}
    for node in root.iter(f"{{{OPF_NS}}}meta"):
        prop = node.get("property")
        if prop:
            values.setdefault(prop, []).append("".join(node.itertext()))
    return values


def _archive_snapshot(path: Path) -> dict[str, bytes]:
    """Archive entries keyed by name, minus the run timestamp in the OPF."""
    with zipfile.ZipFile(path) as zf:
        snapshot = {name: zf.read(name) for name in zf.namelist()}
    snapshot["OEBPS/content.opf"] = _OPF_MODIFIED_RE.sub(
        b"", snapshot["OEBPS/content.opf"]
    )
    return snapshot


def test_export_book_epub_has_accessibility_metadata(scenario: Scenario) -> None:
    _stage_two_volume_book(scenario)
    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]

    with zipfile.ZipFile(path) as zf:
        meta = _opf_meta(zf)
        package = ET.fromstring(zf.read("OEBPS/content.opf"))

    assert meta["schema:accessMode"] == ["textual"]
    assert meta["schema:accessibilityFeature"] == ["structuralNavigation"]
    assert meta["schema:accessibilityHazard"] == ["none"]
    summary = meta["schema:accessibilitySummary"]
    assert len(summary) == 1
    assert summary[0].strip()
    # Default book language is zh-Hans.
    assert "无障碍" in summary[0]
    # Prefixed property names need the vocabulary declaration.
    assert "schema: http://schema.org/" in (package.get("prefix") or "")


def test_export_book_epub_without_chapters_omits_structural_navigation(
    scenario: Scenario,
) -> None:
    # A volume title page is not a navigable chapter: without any chapter
    # document there is no table of contents to advertise.
    scenario.write_book_info(make_book_info(volumes=[make_volume("空卷", [])]))
    scenario.write_chapters([])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        meta = _opf_meta(zf)

    assert "schema:accessibilityFeature" not in meta
    assert meta["schema:accessMode"] == ["textual"]
    assert meta["schema:accessibilityHazard"] == ["none"]


def test_export_chapter_epub_has_accessibility_metadata(scenario: Scenario) -> None:
    scenario.write_book_info(make_book_info())
    scenario.write_chapters([make_chapter("1", "单章内容", title="第一章")])

    path = scenario.export_chapter("1", "epub", ExporterConfig(append_timestamp=False))[
        "epub"
    ]
    assert path is not None
    with zipfile.ZipFile(path) as zf:
        meta = _opf_meta(zf)

    assert meta["schema:accessMode"] == ["textual"]
    assert meta["schema:accessibilityFeature"] == ["structuralNavigation"]
    assert meta["schema:accessibilityHazard"] == ["none"]


def test_accessibility_summary_follows_book_language() -> None:
    zh = export_epub_module._accessibility_summary("zh-Hans", has_toc=True)
    en = export_epub_module._accessibility_summary("en", has_toc=True)
    en_plain = export_epub_module._accessibility_summary("en", has_toc=False)

    assert "无障碍" in zh
    assert "structured navigation" in en
    assert "structured navigation" not in en_plain
    assert "hazards" in en_plain


def test_opf_accessibility_meta_is_xml_escaped() -> None:
    builder = EpubBuilder(title="T")
    builder.opf.add_meta("schema:accessibilitySummary", 'A & <B> "C"')

    raw = builder.opf.to_xml()
    assert "&amp;" in raw
    assert "&lt;B&gt;" in raw

    root = ET.fromstring(raw)
    values = [
        "".join(node.itertext())
        for node in root.iter(f"{{{OPF_NS}}}meta")
        if node.get("property") == "schema:accessibilitySummary"
    ]
    assert values == ['A & <B> "C"']


def test_export_book_epub_accessibility_metadata_is_stable(
    scenario: Scenario,
) -> None:
    _stage_two_volume_book(scenario)
    cfg = ExporterConfig(append_timestamp=False)

    first = scenario.export("epub", cfg)["epub"][0]
    before = _archive_snapshot(first)
    second = scenario.export("epub", cfg)["epub"][0]

    # Every entry must be byte-identical; only the OPF's dcterms:modified
    # timestamp is allowed to differ between runs.
    assert _archive_snapshot(second) == before


# ---------------------------------------------------------------------------
# Archive structure: manifest, spine, nav and NCX all resolve
# ---------------------------------------------------------------------------


def test_export_book_epub_manifest_spine_and_nav_are_consistent(
    scenario: Scenario,
) -> None:
    scenario.write_media("cover.png", PNG_BYTES)
    scenario.write_book_info(
        make_book_info(
            name="结构 <书>",
            cover_url="https://example.com/cover.png",
            volumes=[
                make_volume("空卷", []),
                make_volume(
                    "卷/一",
                    [
                        make_chapter_info("1", "章一"),
                        make_chapter_info("2", "缺失章"),
                        make_chapter_info("a/b", "斜杠章"),
                    ],
                ),
            ],
        )
    )
    scenario.write_chapters(
        [
            make_chapter("1", "内容一", title="章一"),
            make_chapter("a/b", "内容二", title="斜杠章"),
        ]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert len(names) == len(set(names))

        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
        assert rootfile is not None
        assert rootfile.get("full-path") in names

        opf = ET.fromstring(zf.read("OEBPS/content.opf"))
        manifest = {
            item.get("id"): item.get("href")
            for item in opf.findall(f".//{{{OPF_NS}}}item")
        }
        spine = [item.get("idref") for item in opf.findall(f".//{{{OPF_NS}}}itemref")]
        assert spine
        assert set(spine) <= set(manifest)
        for href in manifest.values():
            assert f"OEBPS/{href}" in names

        text_entries = {n for n in names if n.startswith("OEBPS/Text/")}
        assert text_entries == {
            f"OEBPS/{href}"
            for href in manifest.values()
            if href and href.startswith("Text/")
        }

        nav = ET.fromstring(zf.read("OEBPS/nav.xhtml"))
        nav_hrefs = [a.get("href") for a in nav.iter(f"{{{XHTML_NS}}}a")]
        assert nav_hrefs
        for href in nav_hrefs:
            assert f"OEBPS/{href}" in names

        ncx = ET.fromstring(zf.read("OEBPS/toc.ncx"))
        ncx_srcs = [c.get("src") for c in ncx.iter(f"{{{NCX_NS}}}content")]
        assert ncx_srcs
        for src in ncx_srcs:
            assert f"OEBPS/{src}" in names


def test_export_book_epub_very_long_chapter_is_complete(scenario: Scenario) -> None:
    content = "长" * 400_000
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "超长章")])])
    )
    scenario.write_chapters([make_chapter("1", content, title="超长章")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")

    assert content in chapter


# ---------------------------------------------------------------------------
# Image pipeline: covers, media types, dedupe and failure fallbacks
# ---------------------------------------------------------------------------

# Minimal payloads that the magic-byte sniffer recognises.
WEBP_BYTES = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 4
SVG_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect/></svg>'
)
# Enough bytes for the sniffer, but not an image (e.g. a cached 404 page).
NOT_IMAGE_BYTES = b"<html>404 not found, no image here</html>"

_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def _manifest_items(
    zf: zipfile.ZipFile,
) -> dict[str, tuple[str, str, str]]:
    root = ET.fromstring(zf.read("OEBPS/content.opf"))
    return {
        item.get("id") or "": (
            item.get("href") or "",
            item.get("media-type") or "",
            item.get("properties") or "",
        )
        for item in root.findall(f".//{{{OPF_NS}}}item")
    }


def _image_sources(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Every ``<img src>`` in the archive, paired with its owning document."""
    out: list[tuple[str, str]] = []
    for name in zf.namelist():
        if not name.endswith(".xhtml"):
            continue
        root = ET.fromstring(zf.read(name))
        for img in root.iter(f"{{{XHTML_NS}}}img"):
            src = img.get("src")
            if src:
                out.append((name, src))
    return out


def _assert_image_links_resolve(zf: zipfile.ZipFile) -> None:
    """Every image reference must resolve to a manifest-registered entry."""
    names = set(zf.namelist())
    manifest = _manifest_items(zf)
    by_href = {
        href: (media_type, props)
        for href, media_type, props in manifest.values()
        if href.startswith("Images/")
    }

    for doc, src in _image_sources(zf):
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(doc), src))
        assert resolved in names, f"{doc} references missing {src}"
        assert ".." not in Path(resolved).parts
        href = resolved.removeprefix("OEBPS/")
        assert href in by_href, f"{href} is absent from the manifest"
        media_type, _ = by_href[href]
        assert media_type == _IMAGE_MEDIA_TYPES[Path(href).suffix.lower()]

    cover_images = [
        href for href, _, props in manifest.values() if "cover-image" in props
    ]
    assert len(cover_images) <= 1


def test_export_volume_epub_reuses_cached_book_cover(scenario: Scenario) -> None:
    # Downloads cache the book cover as ``cover.<ext>`` (name="cover"); the
    # split export must look it up the same way as the single-file export.
    scenario.write_media("cover.png", PNG_BYTES)
    scenario.write_book_info(
        make_book_info(
            cover_url="https://example.com/cover.png",
            volumes=[
                make_volume("第一卷", [make_chapter_info("1", "章一")]),
                make_volume("第二卷", [make_chapter_info("2", "章二")]),
            ],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容一"), make_chapter("2", "内容二")])

    paths = scenario.export(
        "epub", ExporterConfig(append_timestamp=False, split_mode="volume")
    )["epub"]
    assert len(paths) == 2

    for path in paths:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            manifest = _manifest_items(zf)
            _assert_image_links_resolve(zf)
        assert "OEBPS/Images/cover.png" in names
        assert any("cover-image" in props for _, _, props in manifest.values())


def test_export_book_epub_damaged_cover_is_omitted(scenario: Scenario) -> None:
    # A cached error page under ``cover.png`` must not become the cover.
    scenario.write_media("cover.png", NOT_IMAGE_BYTES)
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
        manifest = _manifest_items(zf)
        # Still a well-formed package, just without a cover.
        assert ET.fromstring(zf.read("OEBPS/content.opf")) is not None

    assert not [n for n in names if n.startswith("OEBPS/Images/")]
    assert "OEBPS/Text/cover.xhtml" not in names
    assert not any("cover-image" in props for _, _, props in manifest.values())


@pytest.mark.parametrize(
    "cover_url",
    ["", "data:image/png;base64,AAAA", "/rel/c.png", "https://example.com/missing.png"],
)
def test_export_book_epub_unresolvable_cover_is_omitted(
    scenario: Scenario, cover_url: str
) -> None:
    scenario.write_book_info(
        make_book_info(
            cover_url=cover_url,
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        manifest = _manifest_items(zf)
        _assert_image_links_resolve(zf)

    assert "OEBPS/Text/c1.xhtml" in names
    assert not any("cover-image" in props for _, _, props in manifest.values())


def test_export_book_epub_cover_extension_follows_bytes(scenario: Scenario) -> None:
    # The download cache is named after the URL suffix, but the bytes win:
    # a JPEG-named PNG must be published as PNG and referenced as such.
    scenario.write_media("cover.jpg", PNG_BYTES)
    scenario.write_book_info(
        make_book_info(
            cover_url="https://example.com/cover.jpg",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        manifest = _manifest_items(zf)
        cover = zf.read("OEBPS/Text/cover.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/cover.png" in names
    assert "OEBPS/Images/cover.jpg" not in names
    cover_items = [
        (href, media_type)
        for href, media_type, props in manifest.values()
        if "cover-image" in props
    ]
    assert cover_items == [("Images/cover.png", "image/png")]
    assert "../Images/cover.png" in cover


def test_export_book_epub_corrupt_chapter_image_is_skipped(
    scenario: Scenario,
) -> None:
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "base64": base64.b64encode(NOT_IMAGE_BYTES).decode("ascii"),
        "mime": "image/png",
        "alt": "坏图",
    }
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert not [n for n in names if n.startswith("OEBPS/Images/")]
    assert "<img" not in chapter
    assert "正文" in chapter


def test_export_book_epub_zero_byte_image_is_skipped(scenario: Scenario) -> None:
    url = "https://example.com/empty.png"
    scenario.write_media(image_filename(url), b"")
    resource = {"type": "image", "paragraph_index": 0, "url": url}
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")

    assert not [n for n in names if n.startswith("OEBPS/Images/")]
    assert "<img" not in chapter


@pytest.mark.parametrize("kind", ["url", "base64"])
def test_export_book_epub_oversized_image_is_skipped(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(export_epub_module, "_MAX_IMAGE_BYTES", 64, raising=False)
    oversized = PNG_BYTES + b"\x00" * 128

    if kind == "url":
        url = "https://example.com/huge.png"
        scenario.write_media(image_filename(url), oversized)
        resource = {"type": "image", "paragraph_index": 0, "url": url}
    else:
        resource = {
            "type": "image",
            "paragraph_index": 0,
            "base64": base64.b64encode(oversized).decode("ascii"),
            "mime": "image/png",
        }

    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")

    assert not [n for n in names if n.startswith("OEBPS/Images/")]
    assert "<img" not in chapter


@pytest.mark.parametrize("index", [None, "2", -5, 10**9])
def test_export_book_epub_invalid_paragraph_index_is_tolerated(
    scenario: Scenario, index: object
) -> None:
    # Hand-written / parser-produced metadata is not type-checked at runtime:
    # a null or string index used to abort the whole chapter with TypeError.
    resource = {
        "type": "image",
        "paragraph_index": index,
        "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mime": "image/png",
    }
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "第一行\n第二行", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")

    assert "OEBPS/Images/img_0.png" in names
    assert chapter.count("<img") == 1
    assert "第一行" in chapter and "第二行" in chapter


def test_export_book_epub_non_string_alt_does_not_drop_image(
    scenario: Scenario,
) -> None:
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mime": "image/png",
        "alt": 5,
    }
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")

    assert 'alt="5"' in chapter
    assert '<img src="../Images/img_0.png"' in chapter


def test_export_book_epub_image_extension_follows_bytes(scenario: Scenario) -> None:
    # URL says .jpg, cached bytes are PNG: extension, media-type and <img src>
    # must all agree with the real payload.
    url = "https://example.com/photo.jpg"
    scenario.write_media(image_filename(url), PNG_BYTES)
    resource = {"type": "image", "paragraph_index": 0, "url": url, "alt": "图"}
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        manifest = _manifest_items(zf)
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/img_0.png" in names
    assert "OEBPS/Images/img_0.jpg" not in names
    assert manifest["img_0"][:2] == ("Images/img_0.png", "image/png")
    assert '<img src="../Images/img_0.png"' in chapter


def test_export_book_epub_svg_and_webp_images(scenario: Scenario) -> None:
    resources = [
        {
            "type": "image",
            "paragraph_index": 0,
            "base64": base64.b64encode(SVG_BYTES).decode("ascii"),
            "mime": "image/svg+xml",
            "alt": "svg",
        },
        {
            "type": "image",
            "paragraph_index": 0,
            "base64": base64.b64encode(WEBP_BYTES).decode("ascii"),
            "mime": "image/webp",
            "alt": "webp",
        },
    ]
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters([make_chapter("1", "正文", extra={"resources": resources})])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        manifest = _manifest_items(zf)
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/img_0.svg" in names
    assert manifest["img_0"][:2] == ("Images/img_0.svg", "image/svg+xml")
    assert "OEBPS/Images/img_1.webp" in names
    assert manifest["img_1"][:2] == ("Images/img_1.webp", "image/webp")
    assert '<img src="../Images/img_0.svg"' in chapter
    assert '<img src="../Images/img_1.webp"' in chapter


def test_export_book_epub_identical_image_bytes_are_stored_once(
    scenario: Scenario,
) -> None:
    url_a = "https://example.com/a.png"
    url_b = "https://example.com/b.png"
    scenario.write_media(image_filename(url_a), PNG_BYTES)
    scenario.write_media(image_filename(url_b), PNG_BYTES)
    resources = [
        {"type": "image", "paragraph_index": 0, "url": url_a},
        {"type": "image", "paragraph_index": 0, "url": url_b},
    ]
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters([make_chapter("1", "正文", extra={"resources": resources})])

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        images = [n for n in zf.namelist() if n.startswith("OEBPS/Images/")]
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")

    assert images == ["OEBPS/Images/img_0.png"]
    assert chapter.count('<img src="../Images/img_0.png"') == 2


def test_export_book_epub_image_url_cannot_escape_images_dir(
    scenario: Scenario,
) -> None:
    url = "../../../outside/escape.png"
    scenario.write_media(image_filename(url), PNG_BYTES)
    resource = {"type": "image", "paragraph_index": 0, "url": url}
    scenario.write_book_info(
        make_book_info(
            cover_url="https://example.com/../../cover.png",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        _assert_image_links_resolve(zf)

    for name in names:
        assert ".." not in Path(name).parts
    assert [n for n in names if n.startswith("OEBPS/Images/")]


def test_export_book_epub_missing_image_file_leaves_no_reference(
    scenario: Scenario,
) -> None:
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "url": "https://example.com/never-downloaded.png",
        "alt": "缺失",
    }
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert not [n for n in names if n.startswith("OEBPS/Images/")]
    assert "<img" not in chapter
    assert "正文" in chapter


def test_export_book_epub_utf8_bom_svg_image(scenario: Scenario) -> None:
    # A UTF-8 BOM hides the XML declaration from the magic-byte sniffer; the
    # resource must still be published as SVG rather than being dropped.
    bom_svg = b"\xef\xbb\xbf" + SVG_BYTES
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "base64": base64.b64encode(bom_svg).decode("ascii"),
        "mime": "image/svg+xml",
    }
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        manifest = _manifest_items(zf)
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/img_0.svg" in names
    assert manifest["img_0"][:2] == ("Images/img_0.svg", "image/svg+xml")


def test_export_book_epub_chapter_image_reuses_cover_bytes(
    scenario: Scenario,
) -> None:
    # The cover is registered first; an identical illustration must reuse the
    # existing asset instead of being stored twice (and its <img> must point
    # at the cover entry, which is already in the manifest).
    scenario.write_media("cover.png", PNG_BYTES)
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mime": "image/png",
    }
    scenario.write_book_info(
        make_book_info(
            cover_url="https://example.com/cover.png",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        images = [n for n in zf.namelist() if n.startswith("OEBPS/Images/")]
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert images == ["OEBPS/Images/cover.png"]
    assert '<img src="../Images/cover.png"' in chapter


def test_export_chapter_epub_includes_image(scenario: Scenario) -> None:
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mime": "image/png",
    }
    scenario.write_book_info(make_book_info())
    scenario.write_chapters(
        [make_chapter("1", "单章正文", extra={"resources": [resource]})]
    )

    result = scenario.export_chapter(
        "1", "epub", ExporterConfig(append_timestamp=False)
    )
    path = result["epub"]
    assert path is not None

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/img_0.png" in names
    assert '<img src="../Images/img_0.png"' in chapter
    assert "单章正文" in chapter


def test_export_book_epub_inline_image_is_fallback_for_missing_download(
    scenario: Scenario,
) -> None:
    # Some parsers attach both the remote URL and an inline copy. When the
    # download is absent the inline payload must be used instead of dropping
    # the illustration.
    resource = {
        "type": "image",
        "paragraph_index": 0,
        "url": "https://example.com/not-cached.png",
        "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mime": "image/png",
    }
    scenario.write_book_info(
        make_book_info(volumes=[make_volume("卷", [make_chapter_info("1", "章")])])
    )
    scenario.write_chapters(
        [make_chapter("1", "正文", extra={"resources": [resource]})]
    )

    path = scenario.export("epub", ExporterConfig(append_timestamp=False))["epub"][0]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        chapter = zf.read("OEBPS/Text/c1.xhtml").decode("utf-8")
        _assert_image_links_resolve(zf)

    assert "OEBPS/Images/img_0.png" in names
    assert '<img src="../Images/img_0.png"' in chapter
