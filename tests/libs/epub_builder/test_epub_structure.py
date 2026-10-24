"""End-to-end EPUB structure tests that do not depend on any site client."""

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from novel_downloader.libs.epub_builder import EpubBuilder, EpubChapter, EpubVolume
from novel_downloader.libs.epub_builder.constants import (
    DC_NS,
    FONT_DIR,
    IMAGE_DIR,
    ROOT_PATH,
    TEXT_DIR,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def png_bytes(color=(255, 0, 0), size=(4, 4)) -> bytes:
    """Generate a real, small PNG using Pillow."""
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def ttf_bytes(payload: bytes = b"FAKEFONTDATA") -> bytes:
    """A minimal byte string detected as TTF by media.font."""
    return b"\x00\x01\x00\x00" + payload


def write_png(path: Path, color=(1, 2, 3)) -> Path:
    path.write_bytes(png_bytes(color))
    return path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def opf_root(zf: zipfile.ZipFile) -> ET.Element:
    return parse(zf, f"{ROOT_PATH}/content.opf")


def manifest_items(root: ET.Element) -> list[ET.Element]:
    manifest = next(e for e in root if local_name(e.tag) == "manifest")
    return [e for e in manifest if local_name(e.tag) == "item"]


def spine_idrefs(root: ET.Element) -> list[str]:
    spine = next(e for e in root if local_name(e.tag) == "spine")
    return [e.attrib["idref"] for e in spine if local_name(e.tag) == "itemref"]


def nav_links(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    root = parse(zf, f"{ROOT_PATH}/nav.xhtml")
    return [
        (e.attrib.get("href", ""), "".join(e.itertext()).strip())
        for e in root.iter()
        if local_name(e.tag) == "a"
    ]


def assert_xml_entries_well_formed(zf: zipfile.ZipFile) -> None:
    for name in zf.namelist():
        lower = name.lower()
        if lower.endswith((".xml", ".xhtml", ".ncx", ".opf")):
            parse(zf, name)  # raises on malformed XML
            text = zf.read(name).decode("utf-8")
            assert CONTROL_CHARS.search(text) is None, name


# ----------------------------------------------------------------------
# ZIP / container / mimetype
# ----------------------------------------------------------------------
def test_mimetype_is_first_and_stored(tmp_path):
    builder = EpubBuilder("Book")
    out = tmp_path / "book.epub"
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        assert zf.namelist()[0] == "mimetype"
        info = zf.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        assert info.compress_size == len(b"application/epub+zip")


def test_container_points_to_opf(tmp_path):
    out = tmp_path / "book.epub"
    EpubBuilder("Book").export(out)

    with zipfile.ZipFile(out) as zf:
        root = parse(zf, "META-INF/container.xml")
        rootfile = next(e for e in root.iter() if local_name(e.tag) == "rootfile")
        assert rootfile.attrib["full-path"] == f"{ROOT_PATH}/content.opf"
        assert rootfile.attrib["media-type"] == "application/oebps-package+xml"


def test_zip_has_no_duplicate_entries(tmp_path):
    out = tmp_path / "book.epub"
    EpubBuilder("Book").export(out)

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names))


# ----------------------------------------------------------------------
# OPF / nav / ncx consistency
# ----------------------------------------------------------------------
def test_opf_manifest_spine_and_files_are_consistent(tmp_path):
    out = tmp_path / "book.epub"
    builder = EpubBuilder("Book", author="Author", uid="uid-1")
    builder.add_chapter(EpubChapter(id="c1", filename="c1.xhtml", title="Chapter 1"))
    builder.add_chapter(EpubChapter(id="c2", filename="c2.xhtml", title="Chapter 2"))
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        root = opf_root(zf)
        items = manifest_items(root)

        ids = [e.attrib["id"] for e in items]
        assert len(ids) == len(set(ids))

        manifest = {e.attrib["id"]: e for e in items}
        for item in items:
            assert f"{ROOT_PATH}/{item.attrib['href']}" in names

        spine = spine_idrefs(root)
        assert spine
        assert set(spine) <= set(manifest)
        # The first two entries are the cover page (if any) and the intro.
        assert "intro" in spine
        assert spine.index("intro") < spine.index("c1") < spine.index("c2")

        nav_item = manifest["nav"]
        assert nav_item.attrib["media-type"] == "application/xhtml+xml"
        assert "nav" in nav_item.attrib.get("properties", "")

        ncx_item = manifest["ncx"]
        assert ncx_item.attrib["media-type"] == "application/x-dtbncx+xml"

        spine_el = next(e for e in root if local_name(e.tag) == "spine")
        assert spine_el.attrib["toc"] == "ncx"

        # metadata
        metadata = next(e for e in root if local_name(e.tag) == "metadata")
        title = metadata.find(f"{{{DC_NS}}}title")
        ident = metadata.find(f"{{{DC_NS}}}identifier")
        assert title is not None and title.text == "Book"
        assert ident is not None and ident.text == "uid-1"
        modified = [
            e for e in metadata if e.attrib.get("property") == "dcterms:modified"
        ]
        assert modified and modified[0].text

        # every nav/ncx target exists in the archive
        for href in [h for h, _ in nav_links(zf)]:
            assert f"{ROOT_PATH}/{href}" in names

        ncx = parse(zf, f"{ROOT_PATH}/toc.ncx")
        for content in (e for e in ncx.iter() if local_name(e.tag) == "content"):
            assert f"{ROOT_PATH}/{content.attrib['src']}" in names


def test_nav_contains_volume_nesting_and_chapters(tmp_path):
    out = tmp_path / "book.epub"
    builder = EpubBuilder("Book")
    builder.add_volume(
        EpubVolume(
            id="v1",
            title="Volume One",
            chapters=[
                EpubChapter(id="c1", filename="c1.xhtml", title="C1"),
                EpubChapter(id="c2", filename="c2.xhtml", title="C2"),
            ],
        )
    )
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        root = parse(zf, f"{ROOT_PATH}/nav.xhtml")
        nav = next(e for e in root.iter() if local_name(e.tag) == "nav")
        assert nav.attrib.get("{http://www.idpf.org/2007/ops}type") == "toc"
        # one nested <ol> containing the two chapter links
        nested = [e for e in nav.iter() if local_name(e.tag) == "ol"]
        assert len(nested) == 2
        assert len([e for e in nested[1] if local_name(e.tag) == "li"]) == 2


# ----------------------------------------------------------------------
# Empty books and duplicate titles
# ----------------------------------------------------------------------
def test_empty_book_exports_valid_epub(tmp_path):
    out = tmp_path / "empty.epub"
    EpubBuilder("", uid="").export(out)

    with zipfile.ZipFile(out) as zf:
        assert_xml_entries_well_formed(zf)
        root = opf_root(zf)
        manifest = manifest_items(root)
        ids = {e.attrib["id"] for e in manifest}
        assert {"nav", "ncx", "style", "intro"} <= ids
        assert spine_idrefs(root) == ["intro"]
        assert nav_links(zf) == [("Text/intro.xhtml", "书籍简介")]


def test_duplicate_titles_are_all_kept(tmp_path):
    out = tmp_path / "dup.epub"
    builder = EpubBuilder("Book")
    builder.add_chapter(EpubChapter(id="c1", filename="c1.xhtml", title="Same Title"))
    builder.add_chapter(EpubChapter(id="c2", filename="c2.xhtml", title="Same Title"))
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        labels = [label for _, label in nav_links(zf)]
        assert labels.count("Same Title") == 2
        manifest = {e.attrib["id"]: e for e in manifest_items(opf_root(zf))}
        assert manifest["c1"].attrib["href"] != manifest["c2"].attrib["href"]


# ----------------------------------------------------------------------
# Escaping / illegal characters
# ----------------------------------------------------------------------
def test_metadata_and_chapter_text_are_escaped(tmp_path):
    out = tmp_path / "escaped.epub"
    builder = EpubBuilder(
        title='A & B <C> "D"',
        author="O'Reilly & Sons",
        description="Line <1> & Line 2",
        uid="uid&1",
        subject=["a&b", "<tag>"],
    )
    builder.add_chapter(
        EpubChapter(
            id="c1",
            filename="c1.xhtml",
            title="Ch & <One>",
            content="<p>Body &amp; text</p>",
        )
    )
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        assert_xml_entries_well_formed(zf)
        root = opf_root(zf)
        metadata = next(e for e in root if local_name(e.tag) == "metadata")
        assert metadata.find(f"{{{DC_NS}}}title").text == 'A & B <C> "D"'
        assert metadata.find(f"{{{DC_NS}}}creator").text == "O'Reilly & Sons"
        assert metadata.find(f"{{{DC_NS}}}description").text == "Line <1> & Line 2"

        chapter = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/c1.xhtml").decode("utf-8")
        assert "<title>Ch &amp; &lt;One&gt;</title>" in chapter
        assert "<p>Body &amp; text</p>" in chapter


def test_illegal_xml_control_characters_are_removed(tmp_path):
    out = tmp_path / "control.epub"
    builder = EpubBuilder(
        title="A\x01&B<C>",
        author="Au\x0b",
        description="De\x1fsc",
        uid="u\x00id",
        subject=["s\x01"],
    )
    builder.add_chapter(
        EpubChapter(
            id="c1",
            filename="c1.xhtml",
            title="T\x01&<",
            content="<p>ok\x0c</p>",
            extra_content="<i>extra\x00</i>",
        )
    )
    builder.add_volume(
        EpubVolume(
            id="v",
            title="Vol\x02 - Sub",
            intro="Intro\x03 line",
            chapters=[EpubChapter(id="c2", filename="c2.xhtml", title="C\x04")],
        )
    )
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        assert_xml_entries_well_formed(zf)
        root = opf_root(zf)
        metadata = next(e for e in root if local_name(e.tag) == "metadata")
        assert metadata.find(f"{{{DC_NS}}}title").text == "A&B<C>"
        assert metadata.find(f"{{{DC_NS}}}creator").text == "Au"

        chapter = parse(zf, f"{ROOT_PATH}/{TEXT_DIR}/c1.xhtml")
        body = next(e for e in chapter.iter() if local_name(e.tag) == "body")
        assert "ok" in "".join(body.itertext())
        assert "extra" in "".join(body.itertext())


# ----------------------------------------------------------------------
# Cover / images / fonts / volume media
# ----------------------------------------------------------------------
def test_cover_image_is_registered_and_referenced(tmp_path):
    cover = write_png(tmp_path / "cover.png", (255, 0, 0))
    out = tmp_path / "cover.epub"
    builder = EpubBuilder("Book", cover_path=cover, uid="u")
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert f"{ROOT_PATH}/{IMAGE_DIR}/cover.png" in names
        assert f"{ROOT_PATH}/{TEXT_DIR}/cover.xhtml" in names

        items = {e.attrib["id"]: e for e in manifest_items(opf_root(zf))}
        assert items["cover-img"].attrib["properties"] == "cover-image"
        assert items["cover-img"].attrib["href"] == "Images/cover.png"

        cover_page = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/cover.xhtml").decode("utf-8")
        assert 'src="../Images/cover.png"' in cover_page

        metadata = next(e for e in opf_root(zf) if local_name(e.tag) == "metadata")
        meta_cover = next(e for e in metadata if e.attrib.get("name") == "cover")
        assert meta_cover.attrib["content"] == "cover-img"


def test_volume_cover_reuses_book_cover_when_identical(tmp_path):
    cover = write_png(tmp_path / "cover.png", (0, 128, 255))
    builder = EpubBuilder("Book", cover_path=cover)
    builder.add_volume(
        EpubVolume(
            id="v",
            title="Vol",
            cover_path=cover,
            chapters=[EpubChapter(id="c1", filename="c1.xhtml", title="C1")],
        )
    )

    out = tmp_path / "same-cover.epub"
    builder.export(out)

    # Regression: the cover image used to be registered twice.
    assert len(builder.images) == 1
    assert builder.images[0].id == "cover-img"

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert names.count(f"{ROOT_PATH}/{IMAGE_DIR}/cover.png") == 1
        vol_cover = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/vol_0_cover.xhtml").decode("utf-8")
        assert 'src="../Images/cover.png"' in vol_cover


def test_volume_cover_and_media_directories(tmp_path):
    volume_cover = write_png(tmp_path / "vol.png", (0, 255, 0))
    builder = EpubBuilder("Book")
    builder.add_volume(
        EpubVolume(
            id="v",
            title="Vol - Sub",
            intro="Volume intro",
            cover_path=volume_cover,
            chapters=[
                EpubChapter(
                    id="c1",
                    filename="c1.xhtml",
                    title="C1",
                    content="<p>text</p>",
                )
            ],
        )
    )

    image_name = builder.add_image_bytes(png_bytes((9, 9, 9)))
    font = builder.add_font_bytes(ttf_bytes())
    assert image_name and font

    out = tmp_path / "media.epub"
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert f"{ROOT_PATH}/{IMAGE_DIR}/{image_name}" in names
        assert f"{ROOT_PATH}/{FONT_DIR}/{font.filename}" in names
        assert f"{ROOT_PATH}/{TEXT_DIR}/vol_0_cover.xhtml" in names
        assert f"{ROOT_PATH}/{TEXT_DIR}/vol_0_intro.xhtml" in names

        vol_cover = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/vol_0_cover.xhtml").decode("utf-8")
        assert f'src="../Images/{builder.images[0].filename}"' in vol_cover

        # volume intro with a cover uses the h2 heading variant
        vol_intro = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/vol_0_intro.xhtml").decode("utf-8")
        assert '<h2 class="vol-title">Vol - Sub</h2>' in vol_intro
        assert "Volume intro" in vol_intro


def test_volume_without_cover_uses_title_page_and_desc_intro(tmp_path):
    builder = EpubBuilder("Book")
    builder.add_volume(
        EpubVolume(
            id="v",
            title="No Cover",
            intro="Only intro",
            chapters=[EpubChapter(id="c1", filename="c1.xhtml", title="C1")],
        )
    )
    out = tmp_path / "no-cover.epub"
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert f"{ROOT_PATH}/{TEXT_DIR}/vol_0_title.xhtml" in names
        assert f"{ROOT_PATH}/{TEXT_DIR}/vol_0_intro.xhtml" in names
        assert f"{ROOT_PATH}/{TEXT_DIR}/vol_0_cover.xhtml" not in names

        title_page = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/vol_0_title.xhtml").decode(
            "utf-8"
        )
        assert "<h1" in title_page

        vol_intro = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/vol_0_intro.xhtml").decode("utf-8")
        assert '<h3 class="vol-title">简介</h3>' in vol_intro


def test_font_is_embedded_with_face_css(tmp_path):
    builder = EpubBuilder("Book")
    font = builder.add_font_bytes(ttf_bytes(), selectors=(".chapter-content",))
    assert font is not None
    builder.add_chapter(
        EpubChapter(
            id="c1",
            filename="c1.xhtml",
            title="C1",
            content="<p>x</p>",
            fonts=[font],
        )
    )
    out = tmp_path / "font.epub"
    builder.export(out)

    with zipfile.ZipFile(out) as zf:
        chapter = zf.read(f"{ROOT_PATH}/{TEXT_DIR}/c1.xhtml").decode("utf-8")
        assert "@font-face" in chapter
        assert f'url("../Fonts/{font.filename}")' in chapter
        assert ".chapter-content" in chapter
        assert f"{ROOT_PATH}/{FONT_DIR}/{font.filename}" in zf.namelist()


# ----------------------------------------------------------------------
# Media helpers: missing / empty / unsupported inputs
# ----------------------------------------------------------------------
def test_invalid_media_inputs_are_ignored(tmp_path):
    builder = EpubBuilder("Book")

    assert builder.add_image(tmp_path / "missing.png") == ""
    assert builder.add_image_bytes(b"") == ""
    assert builder.add_font(tmp_path / "missing.ttf") is None
    assert builder.add_font_bytes(b"") is None

    # BMP is detected but not supported by the EPUB image media map.
    bmp = b"BM" + b"\x00" * 20
    assert builder.add_image_bytes(bmp) == ""
    assert builder.images == []
    assert builder.fonts == []


# ----------------------------------------------------------------------
# Export atomicity
# ----------------------------------------------------------------------
def test_export_failure_preserves_existing_file_and_leaves_no_temp(
    tmp_path, monkeypatch
):
    out = tmp_path / "book.epub"
    out.write_bytes(b"EXISTING")

    builder = EpubBuilder("Book")
    real_writestr = zipfile.ZipFile.writestr

    def failing_writestr(self, name, data, *args, **kwargs):
        if name in ("mimetype", "META-INF/container.xml"):
            return real_writestr(self, name, data, *args, **kwargs)
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", failing_writestr)

    with pytest.raises(OSError, match="disk full"):
        builder.export(out)

    assert out.read_bytes() == b"EXISTING"
    assert [p.name for p in tmp_path.iterdir()] == ["book.epub"]


def test_export_creates_parent_directory(tmp_path):
    out = tmp_path / "nested" / "dir" / "book.epub"
    result = EpubBuilder("Book").export(out)

    assert result == out
    assert out.exists()


def test_export_returns_output_path(tmp_path):
    out = tmp_path / "book.epub"
    assert EpubBuilder("Book").export(str(out)) == out
