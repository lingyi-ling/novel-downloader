"""Round-4 hardening tests for ``novel_downloader.libs.image_utils``.

Covers edge cases that the original suites did not exercise: union/difference
boundaries in slicing, tiny/single-pixel images, alpha and palette handling,
grayscale, EXIF orientation, oversized-image protection, and the standalone
image health check.
"""

import io

import pytest

import novel_downloader.libs.image_utils as image_utils
from novel_downloader.libs.image_utils import (
    ImageHealth,
    check_image_health,
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
# Helpers
# ----------------------------------------------------------------------
def png_bytes(mode="RGB", size=(4, 3), color=None, **save_kwargs) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG", **save_kwargs)
    return buf.getvalue()


def palette_png(transparency: int | None = None) -> bytes:
    img = Image.new("P", (2, 1))
    img.putpalette([255, 0, 0, 0, 0, 255] + [0, 0, 0] * 254)
    img.putdata([0, 1])
    if transparency is not None:
        img.info["transparency"] = transparency
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def oriented_jpeg(orientation: int, size=(4, 2)) -> bytes:
    width, height = size
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, : width // 2] = [255, 0, 0]
    arr[:, width // 2 :] = [0, 0, 255]
    img = Image.fromarray(arr)
    exif = Image.Exif()
    exif[274] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=100, exif=exif)
    return buf.getvalue()


def assert_red(pixel) -> None:
    r, g, b = pixel[:3]
    assert r > 200 and g < 60 and b < 60


def assert_blue(pixel) -> None:
    r, g, b = pixel[:3]
    assert b > 200 and r < 60 and g < 60


# ----------------------------------------------------------------------
# Decoding: modes, palette, EXIF, truncation
# ----------------------------------------------------------------------
@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_load_applies_exif_orientation(orientation):
    arr = load_image_array_bytes(oriented_jpeg(orientation))

    if orientation == 1:
        assert arr.shape == (2, 4, 3)
        assert_red(arr[0, 0])
        assert_blue(arr[0, -1])
    elif orientation == 3:
        assert arr.shape == (2, 4, 3)
        assert_blue(arr[0, 0])
        assert_red(arr[0, -1])
    elif orientation == 6:
        # 90 degrees clockwise: red (left) becomes top, blue becomes bottom.
        assert arr.shape == (4, 2, 3)
        assert_red(arr[0, 0])
        assert_blue(arr[-1, 0])
    else:  # orientation 8
        assert arr.shape == (4, 2, 3)
        assert_blue(arr[0, 0])
        assert_red(arr[-1, 0])


def test_load_path_applies_exif_orientation(tmp_path):
    path = tmp_path / "rotated.jpg"
    path.write_bytes(oriented_jpeg(6))
    arr = load_image_array_path(path)
    assert arr.shape == (4, 2, 3)
    assert_red(arr[0, 0])
    assert_blue(arr[-1, 0])


def test_load_palette_transparency_flattens_onto_white():
    arr = load_image_array_bytes(palette_png(transparency=0), white_bg=True)
    assert arr[0, 0].tolist() == [255, 255, 255]  # transparent palette slot
    assert arr[0, 1].tolist() == [0, 0, 255]


def test_load_palette_transparency_without_white_bg_drops_alpha():
    arr = load_image_array_bytes(palette_png(transparency=0))
    assert arr[0, 0].tolist() == [255, 0, 0]
    assert arr[0, 1].tolist() == [0, 0, 255]


def test_load_palette_without_transparency():
    arr = load_image_array_bytes(palette_png())
    assert arr[0, 0].tolist() == [255, 0, 0]
    assert arr[0, 1].tolist() == [0, 0, 255]


def test_load_grayscale_replicates_to_rgb():
    arr = load_image_array_bytes(png_bytes("L", (3, 2), 123))
    assert arr.shape == (2, 3, 3)
    assert np.all(arr == 123)


def test_load_la_flattens_alpha_onto_white():
    la = np.zeros((1, 2, 2), dtype=np.uint8)
    la[0, 0] = [10, 0]
    la[0, 1] = [10, 255]
    data = io.BytesIO()
    Image.fromarray(la, mode="LA").save(data, format="PNG")

    arr = load_image_array_bytes(data.getvalue(), white_bg=True)
    assert arr[0, 0].tolist() == [255, 255, 255]
    assert arr[0, 1].tolist() == [10, 10, 10]


def test_load_one_bit_image():
    arr = load_image_array_bytes(png_bytes("1", (3, 2), 1))
    assert arr.shape == (2, 3, 3)
    assert np.all(arr == 255)


def test_load_cmyk_image():
    img = Image.new("CMYK", (2, 2), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="TIFF")
    arr = load_image_array_bytes(buf.getvalue())
    assert arr.shape == (2, 2, 3)


def test_load_truncated_png_raises_oserror():
    data = png_bytes(size=(20, 20), color=(1, 2, 3))
    with pytest.raises(OSError):
        load_image_array_bytes(data[:-24])


def test_load_empty_bytes_raises():
    with pytest.raises(OSError):
        load_image_array_bytes(b"")


# ----------------------------------------------------------------------
# Watermark filters: alpha, emptiness, invalid shapes
# ----------------------------------------------------------------------
def test_filter_orange_rgba_preserves_alpha():
    img = np.zeros((2, 2, 4), dtype=np.uint8)
    img[0, 0] = [255, 140, 0, 7]
    img[0, 1] = [0, 0, 255, 128]

    out = filter_orange_watermark(img)
    assert out is img
    assert img[0, 0].tolist() == [255, 255, 255, 7]
    assert img[0, 1].tolist() == [0, 0, 255, 128]


def test_filter_gray_rgba_preserves_alpha():
    img = np.full((1, 1, 4), 250, dtype=np.uint8)
    img[0, 0, 3] = 3
    filter_gray_watermark(img)
    assert img[0, 0].tolist() == [255, 255, 255, 3]


@pytest.mark.parametrize("shape", [(0, 0, 3), (0, 5, 3), (5, 0, 3), (0, 0, 4)])
def test_filters_accept_empty_arrays(shape):
    orange = np.zeros(shape, dtype=np.uint8)
    gray = np.zeros(shape, dtype=np.uint8)
    assert filter_orange_watermark(orange) is orange
    assert filter_gray_watermark(gray) is gray
    assert orange.shape == shape
    assert gray.shape == shape


@pytest.mark.parametrize("shape", [(4, 4), (4, 4, 1), (4, 4, 2), (4, 4, 5)])
def test_filters_reject_non_rgb_shapes(shape):
    img = np.zeros(shape, dtype=np.uint8)
    with pytest.raises(ValueError, match="expects an"):
        filter_orange_watermark(img)
    with pytest.raises(ValueError, match="expects an"):
        filter_gray_watermark(img)


def test_filter_gray_threshold_boundary_is_strict():
    img = np.full((1, 2, 3), 200, dtype=np.uint8)
    img[0, 1] = 201
    filter_gray_watermark(img, threshold=200)
    assert img[0, 0].tolist() == [200, 200, 200]  # 600 is not > 600
    assert img[0, 1].tolist() == [255, 255, 255]


@pytest.mark.parametrize("threshold", [-1, 256, 1000])
def test_filter_gray_rejects_out_of_range_threshold(threshold):
    img = np.zeros((1, 1, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="threshold"):
        filter_gray_watermark(img, threshold=threshold)


# ----------------------------------------------------------------------
# split_by_height
# ----------------------------------------------------------------------
def test_split_by_height_union_of_chunks_covers_effective_region():
    img = np.arange(85 * 2 * 3, dtype=np.uint8).reshape(85, 2, 3)
    chunks = split_by_height(
        img, height=30, top_offset=10, bottom_offset=0, per_chunk_top_ignore=0
    )
    assert [c.shape[0] for c in chunks] == [30, 30, 15]
    np.testing.assert_array_equal(chunks[0][0], img[10])
    np.testing.assert_array_equal(chunks[1][0], img[40])
    np.testing.assert_array_equal(chunks[2][0], img[70])


def test_split_by_height_offsets_cover_whole_image():
    img = np.zeros((10, 5, 3), dtype=np.uint8)
    assert split_by_height(img, top_offset=5, bottom_offset=5) == []
    assert split_by_height(img, top_offset=20, bottom_offset=0) == []


def test_split_by_height_single_row_with_default_ignore_keeps_row():
    img = np.ones((1, 5, 3), dtype=np.uint8)
    chunks = split_by_height(img, top_offset=0, bottom_offset=0)
    assert len(chunks) == 1
    assert chunks[0].shape == (1, 5, 3)
    assert np.all(chunks[0] == 1)


def test_split_by_height_ignore_equal_to_chunk_height_keeps_chunk():
    img = np.ones((10, 5, 3), dtype=np.uint8)
    chunks = split_by_height(
        img, height=10, top_offset=0, bottom_offset=0, per_chunk_top_ignore=10
    )
    assert len(chunks) == 1
    assert chunks[0].shape[0] == 10


@pytest.mark.parametrize("height", [0, -1, -38])
def test_split_by_height_rejects_non_positive_height(height):
    img = np.zeros((10, 5, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="height must be positive"):
        split_by_height(img, height=height)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_offset": -1},
        {"bottom_offset": -1},
        {"per_chunk_top_ignore": -1},
    ],
)
def test_split_by_height_rejects_negative_offsets(kwargs):
    img = np.zeros((10, 5, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="must be >= 0"):
        split_by_height(img, **kwargs)


def test_split_by_height_rejects_2d_array():
    with pytest.raises(ValueError, match="expects an"):
        split_by_height(np.zeros((10, 5), dtype=np.uint8))


# ----------------------------------------------------------------------
# split_by_white_lines
# ----------------------------------------------------------------------
def test_split_by_white_lines_content_only_on_last_row():
    img = np.full((4, 3, 3), 255, dtype=np.uint8)
    img[-1] = 0
    blocks = split_by_white_lines(img, padding=1)
    assert len(blocks) == 1
    assert blocks[0].shape == (3, 3, 3)
    assert np.all(blocks[0][0] == 255)
    assert np.all(blocks[0][1] == 0)
    assert np.all(blocks[0][-1] == 255)


def test_split_by_white_lines_zero_padding_keeps_content_extent():
    img = np.zeros((3, 4, 3), dtype=np.uint8)
    blocks = split_by_white_lines(img, padding=0)
    assert len(blocks) == 1
    np.testing.assert_array_equal(blocks[0], img)


def test_split_by_white_lines_nearly_white_row_is_content():
    img = np.full((1, 3, 3), 255, dtype=np.uint8)
    img[0, 1] = [254, 255, 255]
    blocks = split_by_white_lines(img, padding=0)
    assert len(blocks) == 1
    np.testing.assert_array_equal(blocks[0], img)


@pytest.mark.parametrize("shape", [(0, 3, 3), (3, 0, 3), (0, 0, 3)])
def test_split_by_white_lines_empty_image_returns_empty(shape):
    img = np.zeros(shape, dtype=np.uint8)
    assert split_by_white_lines(img) == []


def test_split_by_white_lines_rejects_negative_padding():
    img = np.zeros((2, 3, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="padding"):
        split_by_white_lines(img, padding=-1)


@pytest.mark.parametrize("shape", [(2, 3), (2, 3, 2), (2, 3, 4)])
def test_split_by_white_lines_rejects_non_rgb(shape):
    with pytest.raises(ValueError, match="expects an"):
        split_by_white_lines(np.zeros(shape, dtype=np.uint8))


# ----------------------------------------------------------------------
# crop_chars_region
# ----------------------------------------------------------------------
def test_crop_chars_region_margin_beyond_width_returns_full_image():
    img = np.zeros((4, 20, 3), dtype=np.uint8)
    cropped = crop_chars_region(img, num_chars=2, left_margin=100, char_width=10)
    assert cropped.shape == (4, 20, 3)


def test_crop_chars_region_zero_width_image():
    img = np.zeros((4, 0, 3), dtype=np.uint8)
    assert crop_chars_region(img, num_chars=2).shape == (4, 0, 3)


def test_crop_chars_region_zero_char_width():
    img = np.zeros((4, 20, 3), dtype=np.uint8)
    assert crop_chars_region(img, num_chars=2, left_margin=5, char_width=0).shape == (
        4,
        5,
        3,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"num_chars": -1},
        {"num_chars": 2, "left_margin": -1},
        {"num_chars": 2, "char_width": -1},
    ],
)
def test_crop_chars_region_rejects_negative_arguments(kwargs):
    img = np.zeros((4, 20, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="must be >= 0"):
        crop_chars_region(img, **kwargs)


def test_crop_chars_region_rejects_2d_array():
    with pytest.raises(ValueError, match="expects an"):
        crop_chars_region(np.zeros((4, 20), dtype=np.uint8), num_chars=2)


# ----------------------------------------------------------------------
# is_empty_image / is_new_paragraph
# ----------------------------------------------------------------------
def test_is_empty_image_empty_array_is_empty():
    assert is_empty_image(np.zeros((0, 0, 3), dtype=np.uint8)) is True


def test_is_empty_image_grayscale():
    assert is_empty_image(np.full((2, 2), 255, dtype=np.uint8)) is True
    assert is_empty_image(np.zeros((2, 2), dtype=np.uint8)) is False


def test_is_empty_image_single_pixel():
    assert is_empty_image(np.full((1, 1, 3), 255, dtype=np.uint8)) is True
    assert is_empty_image(np.array([[[254, 255, 255]]], dtype=np.uint8)) is False


def test_is_empty_image_rgba_transparent_white_is_not_white():
    # Documented semantics: every channel (including alpha) must be 255.
    assert is_empty_image(np.array([[[255, 255, 255, 0]]], dtype=np.uint8)) is False


def test_is_new_paragraph_ignores_alpha_channel():
    rgba = np.full((2, 5, 4), 255, dtype=np.uint8)
    rgba[..., 3] = 0
    assert is_new_paragraph(rgba, paragraph_threshold=5) is True

    rgba[0, 0, :3] = 0
    assert is_new_paragraph(rgba, paragraph_threshold=5) is False


def test_is_new_paragraph_white_threshold_boundary():
    img = np.full((1, 4, 3), 250, dtype=np.uint8)
    assert is_new_paragraph(img, white_threshold=250, paragraph_threshold=4) is True
    img[0, 0] = 249
    assert is_new_paragraph(img, white_threshold=250, paragraph_threshold=4) is False


def test_is_new_paragraph_zero_threshold_scans_nothing():
    img = np.zeros((2, 5, 3), dtype=np.uint8)
    assert is_new_paragraph(img, paragraph_threshold=0) is True


def test_is_new_paragraph_zero_width_image():
    img = np.zeros((2, 0, 3), dtype=np.uint8)
    assert is_new_paragraph(img, paragraph_threshold=30) is True


@pytest.mark.parametrize("white_threshold", [-1, 256])
def test_is_new_paragraph_rejects_bad_white_threshold(white_threshold):
    img = np.full((1, 2, 3), 255, dtype=np.uint8)
    with pytest.raises(ValueError, match="white_threshold"):
        is_new_paragraph(img, white_threshold=white_threshold)


def test_is_new_paragraph_rejects_negative_paragraph_threshold():
    img = np.full((1, 2, 3), 255, dtype=np.uint8)
    with pytest.raises(ValueError, match="paragraph_threshold"):
        is_new_paragraph(img, paragraph_threshold=-1)


@pytest.mark.parametrize("shape", [(2, 3), (2, 3, 2)])
def test_is_new_paragraph_rejects_bad_shape(shape):
    with pytest.raises(ValueError, match="expects an"):
        is_new_paragraph(np.zeros(shape, dtype=np.uint8))


# ----------------------------------------------------------------------
# encode_image_array
# ----------------------------------------------------------------------
@pytest.mark.parametrize("shape", [(0, 5, 3), (5, 0, 3), (0, 0, 3)])
def test_encode_rejects_empty_image(shape):
    with pytest.raises(ValueError, match="empty image"):
        encode_image_array(np.zeros(shape, dtype=np.uint8))


@pytest.mark.parametrize(
    "array", [np.zeros(4, dtype=np.uint8), np.zeros((1, 1, 1, 3), dtype=np.uint8)]
)
def test_encode_rejects_bad_dimensions(array):
    with pytest.raises(ValueError, match="2D or 3D"):
        encode_image_array(array)


def test_encode_normalises_lowercase_format():
    img = np.full((2, 3, 3), 7, dtype=np.uint8)
    assert Image.open(io.BytesIO(encode_image_array(img, "png"))).format == "PNG"
    assert Image.open(io.BytesIO(encode_image_array(img, "jpeg"))).format == "JPEG"


def test_encode_rgba_png_preserves_alpha():
    img = np.zeros((2, 2, 4), dtype=np.uint8)
    img[..., :3] = 200
    img[..., 3] = [[0, 64], [128, 255]]

    data = encode_image_array(img, "PNG")
    loaded = Image.open(io.BytesIO(data))
    assert loaded.mode == "RGBA"
    np.testing.assert_array_equal(np.array(loaded)[..., 3], img[..., 3])


def test_encode_rgba_jpeg_raises_clear_error():
    img = np.zeros((2, 2, 4), dtype=np.uint8)
    with pytest.raises(ValueError, match="alpha"):
        encode_image_array(img, "JPEG")


def test_encode_grayscale_array():
    img = np.full((3, 4), 42, dtype=np.uint8)
    loaded = Image.open(io.BytesIO(encode_image_array(img, "PNG")))
    assert loaded.mode == "L"
    assert loaded.size == (4, 3)


def test_encode_singleton_channel_axis():
    img = np.full((3, 4, 1), 42, dtype=np.uint8)
    loaded = Image.open(io.BytesIO(encode_image_array(img, "PNG")))
    assert loaded.mode == "L"


def test_encode_non_contiguous_input():
    img = np.zeros((4, 8, 3), dtype=np.uint8)
    img[:, ::2] = 255
    loaded = Image.open(io.BytesIO(encode_image_array(img[:, ::2], "PNG")))
    assert loaded.size == (4, 4)


def test_encode_unknown_format_raises():
    with pytest.raises(ValueError):
        encode_image_array(np.zeros((2, 2, 3), dtype=np.uint8), "NOTAFORMAT")


# ----------------------------------------------------------------------
# concat_image_slices_vertical
# ----------------------------------------------------------------------
def test_concat_grayscale_slices_is_supported():
    top = np.full((2, 3), 10, dtype=np.uint8)
    bottom = np.full((1, 3), 20, dtype=np.uint8)
    data = concat_image_slices_vertical([top, bottom], "PNG")

    loaded = Image.open(io.BytesIO(data))
    assert loaded.mode == "L"
    arr = np.array(loaded)
    assert arr.shape == (3, 3)
    assert np.all(arr[:2] == 10)
    assert np.all(arr[2:] == 20)


def test_concat_rgba_slices_png_preserves_alpha():
    top = np.zeros((2, 3, 4), dtype=np.uint8)
    bottom = np.zeros((1, 3, 4), dtype=np.uint8)
    top[..., 3] = 255
    bottom[..., 3] = 0

    loaded = Image.open(io.BytesIO(concat_image_slices_vertical([top, bottom], "PNG")))
    assert loaded.mode == "RGBA"
    alpha = np.array(loaded)[..., 3]
    assert np.all(alpha[:2] == 255)
    assert np.all(alpha[2:] == 0)


def test_concat_rgba_slices_jpeg_raises_clear_error():
    slice_ = np.zeros((2, 2, 4), dtype=np.uint8)
    with pytest.raises(ValueError, match="alpha"):
        concat_image_slices_vertical([slice_], "JPEG")


@pytest.mark.parametrize(
    "bad_slice",
    [
        np.zeros((0, 3, 3), dtype=np.uint8),
        np.zeros((3, 0, 3), dtype=np.uint8),
        np.zeros(4, dtype=np.uint8),
        np.zeros((2, 2, 1, 3), dtype=np.uint8),
    ],
)
def test_concat_rejects_invalid_slice(bad_slice):
    with pytest.raises(ValueError):
        concat_image_slices_vertical([bad_slice], "PNG")


def test_concat_rejects_mixed_channels():
    rgb = np.zeros((2, 3, 3), dtype=np.uint8)
    gray = np.zeros((2, 3), dtype=np.uint8)
    with pytest.raises(ValueError):
        concat_image_slices_vertical([rgb, gray], "PNG")


def test_concat_accepts_lowercase_format():
    top = np.zeros((1, 2, 3), dtype=np.uint8)
    data = concat_image_slices_vertical([top], "png")
    assert Image.open(io.BytesIO(data)).format == "PNG"


# ----------------------------------------------------------------------
# check_image_health
# ----------------------------------------------------------------------
def test_health_encoded_bytes():
    health = check_image_health(png_bytes(size=(7, 5)))
    assert isinstance(health, ImageHealth)
    assert health.ok is True
    assert (health.width, health.height) == (7, 5)
    assert health.mode == "RGB"
    assert health.format == "PNG"
    assert health.error is None
    assert health.warnings == ()


@pytest.mark.parametrize("wrapper", [bytes, bytearray, memoryview])
def test_health_accepts_bytes_like(wrapper):
    health = check_image_health(wrapper(png_bytes(size=(3, 2))))
    assert health.ok is True
    assert (health.width, health.height) == (3, 2)


def test_health_accepts_path_and_str(tmp_path):
    path = tmp_path / "ok.png"
    path.write_bytes(png_bytes(size=(4, 2)))
    assert check_image_health(path).ok is True
    assert check_image_health(str(path)).ok is True


def test_health_missing_path_is_reported_not_raised(tmp_path):
    health = check_image_health(tmp_path / "missing.png")
    assert health.ok is False
    assert health.error is not None


def test_health_corrupt_bytes_is_reported_not_raised():
    health = check_image_health(b"this is not an image")
    assert health.ok is False
    assert "UnidentifiedImageError" in (health.error or "")


def test_health_truncated_png_is_detected():
    health = check_image_health(png_bytes(size=(20, 20))[:-24])
    assert health.ok is False
    assert health.error is not None


def test_health_array_rgb():
    health = check_image_health(np.zeros((5, 7, 3), dtype=np.uint8))
    assert health.ok is True
    assert (health.width, health.height) == (7, 5)
    assert health.mode == "RGB"
    assert health.format is None


@pytest.mark.parametrize(
    ("shape", "mode"),
    [
        ((4, 3), "L"),
        ((4, 3, 1), "L"),
        ((4, 3, 2), "LA"),
        ((4, 3, 4), "RGBA"),
    ],
)
def test_health_array_modes(shape, mode):
    health = check_image_health(np.zeros(shape, dtype=np.uint8))
    assert health.ok is True
    assert health.mode == mode


def test_health_array_empty_is_unhealthy():
    health = check_image_health(np.zeros((0, 3, 3), dtype=np.uint8))
    assert health.ok is False
    assert "empty image" in (health.error or "")


@pytest.mark.parametrize("shape", [(4,), (1, 2, 3, 4), (4, 3, 5)])
def test_health_array_bad_shape_is_unhealthy(shape):
    health = check_image_health(np.zeros(shape, dtype=np.uint8))
    assert health.ok is False
    assert health.error is not None


def test_health_array_dtype_warning():
    health = check_image_health(np.zeros((2, 2, 3), dtype=np.float32))
    assert health.ok is True
    assert any("dtype" in warning for warning in health.warnings)


def test_health_min_size_limits():
    data = png_bytes(size=(4, 3))
    assert check_image_health(data, min_width=4, min_height=3).ok is True

    too_wide = check_image_health(data, min_width=5)
    assert too_wide.ok is False
    assert "min_width" in (too_wide.error or "")

    too_tall = check_image_health(data, min_height=4)
    assert too_tall.ok is False
    assert "min_height" in (too_tall.error or "")


def test_health_max_size_and_pixel_limits():
    data = png_bytes(size=(10, 8))
    assert check_image_health(data, max_width=10, max_height=8).ok is True

    assert "max_width" in (check_image_health(data, max_width=9).error or "")
    assert "max_height" in (check_image_health(data, max_height=7).error or "")
    assert "max_pixels" in (check_image_health(data, max_pixels=79).error or "")
    assert check_image_health(data, max_pixels=80).ok is True


def test_health_allowed_modes():
    data = png_bytes(size=(2, 2), mode="RGB")
    assert check_image_health(data, allowed_modes={"RGB", "RGBA"}).ok is True

    health = check_image_health(data, allowed_modes={"L"})
    assert health.ok is False
    assert "mode" in (health.error or "")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_width": -1},
        {"min_height": -1},
        {"max_width": -1},
        {"max_height": -1},
        {"max_pixels": -1},
    ],
)
def test_health_rejects_negative_limits(kwargs):
    with pytest.raises(ValueError):
        check_image_health(png_bytes(size=(2, 2)), **kwargs)


def test_health_detects_decompression_bomb(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 4)
    health = check_image_health(png_bytes(size=(4, 4)))
    assert health.ok is False
    assert "DecompressionBomb" in (health.error or "")


def test_health_requires_optional_dependencies(monkeypatch):
    monkeypatch.setattr(image_utils, "np", None)
    with pytest.raises(RuntimeError, match="numpy"):
        check_image_health(b"anything")
