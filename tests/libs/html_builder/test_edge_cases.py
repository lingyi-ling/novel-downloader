"""End-to-end and edge-case tests for the standalone HTML builder."""

import io
import re

import pytest

from novel_downloader.libs.html_builder.core import HtmlBuilder
from novel_downloader.libs.html_builder.models import (
    HtmlChapter,
    HtmlFont,
    HtmlVolume,
    IndexDocument,
)

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@pytest.fixture
def fake_templates(tmp_path, monkeypatch):
    css_index = tmp_path / "index.css"
    css_index.write_text("/*index*/", encoding="utf-8")
    css_chapter = tmp_path / "chapter.css"
    css_chapter.write_text("/*chapter*/", encoding="utf-8")
    js_main = tmp_path / "main.js"
    js_main.write_text("//js", encoding="utf-8")

    monkeypatch.setattr(
        "novel_downloader.libs.html_builder.core.HTML_CSS_INDEX_PATH",
        css_index,
    )
    monkeypatch.setattr(
        "novel_downloader.libs.html_builder.core.HTML_CSS_CHAPTER_PATH",
        css_chapter,
    )
    monkeypatch.setattr(
        "novel_downloader.libs.html_builder.core.HTML_JS_MAIN_PATH",
        js_main,
    )
    return tmp_path


def png_bytes(color=(10, 20, 30), size=(3, 3)) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------------------------------------------------
# Empty book / folder layout
# ----------------------------------------------------------------------
def test_empty_book_exports_index_and_static_dirs(tmp_path, fake_templates):
    builder = HtmlBuilder("Empty Book")
    out = builder.export(tmp_path)

    assert out == tmp_path / "Empty Book"
    assert (out / "index.html").exists()
    assert (out / "css" / "index.css").exists()
    assert (out / "css" / "chapter.css").exists()
    assert (out / "js" / "main.js").exists()
    assert (out / "chapters").is_dir()
    assert list((out / "chapters").iterdir()) == []


def test_export_returns_output_directory(tmp_path, fake_templates):
    out = HtmlBuilder("Book").export(tmp_path, folder="Custom")
    assert out == tmp_path / "Custom"
    assert out.is_dir()


def test_output_folder_name_is_sanitized(tmp_path, fake_templates):
    assert HtmlBuilder("a/b").export(tmp_path) == tmp_path / "a_b"
    assert HtmlBuilder("Book").export(tmp_path, folder="c/d") == tmp_path / "c_d"


# ----------------------------------------------------------------------
# Cover / media
# ----------------------------------------------------------------------
def test_real_png_cover_detected_and_written(tmp_path, fake_templates):
    cover = png_bytes((200, 10, 20))
    builder = HtmlBuilder("Book", cover=cover)
    out = builder.export(tmp_path)

    cover_file = out / "media" / "cover.png"
    assert cover_file.read_bytes() == cover
    index = (out / "index.html").read_text("utf-8")
    assert 'src="media/cover.png"' in index


def test_cover_falls_back_to_jpg_for_unknown_bytes(tmp_path, fake_templates):
    builder = HtmlBuilder("Book", cover=b"< 12 bytes")
    assert builder._cover_filename == "cover.jpg"
    out = builder.export(tmp_path)
    assert (out / "media" / "cover.jpg").read_bytes() == b"< 12 bytes"


def test_media_directory_contains_added_images(tmp_path, fake_templates):
    data = png_bytes((1, 2, 3))
    builder = HtmlBuilder("Book")
    name = builder.add_image_bytes(data)

    out = builder.export(tmp_path)
    assert name == "img_0.png"
    assert (out / "media" / name).read_bytes() == data


def test_image_dedup_between_bytes_and_file(tmp_path, fake_templates):
    data = png_bytes((4, 5, 6))
    src = tmp_path / "same.png"
    src.write_bytes(data)

    builder = HtmlBuilder("Book")
    name1 = builder.add_image_bytes(data)
    name2 = builder.add_image(src)

    assert name1 == name2
    assert len(builder.images) == 1


def test_image_bytes_mime_fallback(tmp_path, fake_templates):
    builder = HtmlBuilder("Book")
    # Too short for magic detection; MIME type supplies the extension.
    name = builder.add_image_bytes(b"not-a-real-image!!", mime_type="image/png")
    assert name == "img_0.png"

    out = builder.export(tmp_path)
    assert (out / "media" / name).read_bytes() == b"not-a-real-image!!"


# ----------------------------------------------------------------------
# Volumes / chapters / navigation
# ----------------------------------------------------------------------
def test_volume_toc_and_chapter_links(tmp_path, fake_templates):
    builder = HtmlBuilder("Book")
    ch1 = HtmlChapter(filename="1.html", title="First", content="A")
    ch2 = HtmlChapter(filename="2.html", title="Second", content="B")
    builder.add_volume(
        HtmlVolume(title="Vol & <1>", intro="Intro & <2>", chapters=[ch1, ch2])
    )

    out = builder.export(tmp_path)

    index = (out / "index.html").read_text("utf-8")
    assert "Vol &amp; &lt;1&gt;" in index
    assert "Intro &amp; &lt;2&gt;" in index
    assert "chapters/1.html" in index
    assert "chapters/2.html" in index

    first = (out / "chapters" / "1.html").read_text("utf-8")
    second = (out / "chapters" / "2.html").read_text("utf-8")

    assert 'data-prev=""' in first
    assert 'data-next="2.html"' in first
    assert 'data-prev="1.html"' in second
    assert 'data-next=""' in second
    # menu link always present
    assert 'data-menu="../index.html"' in first


def test_duplicate_chapter_titles_are_all_kept(tmp_path, fake_templates):
    builder = HtmlBuilder("Book")
    builder.add_chapter(HtmlChapter(filename="1.html", title="Same", content="A"))
    builder.add_chapter(HtmlChapter(filename="2.html", title="Same", content="B"))

    out = builder.export(tmp_path)
    index = (out / "index.html").read_text("utf-8")

    matches = re.findall(r'<li><a href="chapters/[12]\.html">Same</a></li>', index)
    assert len(matches) == 2


def test_chapter_prev_next_are_escaped():
    chap = HtmlChapter(filename="c.html", title="T", content="C")
    html = chap.to_html(prev_link="p&1.html", next_link='n"2.html')

    assert 'data-prev="p&amp;1.html"' in html
    assert 'data-next="n&quot;2.html"' in html
    assert 'href="p&amp;1.html"' in html
    assert 'href="n&quot;2.html"' in html


def test_toc_href_is_escaped_for_special_filenames():
    idx = IndexDocument(title="Book")
    idx.add_chapter(HtmlChapter(filename="a&b.html", title="T", content=""))
    idx.add_volume(
        HtmlVolume(
            title="V",
            chapters=[HtmlChapter(filename="c&d.html", title="C", content="")],
        )
    )

    html = idx.to_html()
    assert 'href="chapters/a&amp;b.html"' in html
    assert 'href="chapters/c&amp;d.html"' in html


# ----------------------------------------------------------------------
# Escaping / illegal characters
# ----------------------------------------------------------------------
def test_metadata_and_chapter_escaping(tmp_path, fake_templates):
    builder = HtmlBuilder(
        'A & <B> "C"',
        author="O'Reilly & Sons",
        description="Desc <&>",
        subject=["t&1"],
        serial_status="连载 & more",
        word_count="1000",
    )
    builder.add_chapter(
        HtmlChapter(
            filename="1.html",
            title="Ch & <One>",
            content="<p>Body &amp; text</p>",
        )
    )

    out = builder.export(tmp_path)
    index = (out / "index.html").read_text("utf-8")
    chapter = (out / "chapters" / "1.html").read_text("utf-8")

    assert "<title>A &amp; &lt;B&gt; &quot;C&quot;</title>" in index
    assert "O&#x27;Reilly &amp; Sons" in index
    assert "Desc &lt;&amp;&gt;" in index
    assert "t&amp;1" in index

    assert "<title>Ch &amp; &lt;One&gt;</title>" in chapter
    assert "<p>Body &amp; text</p>" in chapter


def test_illegal_control_chars_are_removed(tmp_path, fake_templates):
    builder = HtmlBuilder(
        "A\x01&<B>",
        author="Au\x0b",
        description="D\x1f&",
    )
    builder.add_chapter(
        HtmlChapter(
            filename="1.html",
            title="T\x01&<",
            content="<p>x\x0c</p>",
            extra_content="<i>e\x00</i>",
        )
    )

    out = builder.export(tmp_path)
    index = (out / "index.html").read_text("utf-8")
    chapter = (out / "chapters" / "1.html").read_text("utf-8")

    for text in (index, chapter):
        assert CONTROL_CHARS.search(text) is None

    assert "<title>A&amp;&lt;B&gt;</title>" in index
    assert "<p>x</p>" in chapter
    assert "<i>e</i>" in chapter


# ----------------------------------------------------------------------
# Fonts
# ----------------------------------------------------------------------
def test_unknown_font_bytes_get_bin_extension_and_default_format():
    builder = HtmlBuilder("Book")
    font = builder.add_font_bytes(b"this is not a real font")
    assert font is not None
    assert font.filename == "font_0.bin"
    assert font._font_format() == "truetype"

    css = font.build_css(font_url_prefix="../fonts/")
    assert 'url("../fonts/font_0.bin")' in css
    assert 'format("truetype")' in css


def test_html_font_custom_selectors_and_fallback_stack():
    font = HtmlFont(filename="f.woff2", data=b"x", family="Fam", selectors=(".a", ".b"))
    chapter = HtmlChapter(filename="c.html", title="T", content="C", fonts=[font])
    html = chapter.to_html()

    assert "@font-face" in html
    assert ".a { font-family:" in html
    assert ".b { font-family:" in html
    assert "system-ui" in html
