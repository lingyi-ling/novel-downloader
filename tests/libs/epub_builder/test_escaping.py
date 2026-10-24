"""Unit tests for escaping / XML-safety in the EPUB models."""

import xml.etree.ElementTree as ET

from novel_downloader.libs.epub_builder.models import (
    EpubChapter,
    EpubCover,
    EpubIntro,
    EpubVolumeCover,
    EpubVolumeTitle,
    NavDocument,
    NCXDocument,
    OpfDocument,
    escape_label,
    escape_text,
)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ----------------------------------------------------------------------
# escape helpers
# ----------------------------------------------------------------------
def test_escape_label_escapes_markup():
    assert escape_label("a & <b>") == "a &amp; &lt;b&gt;"


def test_escape_text_escapes_quotes():
    assert escape_text("\"'&<>") == "&quot;&#x27;&amp;&lt;&gt;"


def test_escape_helpers_drop_illegal_xml_chars():
    assert escape_label("a\x00b\x08c\x0bd\x1fe") == "abcde"
    assert escape_text("x\x0c\x0by\ud800z") == "xyz"


# ----------------------------------------------------------------------
# Nav / NCX
# ----------------------------------------------------------------------
def test_nav_escapes_labels_and_hrefs():
    nav = NavDocument(title="T&u")
    nav.add_chapter("c1", "A & <B>", "a&b.xhtml")
    xhtml = nav.to_xhtml()

    assert "A &amp; &lt;B&gt;" in xhtml
    assert 'href="a&amp;b.xhtml"' in xhtml
    root = ET.fromstring(xhtml)
    link = next(e for e in root.iter() if local_name(e.tag) == "a")
    assert link.attrib["href"] == "a&b.xhtml"


def test_nav_volume_escapes_labels_and_hrefs():
    nav = NavDocument()
    nav.add_volume("v1", "Vol & <1>", "v&1.xhtml", [("c1", "C & <1>", "c&1.xhtml")])
    xhtml = nav.to_xhtml()

    assert "Vol &amp; &lt;1&gt;" in xhtml
    assert "C &amp; &lt;1&gt;" in xhtml
    assert 'href="v&amp;1.xhtml"' in xhtml
    assert 'href="c&amp;1.xhtml"' in xhtml
    ET.fromstring(xhtml)


def test_ncx_escapes_title_uid_labels_and_sources():
    ncx = NCXDocument(title="Book & <More>", uid="uid&1")
    ncx.add_chapter("c&1", "C & <1>", "c&1.xhtml")

    root = ET.fromstring(ncx.to_xml())
    text = next(e for e in root.iter() if local_name(e.tag) == "text")
    assert text.text == "Book & <More>"

    meta = next(e for e in root.iter() if local_name(e.tag) == "meta")
    assert meta.attrib["content"] == "uid&1"

    content = next(e for e in root.iter() if local_name(e.tag) == "content")
    assert content.attrib["src"] == "c&1.xhtml"


def test_ncx_volume_nesting_remains_valid_xml():
    ncx = NCXDocument(title="T", uid="u")
    ncx.add_volume("v1", "V", "v.xhtml", [("c1", "C", "c.xhtml")])
    root = ET.fromstring(ncx.to_xml())

    navpoints = [e for e in root.iter() if local_name(e.tag) == "navPoint"]
    assert len(navpoints) == 2
    assert navpoints[0].attrib["id"] == "v1"
    orders = [int(e.attrib["playOrder"]) for e in navpoints]
    assert orders == [1, 2]


# ----------------------------------------------------------------------
# OPF
# ----------------------------------------------------------------------
def test_opf_escapes_metadata_and_attributes():
    opf = OpfDocument(
        title="T & <1>",
        author="A&B",
        description="D <&>",
        uid="uid&1",
        language="zh-Hans",
    )
    opf.add_manifest_item("i&1", "Text/a&b.xhtml", "application/xhtml+xml")
    opf.add_spine_item("i&1")

    root = ET.fromstring(opf.to_xml())
    metadata = next(e for e in root if local_name(e.tag) == "metadata")

    assert metadata.find("{http://purl.org/dc/elements/1.1/}identifier").text == (
        "uid&1"
    )
    assert metadata.find("{http://purl.org/dc/elements/1.1/}title").text == "T & <1>"
    assert metadata.find("{http://purl.org/dc/elements/1.1/}creator").text == "A&B"
    assert metadata.find("{http://purl.org/dc/elements/1.1/}description").text == (
        "D <&>"
    )

    item = next(e for e in root.iter() if local_name(e.tag) == "item")
    assert item.attrib["id"] == "i&1"
    assert item.attrib["href"] == "Text/a&b.xhtml"

    itemref = next(e for e in root.iter() if local_name(e.tag) == "itemref")
    assert itemref.attrib["idref"] == "i&1"


def test_opf_detects_cover_and_toc_after_escaping():
    opf = OpfDocument(uid="u")
    opf.add_manifest_item("cover&1", "c.jpg", "image/jpeg", properties="cover-image")
    opf.add_manifest_item("ncx&1", "toc.ncx", "application/x-dtbncx+xml")
    opf.add_spine_item("cover&1")

    root = ET.fromstring(opf.to_xml())
    meta = next(
        e
        for e in root.iter()
        if local_name(e.tag) == "meta" and e.attrib.get("name") == "cover"
    )
    assert meta.attrib["content"] == "cover&1"

    spine = next(e for e in root if local_name(e.tag) == "spine")
    assert spine.attrib["toc"] == "ncx&1"


# ----------------------------------------------------------------------
# Content models
# ----------------------------------------------------------------------
def test_chapter_drops_controls_but_keeps_markup():
    ch = EpubChapter(
        id="c",
        filename="c.xhtml",
        title="T\x01",
        content="<p>hello\x0c<em>world</em></p>",
        extra_content="<i>extra\x00</i>",
    )
    xhtml = ch.to_xhtml()

    assert "\x01" not in xhtml and "\x0c" not in xhtml and "\x00" not in xhtml
    assert "<p>hello<em>world</em></p>" in xhtml
    assert "<i>extra</i>" in xhtml
    ET.fromstring(xhtml)


def test_volume_cover_escapes_title_and_image_name():
    page = EpubVolumeCover(
        id="v",
        filename="v.xhtml",
        title="Vol & <1>",
        image_name='a"&.png',
    ).to_xhtml()

    assert 'alt="Vol &amp; &lt;1&gt;"' in page
    assert 'src="../Images/a&quot;&amp;.png"' in page
    ET.fromstring(page)


def test_volume_title_drops_illegal_chars():
    page = EpubVolumeTitle(
        id="v", filename="v.xhtml", full_title="Main\x01 - Sub\x0b"
    ).to_xhtml()

    assert "Main - Sub" in page
    assert "\x01" not in page and "\x0b" not in page
    ET.fromstring(page)


def test_intro_and_description_drop_illegal_chars():
    intro = EpubIntro(
        id="i",
        filename="i.xhtml",
        title="I\x01",
        book_title="Book\x02",
        author="Au\x03",
        description="Line\x04 one\nLine\x05 two",
        subject=["a\x06", "b&c"],
    )
    xhtml = intro.to_xhtml()

    for char in ("\x01", "\x02", "\x03", "\x04", "\x05", "\x06"):
        assert char not in xhtml
    assert "Book" in xhtml
    assert "b&amp;c" in xhtml
    ET.fromstring(xhtml)


def test_cover_defaults_and_custom_ext():
    default = EpubCover()
    assert default.filename == "cover.xhtml"
    assert default.id == "cover"
    assert "../Images/cover.jpg" in default.to_xhtml()

    png = EpubCover(ext="png")
    assert "../Images/cover.png" in png.to_xhtml()
