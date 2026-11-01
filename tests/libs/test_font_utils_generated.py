#!/usr/bin/env python3
"""tests.libs.test_font_utils_generated
--------------------------------------

Coverage for :mod:`novel_downloader.libs.font_utils` that does not depend on
font files checked into ``tests/data`` (that directory is absent, which makes
``test_font_utils.py`` skip entirely and leaves the module at ~49%).

A minimal TTF is built in-memory with fontTools — the same approach used by
``tests/plugins/test_qdfont.py`` — so these tests run everywhere the optional
``image-utils`` extra is installed.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from novel_downloader.libs.font_utils import (
    extract_font_charset,
    extract_font_charset_bytes,
    load_render_font,
    load_render_font_bytes,
    render_char_image,
    render_char_image_array,
    render_text_image,
)

CHARS = ["A", "B", "中"]


def _require_font_deps() -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("fontTools")
    pytest.importorskip("PIL")


def _build_ttf() -> bytes:
    """Build a minimal deterministic TTF whose cmap covers exactly CHARS."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(unitsPerEm=1000, isTTF=True)
    glyph_names = [".notdef"] + [f"g{ord(c)}" for c in CHARS]
    fb.setupGlyphOrder(glyph_names)
    fb.setupCharacterMap({ord(c): f"g{ord(c)}" for c in CHARS})

    glyphs = {}
    for name in glyph_names:
        pen = TTGlyphPen(None)
        if name != ".notdef":
            pen.moveTo((50, 0))
            pen.lineTo((50, 700))
            pen.lineTo((450, 700))
            pen.lineTo((450, 0))
            pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(dict.fromkeys(glyph_names, (500, 0)))
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupOS2(
        sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
    )
    fb.setupNameTable({"familyName": "FontUtilsTest", "styleName": "Regular"})
    fb.setupPost()

    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def ttf_bytes() -> bytes:
    _require_font_deps()
    return _build_ttf()


@pytest.fixture()
def font_path(tmp_path: Path, ttf_bytes: bytes) -> Path:
    path = tmp_path / "generated.ttf"
    path.write_bytes(ttf_bytes)
    return path


@pytest.fixture()
def render_font(ttf_bytes: bytes):
    from PIL import ImageFont

    return ImageFont.truetype(io.BytesIO(ttf_bytes), 32)


# --------------------------------------------------------------------------- #
# render helpers
# --------------------------------------------------------------------------- #


def test_render_char_image_produces_non_blank_rgb_square(render_font) -> None:
    from PIL import Image

    img = render_char_image("A", render_font, size=64)

    assert isinstance(img, Image.Image)
    assert img.mode == "RGB"
    assert img.size == (64, 64)
    # The glyph rectangle is filled with black pixels.
    colors = img.getcolors()
    assert colors is not None
    assert any(count > 0 and value != (255, 255, 255) for count, value in colors)


def test_render_char_image_missing_glyph_yields_blank_canvas(render_font) -> None:
    img = render_char_image("5", render_font, size=64)

    assert img.size == (64, 64)
    colors = img.getcolors()
    assert colors is not None
    # Nothing mapped: the canvas stays pure white.
    assert colors == [(64 * 64, (255, 255, 255))]


def test_render_char_image_reflect_flips_horizontally(render_font) -> None:
    import numpy as np

    plain = np.array(render_char_image("A", render_font, size=64))
    flipped = np.array(render_char_image("A", render_font, size=64, is_reflect=True))

    assert not np.array_equal(plain, flipped)
    # A horizontal flip is exactly a reversed column order.
    assert np.array_equal(flipped, plain[:, ::-1, :])


def test_render_char_image_array_shape_and_values(render_font) -> None:
    import numpy as np

    arr = render_char_image_array("A", render_font, size=48)

    assert arr.shape == (48, 48, 3)
    assert arr.dtype == np.uint8
    assert arr.min() < 255  # glyph pixels present


def test_render_text_image_wraps_lines_and_sizes_canvas(render_font) -> None:
    text = "ABAB" + "ABAB"  # 8 chars -> 2 lines of 5 (wrap) at 4 per line
    img = render_text_image(text, render_font, cell_size=32, chars_per_line=4)

    assert img.size == (4 * 32, 2 * 32)


def test_render_text_image_empty_text_renders_one_line(render_font) -> None:
    img = render_text_image("", render_font, cell_size=32, chars_per_line=4)

    assert img.size == (4 * 32, 1 * 32)


# --------------------------------------------------------------------------- #
# font loading
# --------------------------------------------------------------------------- #


def test_load_render_font_returns_freetype_font(font_path: Path) -> None:
    from PIL import ImageFont

    font = load_render_font(str(font_path), char_size=20)

    assert isinstance(font, ImageFont.FreeTypeFont)


def test_load_render_font_bytes_returns_freetype_font(ttf_bytes: bytes) -> None:
    from PIL import ImageFont

    font = load_render_font_bytes(ttf_bytes, char_size=20)

    assert isinstance(font, ImageFont.FreeTypeFont)


# --------------------------------------------------------------------------- #
# charset extraction
# --------------------------------------------------------------------------- #


def test_extract_font_charset_matches_cmap(font_path: Path) -> None:
    charset = extract_font_charset(str(font_path))

    assert charset == set(CHARS)


def test_extract_font_charset_bytes_matches_cmap(ttf_bytes: bytes) -> None:
    charset = extract_font_charset_bytes(ttf_bytes)

    assert charset == set(CHARS)
