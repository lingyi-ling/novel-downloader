#!/usr/bin/env python3
"""Offline tests for the HTML exporter (``plugins/mixins/export_html.py``)."""

from __future__ import annotations

import base64
import re
from typing import Any

from novel_downloader.libs.filesystem import (
    font_filename,
    image_filename,
    sanitize_filename,
)
from novel_downloader.libs.html_builder import HtmlBuilder
from novel_downloader.plugins.mixins.export_html import _unique_chapter_filename
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


# ---------------------------------------------------------------------------
# Sanitization collisions & robustness
# ---------------------------------------------------------------------------


def test_export_book_html_sanitized_cid_collision_keeps_both(
    scenario: Scenario,
) -> None:
    # "a/b" and "a_b" both sanitize to "ca_b": the second chapter file must
    # not overwrite the first, and the index must link to both files.
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

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    chapter_files = sorted(p.name for p in (folder / "chapters").iterdir())
    assert chapter_files == ["ca_b.html", "ca_b_2.html"]

    index = (folder / "index.html").read_text("utf-8")
    assert 'href="chapters/ca_b.html"' in index
    assert 'href="chapters/ca_b_2.html"' in index

    body_a = (folder / "chapters" / "ca_b.html").read_text("utf-8")
    body_b = (folder / "chapters" / "ca_b_2.html").read_text("utf-8")
    assert "内容A" in body_a
    assert "内容B" in body_b


def test_xp_html_chapter_tolerates_none_title_and_content(
    scenario: Scenario,
) -> None:
    chapter: dict[str, object] = {
        "id": "1",
        "title": None,
        "content": None,
        "extra": {},
    }

    chapter_obj = scenario.client._xp_html_chapter(
        builder=None,  # type: ignore[arg-type]
        cid="1",
        chap_title=None,
        chap=chapter,  # type: ignore[arg-type]
        media_dir=scenario.output_dir,
    )

    assert chapter_obj.filename == "c1.html"
    assert chapter_obj.title == ""
    assert chapter_obj.content == ""


# ---------------------------------------------------------------------------
# Media resources: images (``_append_image`` and the resource loop)
# ---------------------------------------------------------------------------

IMG_TAG = '<img src="../media/{fname}" alt="{alt}" class="chapter-image"/>'


def _render(
    scenario: Scenario,
    chap: dict[str, Any],
    *,
    cid: str = "1",
) -> tuple[Any, HtmlBuilder]:
    """Render one chapter through ``_xp_html_chapter`` with a real builder."""
    builder = HtmlBuilder(title="书")
    chapter_obj = scenario.client._xp_html_chapter(
        builder=builder,
        cid=cid,
        chap_title=None,
        chap=chap,  # type: ignore[arg-type]
        media_dir=scenario.book_dir / "media",
    )
    return chapter_obj, builder


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_xp_html_chapter_image_from_url(scenario: Scenario) -> None:
    url = "https://example.com/pic1.png"
    scenario.write_media(image_filename(url), PNG_BYTES)

    chapter_obj, builder = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "第一行",
            "extra": {
                "resources": [
                    {"type": "image", "paragraph_index": 0, "url": url, "alt": "插图"}
                ]
            },
        },
    )

    # paragraph_index 0 places the image before the first paragraph.
    assert chapter_obj.content.splitlines() == [
        IMG_TAG.format(fname="img_0.png", alt="插图"),
        "<p>第一行</p>",
    ]
    assert [img.filename for img in builder.images] == ["img_0.png"]


def test_xp_html_chapter_image_protocol_relative_url(scenario: Scenario) -> None:
    # ``//example.com/...`` is upgraded to ``https://`` before name hashing.
    url = "https://cdn.example.com/pic.png"
    scenario.write_media(image_filename(url), PNG_BYTES)

    chapter_obj, _ = _render(
        scenario,
        {
            "id": "1",
            "title": None,
            "content": "行",
            "extra": {
                "resources": [
                    {
                        "type": "image",
                        "paragraph_index": 0,
                        "url": "//cdn.example.com/pic.png",
                    }
                ]
            },
        },
    )

    assert IMG_TAG.format(fname="img_0.png", alt="image") in chapter_obj.content


def test_xp_html_chapter_image_from_base64(scenario: Scenario) -> None:
    chapter_obj, builder = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行",
            "extra": {
                "resources": [
                    {
                        "type": "image",
                        "paragraph_index": 0,
                        "base64": _b64(PNG_BYTES),
                        "mime": "image/png",
                    }
                ]
            },
        },
    )

    assert IMG_TAG.format(fname="img_0.png", alt="image") in chapter_obj.content
    assert builder.images[0].data == PNG_BYTES


def test_xp_html_chapter_image_invalid_base64_is_swallowed(
    scenario: Scenario,
) -> None:
    chapter_obj, builder = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行",
            "extra": {
                "resources": [
                    # "abcde" has length 5: b64decode always raises.
                    {"type": "image", "paragraph_index": 0, "base64": "abcde"}
                ]
            },
        },
    )

    assert "<img" not in chapter_obj.content
    assert builder.images == []
    assert chapter_obj.content == "<p>行</p>"


def test_xp_html_chapter_image_unusable_url_skipped(scenario: Scenario) -> None:
    chapter_obj, builder = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行",
            "extra": {
                "resources": [
                    # Non-http scheme, missing local file, and no payload at
                    # all: none of these may render an <img> tag.
                    {"type": "image", "paragraph_index": 0, "url": "ftp://e.com/x.png"},
                    {
                        "type": "image",
                        "paragraph_index": 0,
                        "url": "https://example.com/gone.png",
                    },
                    {"type": "image", "paragraph_index": 0},
                ]
            },
        },
    )

    assert "<img" not in chapter_obj.content
    assert builder.images == []


def test_xp_html_chapter_image_placed_after_paragraph(scenario: Scenario) -> None:
    first, second = PNG_BYTES + b"a", PNG_BYTES + b"b"

    chapter_obj, builder = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行一\n行二\n行三",
            "extra": {
                "resources": [
                    {"type": "image", "paragraph_index": 1, "base64": _b64(first)},
                    {"type": "image", "paragraph_index": 2, "base64": _b64(second)},
                ]
            },
        },
    )

    assert chapter_obj.content.splitlines() == [
        "<p>行一</p>",
        IMG_TAG.format(fname="img_0.png", alt="image"),
        "<p>行二</p>",
        IMG_TAG.format(fname="img_1.png", alt="image"),
        "<p>行三</p>",
    ]
    assert [img.filename for img in builder.images] == ["img_0.png", "img_1.png"]


def test_xp_html_chapter_image_index_clamped_to_last_line(
    scenario: Scenario,
) -> None:
    clamped, defaulted = PNG_BYTES + b"a", PNG_BYTES + b"b"

    chapter_obj, _ = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行一",
            "extra": {
                "resources": [
                    # Index beyond the paragraph count and a missing index
                    # both land after the last (only) line.
                    {"type": "image", "paragraph_index": 99, "base64": _b64(clamped)},
                    {"type": "image", "base64": _b64(defaulted)},
                ]
            },
        },
    )

    assert chapter_obj.content.splitlines() == [
        "<p>行一</p>",
        IMG_TAG.format(fname="img_0.png", alt="image"),
        IMG_TAG.format(fname="img_1.png", alt="image"),
    ]


# ---------------------------------------------------------------------------
# Media resources: fonts
# ---------------------------------------------------------------------------

FONT_BYTES = b"fake-font-payload-for-tests"


def test_xp_html_chapter_font_from_url(scenario: Scenario) -> None:
    url = "https://example.com/body.ttf"
    scenario.write_media(font_filename(url), FONT_BYTES)

    chapter_obj, builder = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行",
            "extra": {"resources": [{"type": "font", "url": url}]},
        },
    )

    assert [f.filename for f in chapter_obj.fonts] == ["font_0.ttf"]
    assert chapter_obj.fonts[0].data == FONT_BYTES
    assert [f.filename for f in builder.fonts] == ["font_0.ttf"]
    # Fonts ride along with the chapter; no inline HTML is emitted.
    assert chapter_obj.content == "<p>行</p>"


def test_xp_html_chapter_font_from_base64(scenario: Scenario) -> None:
    chapter_obj, _ = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行",
            "extra": {"resources": [{"type": "font", "base64": _b64(FONT_BYTES)}]},
        },
    )

    assert [f.filename for f in chapter_obj.fonts] == ["font_0.bin"]
    assert chapter_obj.fonts[0].data == FONT_BYTES


def test_xp_html_chapter_font_unusable_resources_skipped(
    scenario: Scenario,
) -> None:
    chapter_obj, _ = _render(
        scenario,
        {
            "id": "1",
            "title": "第一章",
            "content": "行",
            "extra": {
                "resources": [
                    # URL whose local file was never cached, and a payload
                    # that is not valid base64: both must be swallowed.
                    {"type": "font", "url": "https://example.com/gone.ttf"},
                    {"type": "font", "base64": "abcde"},
                ]
            },
        },
    )

    assert chapter_obj.fonts == []


# ---------------------------------------------------------------------------
# Media resources through the full export pipeline
# ---------------------------------------------------------------------------


def test_export_book_html_image_resource_written(scenario: Scenario) -> None:
    url = "https://example.com/pic1.png"
    scenario.write_media(image_filename(url), PNG_BYTES)
    scenario.write_book_info(
        make_book_info(
            name="插图书",
            volumes=[make_volume("卷", [make_chapter_info("1", "章")])],
        )
    )
    scenario.write_chapters(
        [
            make_chapter(
                "1",
                "行一\n行二",
                extra={
                    "resources": [
                        {"type": "image", "paragraph_index": 1, "url": url, "alt": "图"}
                    ]
                },
            )
        ]
    )

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    assert (folder / "media" / "img_0.png").is_file()
    chapter = (folder / "chapters" / "c1.html").read_text("utf-8")
    assert '<img src="../media/img_0.png" alt="图"' in chapter


# ---------------------------------------------------------------------------
# Early-return boundaries
# ---------------------------------------------------------------------------


def test_export_book_html_missing_book_dir_returns_empty(scenario: Scenario) -> None:
    assert scenario.export("html", ExporterConfig())["html"] == []


def test_export_book_html_volume_without_chapter_ids_skipped(
    scenario: Scenario,
) -> None:
    scenario.write_book_info(
        make_book_info(
            volumes=[
                make_volume("空卷", []),
                make_volume(
                    "无ID卷", [{"title": "无ID", "url": "https://example.com/x"}]
                ),
                make_volume("实卷", [make_chapter_info("1", "章")]),
            ]
        )
    )
    scenario.write_chapters([make_chapter("1", "内容")])

    folder = scenario.export("html", ExporterConfig(append_timestamp=False))["html"][0]
    assert folder.is_dir()
    assert sorted(p.name for p in (folder / "chapters").iterdir()) == ["c1.html"]


def test_export_chapter_html_missing_book_dir_returns_none(
    scenario: Scenario,
) -> None:
    assert scenario.export_chapter("1", "html")["html"] is None


def test_export_chapter_html_missing_database_returns_none(
    scenario: Scenario,
) -> None:
    # Book metadata exists, but no chapter sqlite was ever written.
    scenario.write_book_info(make_book_info())
    assert scenario.export_chapter("1", "html")["html"] is None


# ---------------------------------------------------------------------------
# Filename collision helpers
# ---------------------------------------------------------------------------


def test_unique_chapter_filename_without_extension() -> None:
    used: set[str] = {"name"}
    assert _unique_chapter_filename("name", used) == "name_2"
    assert used == {"name", "name_2"}


def test_unique_chapter_filename_skips_taken_suffixes() -> None:
    used = {"a.html", "a_2.html"}
    assert _unique_chapter_filename("a.html", used) == "a_3.html"
    assert used == {"a.html", "a_2.html", "a_3.html"}


def test_unique_chapter_filename_first_use_unchanged() -> None:
    used: set[str] = set()
    assert _unique_chapter_filename("a.html", used) == "a.html"
    assert _unique_chapter_filename("a.html", used) == "a_2.html"
