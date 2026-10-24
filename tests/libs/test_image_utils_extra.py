"""Additional image_utils coverage: optional deps, decoding, pure helpers."""

import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import novel_downloader.libs.image_utils as image_utils
from novel_downloader.libs.image_utils import (
    concat_image_slices_vertical,
    crop_chars_region,
    encode_image_array,
    filter_gray_watermark,
    filter_orange_watermark,
    is_empty_image,
    is_new_paragraph,
    load_image_array_bytes,
    load_image_array_path,
    split_by_height,
    split_by_white_lines,
)

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


# ----------------------------------------------------------------------
# Helpers: generate real image bytes with Pillow (no network)
# ----------------------------------------------------------------------
def png_bytes(mode="RGB", color=(10, 20, 30), size=(4, 3)) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes(color=(200, 30, 40), size=(5, 4)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ----------------------------------------------------------------------
# Optional dependency handling
# ----------------------------------------------------------------------
def test_missing_deps_raise_actionable_runtime_error(monkeypatch):
    monkeypatch.setattr(image_utils, "np", None)
    monkeypatch.setattr(image_utils, "Image", None)

    with pytest.raises(RuntimeError, match="numpy"):
        load_image_array_bytes(b"whatever")
    with pytest.raises(RuntimeError, match="Pillow"):
        is_empty_image(np.zeros((1, 1, 3), dtype=np.uint8))


def test_module_imports_and_degrades_without_numpy_and_pillow(tmp_path):
    """Importing the module must not require the optional image extra."""
    repo = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import importlib.abc

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in ("numpy", "PIL"):
                    raise ImportError("blocked: " + fullname)
                return None

        import sys
        sys.meta_path.insert(0, Blocker())

        import novel_downloader.libs.image_utils as m

        assert m.np is None
        assert m.Image is None

        try:
            m.load_image_array_bytes(b"not-an-image")
        except RuntimeError as exc:
            msg = str(exc)
            assert "numpy" in msg and "Pillow" in msg
        else:
            raise AssertionError("expected RuntimeError")

        print("optional-deps-ok")
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo / "src")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "optional-deps-ok" in result.stdout


# ----------------------------------------------------------------------
# Real image decoding
# ----------------------------------------------------------------------
def test_load_real_png_bytes():
    arr = load_image_array_bytes(png_bytes(color=(1, 2, 3), size=(7, 5)))
    assert arr.shape == (5, 7, 3)
    assert arr.dtype == np.uint8
    assert arr[0, 0].tolist() == [1, 2, 3]


def test_load_real_jpeg_bytes():
    arr = load_image_array_bytes(jpeg_bytes(color=(220, 20, 30), size=(6, 4)))
    assert arr.shape == (4, 6, 3)
    # JPEG is lossy but this large flat block keeps its hue.
    assert arr[..., 0].mean() > 180
    assert arr[..., 1].mean() < 60


def test_load_real_png_from_path(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(png_bytes(color=(7, 8, 9), size=(2, 3)))
    arr = load_image_array_path(p)
    assert arr.shape == (3, 2, 3)
    assert arr[0, 0].tolist() == [7, 8, 9]


def test_load_rgba_transparent_flattened_onto_white():
    # Fully transparent red: flattening must produce pure white.
    data = png_bytes(mode="RGBA", color=(255, 0, 0, 0), size=(2, 2))
    arr = load_image_array_bytes(data, white_bg=True)
    assert np.all(arr == 255)


def test_load_rgba_semi_transparent_is_blended():
    data = png_bytes(mode="RGBA", color=(255, 0, 0, 128), size=(1, 1))
    arr = load_image_array_bytes(data, white_bg=True)
    # Half-red over white -> around (255, 127, 127)
    r, g, b = arr[0, 0].tolist()
    assert r == 255
    assert 120 <= g <= 135
    assert 120 <= b <= 135


def test_load_rgba_without_white_bg_drops_alpha():
    data = png_bytes(mode="RGBA", color=(10, 20, 30, 0), size=(1, 1))
    arr = load_image_array_bytes(data, white_bg=False)
    assert arr[0, 0].tolist() == [10, 20, 30]


def test_load_palette_png():
    img = Image.new("P", (3, 2))
    img.putpalette([255, 0, 0, 0, 0, 255] + [0, 0, 0] * 254)
    img.putdata([0, 1, 0, 1, 0, 1])
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    arr = load_image_array_bytes(buf.getvalue())
    assert arr.shape == (2, 3, 3)
    assert arr[0, 0].tolist() == [255, 0, 0]
    assert arr[0, 1].tolist() == [0, 0, 255]


def test_load_invalid_bytes_raises():
    # PIL.UnidentifiedImageError is an OSError subclass.
    with pytest.raises(OSError):
        load_image_array_bytes(b"this is definitely not an image")


def test_load_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_image_array_path(tmp_path / "missing.png")


# ----------------------------------------------------------------------
# Watermark filters
# ----------------------------------------------------------------------
def test_filter_orange_modifies_in_place_and_keeps_other_colors():
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    img[0, 0] = [255, 140, 0]  # orange
    img[0, 1] = [0, 0, 255]  # blue
    img[1, 0] = [0, 255, 0]  # green
    img[1, 1] = [255, 255, 255]  # white

    out = filter_orange_watermark(img)
    assert out is img
    assert img[0, 0].tolist() == [255, 255, 255]
    assert img[0, 1].tolist() == [0, 0, 255]
    assert img[1, 0].tolist() == [0, 255, 0]
    assert img[1, 1].tolist() == [255, 255, 255]


def test_filter_orange_ignores_dark_orange():
    img = np.full((1, 1, 3), [100, 50, 0], dtype=np.uint8)
    filter_orange_watermark(img)
    assert img[0, 0].tolist() == [100, 50, 0]


def test_filter_gray_custom_threshold_and_background():
    img = np.array(
        [
            [[230, 230, 230], [100, 100, 100]],
            [[250, 250, 250], [199, 199, 199]],
        ],
        dtype=np.uint8,
    )
    out = filter_gray_watermark(img, threshold=200, background=(0, 0, 0))
    assert out is img
    # sum > 600 -> replaced; 199*3 = 597 stays.
    assert img[0, 0].tolist() == [0, 0, 0]
    assert img[0, 1].tolist() == [100, 100, 100]
    assert img[1, 0].tolist() == [0, 0, 0]
    assert img[1, 1].tolist() == [199, 199, 199]


# ----------------------------------------------------------------------
# Splitting / cropping
# ----------------------------------------------------------------------
def test_split_by_height_offsets_and_ignore():
    img = np.zeros((100, 20, 3), dtype=np.uint8)
    chunks = split_by_height(
        img, height=30, top_offset=10, bottom_offset=10, per_chunk_top_ignore=0
    )
    assert [c.shape[0] for c in chunks] == [30, 30, 30]
    assert chunks[0][0, 0].tolist() == img[10, 0].tolist()


def test_split_by_height_default_per_chunk_ignore():
    img = np.zeros((100, 20, 3), dtype=np.uint8)
    chunks = split_by_height(img, height=30, top_offset=0, bottom_offset=0)
    assert [c.shape[0] for c in chunks] == [20, 20, 20, 10]


def test_split_by_height_offsets_exceed_image():
    img = np.zeros((10, 5, 3), dtype=np.uint8)
    assert split_by_height(img, top_offset=20, bottom_offset=0) == []


def test_split_by_white_lines_handles_top_and_bottom_content():
    block_a = np.zeros((3, 6, 3), dtype=np.uint8)
    white = np.full((2, 6, 3), 255, dtype=np.uint8)
    block_b = np.ones((2, 6, 3), dtype=np.uint8)

    img = np.vstack([block_a, white, block_b])
    blocks = split_by_white_lines(img, padding=1)

    assert [b.shape[0] for b in blocks] == [5, 4]
    assert np.all(blocks[0][:1] == 255)
    assert np.all(blocks[0][-1:] == 255)
    assert np.all(blocks[1][:1] == 255)


def test_split_by_white_lines_all_white_returns_empty():
    img = np.full((20, 10, 3), 255, dtype=np.uint8)
    assert split_by_white_lines(img) == []


def test_split_by_white_lines_single_content_row():
    img = np.zeros((1, 4, 3), dtype=np.uint8)
    blocks = split_by_white_lines(img, padding=2)
    assert len(blocks) == 1
    assert blocks[0].shape == (5, 4, 3)


def test_crop_chars_region_clamps_to_width():
    img = np.zeros((4, 20, 3), dtype=np.uint8)
    assert crop_chars_region(img, num_chars=99, left_margin=5, char_width=10).shape == (
        4,
        20,
        3,
    )


def test_crop_chars_region_zero_chars():
    img = np.zeros((4, 20, 3), dtype=np.uint8)
    assert crop_chars_region(img, num_chars=0, left_margin=5, char_width=10).shape == (
        4,
        5,
        3,
    )


# ----------------------------------------------------------------------
# Paragraph / blank detection
# ----------------------------------------------------------------------
def test_is_empty_image_with_alpha_channel():
    rgba = np.full((2, 2, 4), 255, dtype=np.uint8)
    assert is_empty_image(rgba) is True

    rgba[0, 0, 3] = 0
    assert is_empty_image(rgba) is False


def test_is_new_paragraph_threshold():
    img = np.full((3, 40, 3), 240, dtype=np.uint8)
    assert is_new_paragraph(img, white_threshold=250, paragraph_threshold=20) is False
    assert is_new_paragraph(img, white_threshold=240, paragraph_threshold=20) is True


# ----------------------------------------------------------------------
# Encoding helpers
# ----------------------------------------------------------------------
def test_encode_default_format_is_jpeg():
    arr = np.full((4, 5, 3), 128, dtype=np.uint8)
    data = encode_image_array(arr)
    assert data.startswith(b"\xff\xd8")

    loaded = np.array(Image.open(io.BytesIO(data)))
    assert loaded.shape == (4, 5, 3)


def test_concat_mismatched_widths_raises():
    a = np.zeros((2, 4, 3), dtype=np.uint8)
    b = np.zeros((2, 5, 3), dtype=np.uint8)
    with pytest.raises(ValueError):
        concat_image_slices_vertical([a, b])
