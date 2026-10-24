#!/usr/bin/env python3
"""
novel_downloader.libs.image_utils
---------------------------------
"""

from __future__ import annotations

import io
from collections.abc import Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

# ``numpy`` and Pillow live in the optional ``image-utils`` extra. Import the
# module without them must still succeed so unrelated commands keep working;
# the public helpers below raise a clear error only when actually called.
if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray
    from PIL import Image, ImageOps
else:
    try:
        import numpy as np
        from numpy.typing import NDArray
    except ImportError:  # pragma: no cover - depends on environment
        np = None  # type: ignore[assignment]
        NDArray = Any  # type: ignore[misc,assignment]

    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - depends on environment
        Image = None  # type: ignore[assignment]
        ImageOps = None  # type: ignore[assignment]


def _require_image_deps() -> None:
    """Raise an actionable error when the optional image dependencies are absent."""
    missing: list[str] = []
    if np is None:
        missing.append("numpy")
    if Image is None or ImageOps is None:
        missing.append("Pillow")
    if missing:
        raise RuntimeError(
            "novel_downloader.libs.image_utils requires the optional "
            f"dependencies: {', '.join(missing)}. "
            'Install them with: pip install "novel-downloader[image-utils]"'
        )


def load_image_array_bytes(data: bytes, white_bg: bool = False) -> NDArray[np.uint8]:
    """
    Decode image bytes into an RGB NumPy array (with white background).

    Reads common image formats (e.g. PNG/JPEG/WebP) from an
    in-memory byte buffer using Pillow, converts the image to RGB,
    and returns a NumPy array suitable for OCR inference.

    EXIF orientation is applied, so the array matches the image as it is
    normally displayed.

    :param data: Image file content as raw bytes.
    :param white_bg: If True, flatten images with transparency onto white.
    :return: NumPy array of shape (H, W, 3), dtype=uint8, in RGB order.
    :raises PIL.UnidentifiedImageError, OSError: If input bytes cannot be decoded.
    """
    _require_image_deps()
    with Image.open(io.BytesIO(data)) as img:
        return _pil_to_rgb_array(img, white_bg)


def load_image_array_path(path: Path, white_bg: bool = False) -> NDArray[np.uint8]:
    """
    Load a image into an RGB NumPy array (with white background).

    EXIF orientation is applied, so the array matches the image as it is
    normally displayed.

    :param path: Image file path.
    :param white_bg: If True, flatten images with transparency onto white.
    :return: Numpy array representing the image (H, W, 3)
    """
    _require_image_deps()
    with Image.open(path) as img:
        return _pil_to_rgb_array(img, white_bg)


def _rgb_view(img: NDArray[np.uint8], func_name: str) -> NDArray[np.uint8]:
    """
    Return a view of the RGB channels of an RGB/RGBA array.

    Boolean-mask assignment on ``(H, W, 4)`` arrays cannot use a 3-value
    colour, so the filters write through this view and keep the alpha
    channel untouched.
    """
    if img.ndim != 3 or img.shape[2] not in (3, 4):
        raise ValueError(
            f"{func_name} expects an (H, W, 3) or (H, W, 4) array, "
            f"got shape {img.shape!r}"
        )
    return img[..., :3]


def filter_orange_watermark(img: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """
    Remove orange-like watermark colors by replacing them with white.

    Input may be RGB or RGBA; any alpha channel is preserved as-is. The
    input array is modified in-place.

    :param img: Input image as numpy array (H, W, 3) or (H, W, 4)
    :return: Filtered numpy array, same shape as the input
    """
    _require_image_deps()
    rgb = _rgb_view(img, "filter_orange_watermark")
    if rgb.size == 0:
        return img

    pil_img = Image.fromarray(np.ascontiguousarray(rgb)).convert("HSV")
    hsv_arr = np.array(pil_img)

    lower_h, upper_h = int(10 / 360 * 255), int(40 / 360 * 255)

    mask_orange = (
        (hsv_arr[:, :, 0] >= lower_h)
        & (hsv_arr[:, :, 0] <= upper_h)
        & (hsv_arr[:, :, 1] > 50)
        & (hsv_arr[:, :, 2] > 100)
    )

    rgb[mask_orange] = [255, 255, 255]
    return img


def filter_gray_watermark(
    img: NDArray[np.uint8],
    threshold: int = 200,
    background: tuple[int, int, int] = (255, 255, 255),
) -> NDArray[np.uint8]:
    """
    Remove gray-like watermark colors by replacing them with background color.

    Input may be RGB or RGBA; any alpha channel is preserved as-is. The
    input array is modified in-place.

    :param img: Input image as numpy array (H, W, 3) or (H, W, 4)
    :param threshold: Per-channel brightness threshold in the range 0-255.
    :param background: Replacement colour for the RGB channels.
    :return: Filtered numpy array, same shape as the input
    """
    _require_image_deps()
    if not 0 <= threshold <= 255:
        raise ValueError(f"threshold must be between 0 and 255, got {threshold}")

    rgb = _rgb_view(img, "filter_gray_watermark")
    if rgb.size == 0:
        return img

    img16 = rgb.astype(np.uint16, copy=False)
    sum_rgb = img16[..., 0] + img16[..., 1]
    sum_rgb += img16[..., 2]
    mask = sum_rgb > threshold * 3
    rgb[mask] = background
    return img


def split_by_height(
    img: NDArray[np.uint8],
    height: int = 38,
    top_offset: int = 10,
    bottom_offset: int = 10,
    per_chunk_top_ignore: int = 10,
) -> list[NDArray[np.uint8]]:
    """
    Split an image vertically into chunks of fixed height,
    with optional global offsets and per-chunk top ignore.

    :param img: Numpy array representing the image (H, W, C)
    :param height: Height of each chunk (must be positive)
    :param top_offset: Number of pixels to skip from the top (whole image)
    :param bottom_offset: Number of pixels to skip from the bottom (whole image)
    :param per_chunk_top_ignore: Number of pixels to skip from the top of each chunk
    :return: List of numpy arrays, each a sub-image of the original
    :raises ValueError: If ``height`` is not positive or an offset is negative.
    """
    _require_image_deps()
    if img.ndim != 3:
        raise ValueError(
            f"split_by_height expects an (H, W, C) array, got {img.shape!r}"
        )
    if height <= 0:
        raise ValueError(f"height must be positive, got {height}")
    if top_offset < 0 or bottom_offset < 0 or per_chunk_top_ignore < 0:
        raise ValueError(
            "top_offset, bottom_offset and per_chunk_top_ignore must be >= 0"
        )

    h, _w = img.shape[:2]
    chunks = []
    effective_height = h - top_offset - bottom_offset

    for y in range(0, effective_height, height):
        chunk = img[top_offset + y : top_offset + y + height, :, :]
        if per_chunk_top_ignore > 0 and chunk.shape[0] > per_chunk_top_ignore:
            chunk = chunk[per_chunk_top_ignore:, :, :]
        chunks.append(chunk)

    return chunks


def split_by_white_lines(
    img_arr: NDArray[np.uint8], padding: int = 4
) -> list[NDArray[np.uint8]]:
    """
    Split an RGB image (numpy array) into multiple blocks using full-white
    horizontal separator lines, then add fixed white padding to the top and
    bottom of each block.

    :param img_arr: Input RGB image array with shape (H, W, 3).
    :param padding: Number of white pixels to pad on the top and bottom.
    :return: List of sliced blocks as numpy arrays.
    :raises ValueError: If the array is not RGB or ``padding`` is negative.
    """
    _require_image_deps()
    if img_arr.ndim != 3 or img_arr.shape[2] != 3:
        raise ValueError(
            f"split_by_white_lines expects an (H, W, 3) array, got {img_arr.shape!r}"
        )
    if padding < 0:
        raise ValueError(f"padding must be >= 0, got {padding}")

    h, w = img_arr.shape[:2]
    if h == 0 or w == 0:
        return []

    # vectorized detection: row is white if min(pixel) == 255
    pix_and = np.bitwise_and.reduce(img_arr, axis=2)
    white_mask = pix_and.min(axis=1) == 255

    # Invert: 1 for content (non-white)
    content_mask = ~white_mask

    if not content_mask.any():
        return []  # whole image is white

    # Locate ranges of consecutive content rows
    # Example: 0 1 1 0 0 1 1 1 0
    # start_indices = [0, 5]
    # end_indices   = [2, 8]
    diff = np.diff(content_mask.astype(np.int8))
    starts = np.flatnonzero(diff == 1) + 1
    ends = np.flatnonzero(diff == -1) + 1

    # If the sequence starts with content
    if content_mask[0]:
        starts = np.r_[0, starts]
    # If the sequence ends with content
    if content_mask[-1]:
        ends = np.r_[ends, h]

    # Prepare padding rows
    pad_block = np.full((padding, w, 3), 255, dtype=np.uint8)

    blocks = []
    for start, end in zip(starts, ends, strict=False):
        block = img_arr[start:end]
        padded = np.concatenate((pad_block, block, pad_block), axis=0)
        blocks.append(padded)

    return blocks


def crop_chars_region(
    img: NDArray[np.uint8],
    num_chars: int,
    left_margin: int = 14,
    char_width: int = 28,
) -> NDArray[np.uint8]:
    """
    Crop the image to keep only the region that covers the specified
    number of characters starting after the left margin.

    :param img: Input image as (H, W, 3) numpy array
    :param num_chars: How many characters to keep
    :param left_margin: Number of columns to skip from the left
    :param char_width: Width of one character (in pixels)
    :return: Cropped image as (H, W', 3)
    :raises ValueError: If the array is not 3D or any argument is negative.
    """
    _require_image_deps()
    if img.ndim != 3:
        raise ValueError(
            f"crop_chars_region expects an (H, W, C) array, got {img.shape!r}"
        )
    if num_chars < 0 or left_margin < 0 or char_width < 0:
        raise ValueError("num_chars, left_margin and char_width must be >= 0")

    _h, w = img.shape[:2]
    end_col = min(left_margin + char_width * num_chars, w)
    return img[:, :end_col, :]


def is_empty_image(img: NDArray[np.uint8]) -> bool:
    """
    Check if the image is completely white (255, 255, 255).

    An empty array (zero pixels) is considered empty.

    :param img: Input image as (H, W, 3) numpy array
    :return: True if all pixels are white, False otherwise
    """
    _require_image_deps()
    return bool(np.all(img == 255))


def is_new_paragraph(
    img: np.ndarray,
    white_threshold: int = 250,
    paragraph_threshold: int = 30,
) -> bool:
    """
    Determine whether a line image represents the start of a new paragraph,
    based on the amount of leading full-white columns.

    For RGBA input only the RGB channels are considered, so a transparent
    white pixel still counts as white.

    :param img: Input line image as (H, W, 3) or (H, W, 4) numpy array.
    :param white_threshold: Minimum pixel value considered as white (0-255).
    :param paragraph_threshold: Left margin (in px) to classify as new paragraph.
    :return: True if this line starts a new paragraph.
    :raises ValueError: If the array shape or a threshold is invalid.
    """
    _require_image_deps()
    if img.ndim != 3 or img.shape[2] < 3:
        raise ValueError(
            f"is_new_paragraph expects an (H, W, 3+) array, got {img.shape!r}"
        )
    if not 0 <= white_threshold <= 255:
        raise ValueError(
            f"white_threshold must be between 0 and 255, got {white_threshold}"
        )
    if paragraph_threshold < 0:
        raise ValueError(f"paragraph_threshold must be >= 0, got {paragraph_threshold}")

    _h, w = img.shape[:2]
    max_scan = min(w, paragraph_threshold)
    if max_scan == 0:
        return True

    # Integer RGB sum: avoids float mean. Slice RGB to ignore alpha.
    rgb_sum = img[:, :max_scan, :3].sum(axis=2, dtype=np.uint16)
    return bool((rgb_sum >= 3 * white_threshold).all())


def encode_image_array(
    img: NDArray[np.uint8],
    format: str = "JPEG",
) -> bytes:
    """
    Encode a single RGB image array into the specified format and return
    the raw image bytes.

    :param img: Image as a numpy array (H, W, 3), dtype=uint8, RGB.
    :param format: Output format ("JPEG" or "PNG"), case-insensitive.
    :return: Encoded image bytes.
    :raises ValueError: If the image is empty, has unsupported dimensions,
        or alpha cannot be represented in the requested format.
    """
    _require_image_deps()
    if img.ndim not in (2, 3):
        raise ValueError(
            f"encode_image_array expects a 2D or 3D array, got {img.ndim}D"
        )
    if img.size == 0 or img.shape[0] == 0 or img.shape[1] == 0:
        raise ValueError("cannot encode an empty image")

    fmt = format.upper()
    if fmt == "JPEG" and img.ndim == 3 and img.shape[2] in (2, 4):
        raise ValueError(
            "JPEG does not support an alpha channel; use PNG or remove the alpha "
            "channel before encoding"
        )

    pil_img = _as_pillow_image(img)

    buf = io.BytesIO()
    _save_pillow_image(pil_img, buf, fmt)
    return buf.getvalue()


def concat_image_slices_vertical(
    slices: list[NDArray[np.uint8]],
    format: str = "JPEG",
) -> bytes:
    """
    Vertically concatenate a list of RGB image slices and return the encoded
    image bytes in the specified format.

    Slices must share the same width and number of channels. Grayscale and
    RGBA slices are supported; an alpha channel is preserved for formats that
    support it (e.g. PNG).

    :param slices: List of image numpy arrays (H, W, 3).
    :param format: Output image format ("JPEG" or "PNG"), case-insensitive.
    :return: Encoded image bytes.
    :raises ValueError: If slices is empty, a slice is empty, the slices are
        incompatible, or alpha cannot be represented in the requested format.
    """
    _require_image_deps()
    if not slices:
        raise ValueError("No slices provided.")

    for idx, item in enumerate(slices):
        if item.ndim not in (2, 3):
            raise ValueError(f"slice {idx} must be a 2D or 3D array, got {item.ndim}D")
        if item.size == 0 or item.shape[0] == 0 or item.shape[1] == 0:
            raise ValueError(f"slice {idx} is empty")

    combined = np.concatenate(slices, axis=0)
    if combined.size == 0:
        raise ValueError("cannot encode an empty image")

    fmt = format.upper()
    if fmt == "JPEG" and combined.ndim == 3 and combined.shape[2] in (2, 4):
        raise ValueError(
            "JPEG does not support an alpha channel; use PNG or remove the alpha "
            "channel before encoding"
        )

    img = _as_pillow_image(combined)
    buf = io.BytesIO()
    _save_pillow_image(img, buf, fmt)
    return buf.getvalue()


@dataclass(frozen=True, slots=True)
class ImageHealth:
    """
    Result of :func:`check_image_health`.

    :ivar ok: True when the image was readable and passed every check.
    :ivar width: Image width in pixels (0 when it could not be determined).
    :ivar height: Image height in pixels (0 when it could not be determined).
    :ivar mode: Pillow mode (or a mode inferred from array channels).
    :ivar format: Container format reported by Pillow (``None`` for arrays).
    :ivar error: Human-readable failure reason, or ``None`` when healthy.
    :ivar warnings: Non-fatal findings (for example an unexpected dtype).
    """

    ok: bool
    width: int = 0
    height: int = 0
    mode: str | None = None
    format: str | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()


def check_image_health(
    source: bytes | bytearray | memoryview | str | Path | NDArray[np.uint8],
    *,
    min_width: int = 1,
    min_height: int = 1,
    max_width: int | None = None,
    max_height: int | None = None,
    max_pixels: int | None = None,
    allowed_modes: Collection[str] | None = None,
    verify: bool = True,
) -> ImageHealth:
    """
    Inspect an encoded image or an in-memory array without raising on bad input.

    The function is intentionally standalone: download pipelines may call it
    before accepting an image, but nothing in the library calls it
    automatically. Encoded input is parsed by Pillow and, when ``verify`` is
    true, its integrity is checked (this catches truncated/corrupt files).
    Arrays are checked structurally.

    :param source: Raw image bytes, a filesystem path, or an image array.
    :param min_width: Minimum acceptable width in pixels.
    :param min_height: Minimum acceptable height in pixels.
    :param max_width: Optional maximum width in pixels.
    :param max_height: Optional maximum height in pixels.
    :param max_pixels: Optional maximum ``width * height``.
    :param allowed_modes: Optional collection of accepted Pillow mode names.
    :param verify: Verify encoded image integrity (default ``True``).
    :return: An :class:`ImageHealth` describing the result. Corrupt or missing
        input produces ``ok=False`` with ``error`` instead of an exception.
    :raises ValueError: If a limit is negative.
    :raises RuntimeError: If the optional image dependencies are missing.
    """
    _require_image_deps()
    _validate_health_limits(min_width, min_height, max_width, max_height, max_pixels)

    if isinstance(source, np.ndarray):
        return _check_array_health(
            source,
            min_width=min_width,
            min_height=min_height,
            max_width=max_width,
            max_height=max_height,
            max_pixels=max_pixels,
            allowed_modes=allowed_modes,
        )

    width = height = 0
    mode: str | None = None
    fmt: str | None = None
    try:
        target: io.BytesIO | Path
        if isinstance(source, (bytes, bytearray, memoryview)):
            target = io.BytesIO(bytes(source))
        else:
            target = Path(source)

        with Image.open(target) as img:
            width, height = img.size
            mode = img.mode
            fmt = img.format
            if verify:
                img.verify()
    except Exception as exc:
        return ImageHealth(
            ok=False,
            width=width,
            height=height,
            mode=mode,
            format=fmt,
            error=f"{type(exc).__name__}: {exc}",
        )

    return _evaluate_health(
        width,
        height,
        mode,
        fmt,
        min_width=min_width,
        min_height=min_height,
        max_width=max_width,
        max_height=max_height,
        max_pixels=max_pixels,
        allowed_modes=allowed_modes,
    )


def _validate_health_limits(
    min_width: int,
    min_height: int,
    max_width: int | None,
    max_height: int | None,
    max_pixels: int | None,
) -> None:
    """Validate the numeric limits accepted by :func:`check_image_health`."""
    for name, value in (("min_width", min_width), ("min_height", min_height)):
        if value < 0:
            raise ValueError(f"{name} must be >= 0, got {value}")
    for name, opt in (
        ("max_width", max_width),
        ("max_height", max_height),
        ("max_pixels", max_pixels),
    ):
        if opt is not None and opt < 0:
            raise ValueError(f"{name} must be >= 0 when provided, got {opt}")


def _check_array_health(
    arr: NDArray[np.uint8],
    *,
    min_width: int,
    min_height: int,
    max_width: int | None,
    max_height: int | None,
    max_pixels: int | None,
    allowed_modes: Collection[str] | None,
) -> ImageHealth:
    """Structural health check for an in-memory image array."""
    if arr.ndim < 2:
        return ImageHealth(
            ok=False,
            error=f"expected at least 2 dimensions, got {arr.ndim}",
        )

    height, width = int(arr.shape[0]), int(arr.shape[1])
    if arr.ndim == 2:
        mode: str | None = "L"
    elif arr.ndim == 3:
        channels = int(arr.shape[2])
        mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}.get(channels)
        if mode is None:
            return ImageHealth(
                ok=False,
                width=width,
                height=height,
                error=f"unsupported channel count {channels}",
            )
    else:
        return ImageHealth(
            ok=False,
            width=width,
            height=height,
            error=f"expected a 2D or 3D array, got {arr.ndim} dimensions",
        )

    health = _evaluate_health(
        width,
        height,
        mode,
        None,
        min_width=min_width,
        min_height=min_height,
        max_width=max_width,
        max_height=max_height,
        max_pixels=max_pixels,
        allowed_modes=allowed_modes,
    )
    if arr.dtype != np.uint8:
        return replace(
            health,
            warnings=(
                *health.warnings,
                f"unexpected dtype {arr.dtype}; expected uint8",
            ),
        )
    return health


def _evaluate_health(
    width: int,
    height: int,
    mode: str | None,
    fmt: str | None,
    *,
    min_width: int,
    min_height: int,
    max_width: int | None,
    max_height: int | None,
    max_pixels: int | None,
    allowed_modes: Collection[str] | None,
) -> ImageHealth:
    """Apply size/mode limits and build the health result."""
    if width <= 0 or height <= 0:
        return ImageHealth(
            ok=False,
            width=width,
            height=height,
            mode=mode,
            format=fmt,
            error=f"empty image ({width}x{height})",
        )

    errors: list[str] = []
    if width < min_width:
        errors.append(f"width {width} < min_width {min_width}")
    if height < min_height:
        errors.append(f"height {height} < min_height {min_height}")
    if max_width is not None and width > max_width:
        errors.append(f"width {width} > max_width {max_width}")
    if max_height is not None and height > max_height:
        errors.append(f"height {height} > max_height {max_height}")
    if max_pixels is not None and width * height > max_pixels:
        errors.append(f"pixels {width * height} > max_pixels {max_pixels}")
    if allowed_modes is not None and mode not in allowed_modes:
        allowed = ", ".join(sorted(allowed_modes))
        errors.append(f"mode {mode!r} not in allowed modes ({allowed})")

    return ImageHealth(
        ok=not errors,
        width=width,
        height=height,
        mode=mode,
        format=fmt,
        error="; ".join(errors) if errors else None,
    )


def _as_pillow_image(img: NDArray[np.uint8]) -> Image.Image:
    """Build a PIL image from an array, dropping a singleton channel axis."""
    array = np.ascontiguousarray(img)
    if array.ndim == 3 and array.shape[2] == 1:
        array = array[..., 0]
    return Image.fromarray(array)


def _save_pillow_image(img: Image.Image, buf: io.BytesIO, fmt: str) -> None:
    """Save ``img`` into ``buf``, translating unknown-format errors."""
    try:
        if fmt == "JPEG":
            img.save(buf, format=fmt, quality=95, subsampling=0)
        else:
            img.save(buf, format=fmt)
    except KeyError as exc:
        raise ValueError(f"unsupported image format: {fmt!r}") from exc


def _pil_to_rgb_array(img: Image.Image, white_bg: bool) -> NDArray[np.uint8]:
    """Convert PIL image to RGB numpy array, optionally flattening alpha."""
    _require_image_deps()

    # Honour EXIF orientation so arrays match what image viewers display.
    img = ImageOps.exif_transpose(img)

    if img.mode == "P":
        img = img.convert("RGBA")

    if white_bg and ("A" in img.getbands()):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.getchannel("A"))
        img = bg.convert("RGB")
    else:
        img = img.convert("RGB")

    return np.array(img, copy=True)
